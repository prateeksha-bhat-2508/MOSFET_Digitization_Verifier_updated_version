"""WPD-derived *visual aids* only; never independent Level 2 calibration.

If OCR cannot recover source axes, the reviewer can still see individual CSV
points drawn over a pixel-matched standalone graph. This path deliberately
inherits the WPD calibration and can NOT validate it: its status is REVIEW.
No change to source files and no data-dependent scale guessing is allowed.
"""
from __future__ import annotations

import io
import math
import tarfile
from pathlib import Path

import numpy as np
from PIL import Image


def _number(value):
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _axis(markers, name, scale):
    if scale not in {"linear", "log"}:
        return None
    a, b = markers.get(name + "1"), markers.get(name + "2")
    if a is None or b is None:
        return None
    coordinate = "px" if name == "X" else "py"
    numeric = "dx" if name == "X" else "dy"
    p1, p2 = _number(getattr(a, coordinate, None)), _number(getattr(b, coordinate, None))
    v1, v2 = _number(getattr(a, numeric, None)), _number(getattr(b, numeric, None))
    if None in (p1, p2, v1, v2) or abs(p2-p1) < 2:
        return None
    if scale == "log":
        if min(v1, v2) <= 0:
            return None
        v1, v2 = math.log10(v1), math.log10(v2)
    slope = (v2-v1)/(p2-p1)
    if abs(slope) < 1e-12:
        return None
    return {"kind": scale, "coef": [slope, v1-slope*p1], "status": "REVIEW", "n": 2}


def _matches_embedded_image(source: Path, tar_path: Path):
    """Require the standalone and embedded graph to share pixel coordinates."""
    source_image = Image.open(source).convert("RGB")
    source_array = np.asarray(source_image, dtype=np.int16)
    with tarfile.open(tar_path, "r:*") as archive:
        candidates = [item for item in archive.getmembers() if item.isfile()
                      and Path(item.name).suffix.lower() in {".jpeg", ".jpg", ".png", ".bmp", ".tif", ".tiff", ".webp"}]
        for member in candidates:
            raw = archive.extractfile(member)
            if raw is None:
                continue
            try:
                embedded = Image.open(io.BytesIO(raw.read())).convert("RGB")
            except (OSError, ValueError):
                continue
            if embedded.size != source_image.size:
                continue
            embedded_array = np.asarray(embedded, dtype=np.int16)
            differences = np.abs(source_array - embedded_array)
            if float(differences.mean()) <= 2.0 and float(np.quantile(differences, 0.99)) <= 14:
                return True
    return False


def build_wpd_preview_mapping(source, device_dir, projects):
    """Return mapping for a non-independent visual preview, if safe to draw.

    This deliberately never produces a PASS result, even for identical images
    and all aligned CSV points. It is *not* passed to the independent mapper.
    """
    if source is None:
        return None
    source = Path(source)
    device_dir = Path(device_dir)
    if not source.is_file():
        return None
    from wpd_inventory import classify_family
    family = classify_family(source.name)
    for project in projects:
        if getattr(project, "family", None) != family:
            continue
        name = getattr(project, "project", "")
        tar_path = device_dir / name
        if not tar_path.is_file() or len(getattr(project, "axis_settings", []) or []) != 1:
            continue
        markers = {p.role: p for p in getattr(project, "calibration", [])}
        x_fit = _axis(markers, "X", getattr(project, "x_scale", ""))
        y_fit = _axis(markers, "Y", getattr(project, "y_scale", ""))
        if not x_fit or not y_fit:
            continue
        try:
            if not _matches_embedded_image(source, tar_path):
                continue
        except (OSError, ValueError, tarfile.TarError):
            continue
        return {"x": x_fit, "y": y_fit, "source": "WPD_TAR_PREVIEW_ONLY",
                "tar_file": name, "independent": False}
    return None
