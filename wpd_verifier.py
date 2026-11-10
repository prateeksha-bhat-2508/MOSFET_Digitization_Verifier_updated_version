"""WPD Verifier 4.3.4 entry point and shared result types.

Inputs are read only. Reports are Word documents and an Excel workbook.
Independent axes come exclusively from image evidence.
"""
from __future__ import annotations
import io
import json
import math
import sys
import tarfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from wpd_inventory import (
    IMAGE_EXTS, norm, classify_family, detect_vgs, detect_temperature,
    expected_filename, discover_devices, iter_device_files, load_csv_xy,
)
from wpd_mapping import (
    detect_plot_rect_and_ticks, data_to_pixel, pixel_to_data,
    configure_tesseract,
)
VERSION = "4.3.4"
OUTPUT_FOLDER_NAME = "WPD_Verifier_Output"
MANDATORY_FAMILIES = ("CC", "OC", "TC")


def aggregate(statuses: Iterable[str]) -> str:
    values = [s for s in statuses if s and s not in {"WARNING", "EXCLUDED", "NOT_APPLICABLE"}]
    if "FAIL" in values:
        return "FAIL"
    if not values or "REVIEW" in values:
        return "REVIEW"
    return "PASS"


@dataclass
class FileAudit:
    file_type: str
    family: str
    current_filename: str
    expected_filename: str
    status: str
    comment: str



@dataclass
class RequirementCheck:
    category: str
    family: str
    requirement: str
    expected: str
    found: str
    status: str
    comment: str
    issue_code: str = ''
    severity: str = ''



@dataclass
class CalibrationPoint:
    role: str
    px: float | None
    py: float | None
    dx: Any = None
    dy: Any = None
    dz: Any = None

    @property
    def displayed_value(self):
        return self.dx if self.role in {'X1', 'X2'} else self.dy



@dataclass
class ProjectResult:
    project: str
    family: str
    graph_type: str = ''
    x_scale: str = ''
    y_scale: str = ''
    image_name: str = ''
    datasets: list[dict] = field(default_factory=list)
    calibration: list[CalibrationPoint] = field(default_factory=list)
    calibration_checks: list[dict] = field(default_factory=list)
    axis_settings: list[dict] = field(default_factory=list)
    source_axis_summary: str = ''
    calibration_visual: str = ''
    extraction_visual: str = ''
    level1_report: str = ''
    graph_type_status: str = 'REVIEW'
    scale_status: str = 'REVIEW'
    marker_geometry_status: str = 'REVIEW'
    marker_value_status: str = 'REVIEW'
    calibration_status: str = 'REVIEW'
    dataset_status: str = 'REVIEW'
    trace_status: str = 'REVIEW'
    trace_median_error_px: float | None = None
    trace_p95_error_px: float | None = None
    completeness_status: str = 'REVIEW'
    overall_status: str = 'REVIEW'
    comments: list[str] = field(default_factory=list)



@dataclass
class Level2Result:
    csv_file: str
    family: str
    source_image: str = ''
    diagnostic_visual: str = ''
    axis_debug_visual: str = ''
    overlay_visual: str = ''
    overlay_kind: str = 'UNAVAILABLE'
    origin_px: tuple[float, float] | None = None
    axis_status: str = 'REVIEW'
    trace_status: str = 'REVIEW'
    x_order_status: str = 'REVIEW'
    overall_status: str = 'REVIEW'
    median_error_px: float | None = None
    p95_error_px: float | None = None
    x_mapping: str = ''
    y_mapping: str = ''
    ocr_x_labels: list[tuple] = field(default_factory=list)
    ocr_y_labels: list[tuple] = field(default_factory=list)
    x_axis_evidence: list[dict] = field(default_factory=list)
    y_axis_evidence: list[dict] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    source_visual: str = ''
    origin_kind: str = ''
    anchor_values: tuple | None = None
    point_count: int = 0
    first_point: list | None = None
    last_point: list | None = None
    invalid_projection_count: int = 0
    outside_plot_count: int = 0



@dataclass
class DeviceResult:
    intern: str
    device: str
    path: str
    files: list[FileAudit] = field(default_factory=list)
    requirements: list[RequirementCheck] = field(default_factory=list)
    datasheets: list[str] = field(default_factory=list)
    inventory: dict[str, int] = field(default_factory=dict)
    projects: list[ProjectResult] = field(default_factory=list)
    level2: list[Level2Result] = field(default_factory=list)
    datasheet_status: str = 'REVIEW'
    file_completeness_status: str = 'REVIEW'
    no_digitization_files: bool = False
    fatal_error: str = ''
    warnings: list[str] = field(default_factory=list)
    cc_status: str = 'REVIEW'
    oc_status: str = 'REVIEW'
    tc_status: str = 'REVIEW'
    overall_status: str = 'REVIEW'
    level1_report_docx: str = ''
    level2_report_docx: str = ''
    report_docx: str = ''
    availability: dict = field(default_factory=dict)
    availability_flags: list[str] = field(default_factory=list)
    graph_in_datasheet: dict = field(default_factory=dict)
    family_comments: dict[str, str] = field(default_factory=dict)
    source_notes: list[str] = field(default_factory=list)
    device_relpath: str = ''
    report_key: str = ''
    source_snapshot: str = ''



def read_tar_project(tar_path: Path) -> dict:
    with tarfile.open(tar_path, 'r:*') as tf:
        members = [m for m in tf.getmembers() if m.isfile()]
        wpd_member = next((m for m in members if Path(m.name).name.lower() == 'wpd.json'), None)
        if not wpd_member:
            raise FileNotFoundError('wpd.json not found in archive')
        raw = tf.extractfile(wpd_member).read()
        data = json.loads(raw.decode('utf-8-sig'))
        image_member = next((m for m in members if Path(m.name).suffix.lower() in IMAGE_EXTS), None)
        image_bytes = tf.extractfile(image_member).read() if image_member else None
        image_name = Path(image_member.name).name if image_member else ''

    axes = []
    for a in data.get('axesColl', []) or []:
        cps = []
        for i, p in enumerate(a.get('calibrationPoints', []) or []):
            role = ['X1', 'X2', 'Y1', 'Y2'][i] if i < 4 else f'P{i+1}'
            cps.append(CalibrationPoint(role, _f(p.get('px')), _f(p.get('py')), p.get('dx'), p.get('dy'), p.get('dz')))
        axes.append({
            'name': a.get('name', ''),
            'type': a.get('type', ''),
            'is_log_x': bool(a.get('isLogX', False)),
            'is_log_y': bool(a.get('isLogY', False)),
            'no_rotation': bool(a.get('noRotation', False)),
            'calibration': cps,
        })

    datasets = []
    for ds in data.get('datasetColl', []) or []:
        points = []
        exported_values = []
        for p in ds.get('data', []) or []:
            x = _f(p.get('x')); y = _f(p.get('y'))
            if x is not None and y is not None:
                points.append((x, y))
            value = p.get('value')
            if isinstance(value, (list, tuple)) and len(value) >= 2:
                vx, vy = _f(value[0]), _f(value[1])
                exported_values.append([vx, vy] if vx is not None and vy is not None else None)
            else:
                exported_values.append(None)
        datasets.append({'name': ds.get('name', ''), 'axes_name': ds.get('axesName', ''), 'points': points,
                         'exported_values': exported_values, 'raw_point_count': len(ds.get('data', []) or [])})
    return {'version': data.get('version'), 'axes': axes, 'datasets': datasets, 'image_name': image_name, 'image_bytes': image_bytes}



def _f(v):
    try:
        return float(v)
    except Exception:
        return None



def font_default(size=14):
    candidates = [
        r'C:\Windows\Fonts\arial.ttf',
        r'C:\Windows\Fonts\calibri.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    ]
    for p in candidates:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()



def draw_label(draw: ImageDraw.ImageDraw, xy, text, fill=(180, 0, 180), font=None):
    font = font or font_default(14)
    x, y = xy
    bbox = draw.textbbox((x, y), text, font=font)
    pad = 3
    draw.rectangle((bbox[0]-pad, bbox[1]-pad, bbox[2]+pad, bbox[3]+pad), fill=(255,255,255), outline=fill)
    draw.text((x, y), text, fill=fill, font=font)



def make_calibration_visual(image_bytes: bytes, axes: list[dict], out_path: Path):
    """Draw recorded calibration centres with visible labels and an external legend."""
    from wpd_evidence import draw_calibration
    draw_calibration(image_bytes, axes, out_path)


def make_extraction_visual(image_bytes: bytes, datasets: list[dict], out_path: Path):
    """Draw raw WPD points with distinct outlined dots and no connecting lines."""
    from wpd_evidence import draw_extraction
    draw_extraction(image_bytes, datasets, out_path)


def edge_distance_metrics(image_bytes, points):
    """Diagnostic distance to any visible ink, never proof of curve identity."""
    if not points:
        return None, None
    image = np.asarray(Image.open(io.BytesIO(image_bytes)).convert("L"))
    mask = image < 215
    try:
        from scipy.ndimage import distance_transform_edt
        distance = distance_transform_edt(~mask)
        values = []
        for x, y in points:
            ix, iy = int(round(x)), int(round(y))
            if 0 <= iy < image.shape[0] and 0 <= ix < image.shape[1]:
                values.append(float(distance[iy, ix]))
        if values:
            return float(np.median(values)), float(np.percentile(values, 95))
    except ImportError:
        pass
    return None, None


def verify_project(tar_path, out_dir):
    from wpd_level1 import verify_project_impl
    return verify_project_impl(tar_path, out_dir, sys.modules[__name__])


def main():
    from wpd_runner import main as run
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
