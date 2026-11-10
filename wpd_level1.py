"""Evidence-based Level 1 checks for a WebPlotDigitizer project.

This module deliberately distinguishes a confirmed calibration error from a
check which still needs visual evidence.  Generic image ink, dataset names and
the number of samples cannot establish correct curve identity or coverage.

The original application supplies its result classes and rendering/parser
helpers through ``api``.  Source-axis recognition is independent of WPD values
and CSV exports, and is shared with the Level 2 mapping module.
"""
from __future__ import annotations

import io
import json
import math
import re
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image


def _aggregate(values):
    values = list(values)
    if "FAIL" in values:
        return "FAIL"
    if not values or "REVIEW" in values:
        return "REVIEW"
    return "PASS"


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _equal_value(first, second):
    """Compare the stored number to the printed number, not to an axis span."""
    return math.isclose(first, second, rel_tol=1e-8, abs_tol=1e-10)


def _fit_is_confirmed(fit):
    # New mapping explicitly records evidence sufficiency.  Do not upgrade a
    # legacy two-label fit, or a fit chosen by guessing between scale modes.
    return bool(
        fit
        and fit.get("status") == "PASS"
        and fit.get("scale_status", fit.get("status")) == "PASS"
        and fit.get("kind") in {"linear", "log"}
        and len(fit.get("points", [])) >= 3
    )


def _source_ticks(fit):
    ticks = []
    for entry in (fit or {}).get("points", []):
        if len(entry) < 2:
            continue
        pixel, value = _number(entry[0]), _number(entry[1])
        if pixel is not None and value is not None:
            ticks.append((pixel, value))
    return ticks


def _pixel_value(pixel, fit):
    try:
        a, b = fit["coef"]
        transformed = float(a) * pixel + float(b)
        return _number(10.0 ** transformed if fit["kind"] == "log" else transformed)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _value_pixel(value, fit):
    try:
        a, b = (float(v) for v in fit["coef"])
        if abs(a) < 1e-15 or (fit["kind"] == "log" and value <= 0):
            return None
        target = math.log10(value) if fit["kind"] == "log" else value
        return _number((target - b) / a)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _marker_tolerances(fit):
    """Small pixel uncertainty, never a percentage of the whole value range.

    These are explicit screening tolerances, not a claim that an image resolves
    an exact subpixel tick position.  A large fit residual blocks confirmation
    in the independent mapper instead of increasing this tolerance indefinitely.
    """
    residual = _number((fit or {}).get("pixel_error")) or 0.0
    aligned = min(3.0, max(1.5, 1.0 + 2.0 * residual))
    displaced = max(5.0, 2.5 * aligned)
    return aligned, displaced


def audit_calibration(axis, source_cal, image_size=None):
    """Audit one axes definition against independently observed tick positions.

    X markers are compared by X coordinate and Y markers by Y coordinate.
    Valid calibration references may be on interior grid lines; neither pair
    is required to sit on the lower or left frame border.
    """
    source_cal = source_cal or {}
    result = {
        "scale_status": "REVIEW", "marker_geometry_status": "REVIEW",
        "marker_value_status": "REVIEW", "checks": [], "comments": [],
    }
    scales = []
    for dim, key in (("X", "is_log_x"), ("Y", "is_log_y")):
        stored = "log" if axis.get(key) else "linear"
        fit = source_cal.get(dim.lower())
        if axis.get("_scale_unknown_" + dim.lower()):
            status = "REVIEW"
            result["comments"].append(f"{dim} scale flag is absent or has an unsupported representation in the stored JSON.")
        elif _fit_is_confirmed(fit):
            status = "PASS" if stored == fit["kind"] else "FAIL"
            if status == "FAIL":
                result["comments"].append(
                    f"{dim} scale is {stored} in WPD; independently observed source ticks "
                    f"confirm a {fit['kind']} axis."
                )
        else:
            status = "REVIEW"
            result["comments"].append(
                f"{dim} linear/log setting could not be independently confirmed from source ticks."
            )
        scales.append(status)
    result["scale_status"] = _aggregate(scales)

    cps = list(axis.get("calibration", []))
    by_role = {getattr(cp, "role", ""): cp for cp in cps}
    geometry, values = [], []
    for role in ("X1", "X2", "Y1", "Y2"):
        cp = by_role.get(role)
        check = {
            "axis_name": axis.get("name", ""), "role": role,
            "px": _number(cp.px) if cp is not None else None,
            "py": _number(cp.py) if cp is not None else None,
            "assigned": None, "independent_value": None,
            "geometry_error_px": None, "value_error": None,
            "geometry_status": "REVIEW", "value_status": "REVIEW",
            "status": "REVIEW", "comment": "",
        }
        if cp is None:
            check.update(geometry_status="FAIL", value_status="FAIL", comment="Required calibration marker is missing.")
        else:
            x, y = _number(cp.px), _number(cp.py)
            assigned = _number(cp.displayed_value)
            check["assigned"] = assigned
            dim = "x" if role.startswith("X") else "y"
            fit = source_cal.get(dim)
            pixel = x if dim == "x" else y
            if x is None or y is None:
                check.update(geometry_status="FAIL", comment="Marker pixel coordinates are missing or non-finite.")
            elif image_size and not (0 <= x < image_size[0] and 0 <= y < image_size[1]):
                check.update(geometry_status="FAIL", comment="Marker is outside the embedded source image.")
            if assigned is None:
                check["value_status"] = "FAIL"
                check["comment"] += " Stored calibration value is missing, non-numeric or non-finite."
            elif axis.get("is_log_" + dim) and not axis.get("_scale_unknown_" + dim) and assigned <= 0:
                check["value_status"] = "FAIL"
                check["comment"] += " A logarithmic WPD axis has a non-positive calibration value."

            if pixel is not None and assigned is not None and _fit_is_confirmed(fit):
                ticks = _source_ticks(fit)
                tolerance, fail_tolerance = _marker_tolerances(fit)
                nearest = min(ticks, key=lambda t: abs(pixel - t[0])) if ticks else None
                matching = [t for t in ticks if _equal_value(t[1], assigned)]
                target = min(matching, key=lambda t: abs(pixel - t[0])) if matching else None
                check["alignment_tolerance_px"] = tolerance
                check["independent_value"] = _pixel_value(pixel, fit)

                if nearest and abs(pixel - nearest[0]) <= tolerance:
                    tick_pixel, tick_value = nearest
                    check["geometry_error_px"] = abs(pixel - tick_pixel)
                    check["source_tick_pixel"] = tick_pixel
                    check["independent_value"] = tick_value
                    check["value_error"] = abs(assigned - tick_value)
                    if check["geometry_status"] != "FAIL":
                        check["geometry_status"] = "PASS"
                    if check["value_status"] != "FAIL":
                        if _equal_value(assigned, tick_value):
                            check["value_status"] = "PASS"
                        else:
                            projected = _value_pixel(assigned, fit)
                            separation = abs(projected - tick_pixel) if projected is not None else None
                            check["value_disagreement_px"] = separation
                            check["value_uncertainty_px"] = 2.0 * tolerance
                            if separation is not None and separation <= 2.0 * tolerance:
                                check["value_status"] = "REVIEW"
                                check["comment"] += (
                                    f" The stored/read numbers disagree, but their projected separation ({separation:.2f} px) "
                                    f"is within the {2.0*tolerance:.2f} px source-reading uncertainty. "
                                    "Confirm the printed digits and stored number visually; this is not a PASS or a confirmed calibration error."
                                )
                            else:
                                check["value_status"] = "FAIL"
                    check["comment"] += (
                        f" Marker is {abs(pixel-tick_pixel):.2f} px from an independently observed "
                        f"{dim.upper()} tick/grid coordinate. OCR-read tick value={tick_value:g}; stored value={assigned:g}."
                    )
                elif target:
                    error = abs(pixel - target[0])
                    check["geometry_error_px"] = error
                    check["source_tick_pixel"] = target[0]
                    if check["geometry_status"] != "FAIL":
                        check["geometry_status"] = "FAIL" if error > fail_tolerance else "REVIEW"
                    check["comment"] += (
                        f" Stored value {assigned:g} appears on the source axis at pixel {target[0]:.2f}; "
                        f"marker coordinate is {pixel:.2f} ({error:.2f} px away). "
                        "The clicked position is not on a confidently matched labelled tick."
                    )
                else:
                    # Interpolation is useful evidence, but a fitted number is
                    # not an observed printed tick.  Do not turn it into a
                    # numeric calibration PASS or invent an OCR digit.
                    projected = _value_pixel(assigned, fit)
                    if projected is not None:
                        check["geometry_error_px"] = abs(pixel - projected)
                    check["comment"] += (
                        "No confidently read tick matches this marker/value pair. "
                        "An interpolated axis estimate is shown only for review; inspect the intended minor tick/grid line."
                    )
            elif not check["comment"]:
                check["comment"] = "Independent tick evidence is insufficient to confirm marker placement and its assigned value."
            rect = source_cal.get("rect")
            if x is not None and y is not None and rect and source_cal.get("rect_status") == "PASS":
                left, top, right, bottom = (float(v) for v in rect)
                orthogonal = y if dim == "x" else x
                lower, upper = (top, bottom) if dim == "x" else (left, right)
                if orthogonal < lower - 4.0 or orthogonal > upper + 4.0:
                    check["geometry_status"] = _aggregate([check["geometry_status"], "REVIEW"])
                    check["comment"] += (
                        " The marker is outside the independently detected plot frame in its orthogonal direction; "
                        "confirm that it represents an intended tick/grid reference rather than label text."
                    )
        check["comment"] = check["comment"].strip()
        check["status"] = _aggregate([check["geometry_status"], check["value_status"]])
        geometry.append(check["geometry_status"])
        values.append(check["value_status"])
        result["checks"].append(check)
        if check["status"] != "PASS":
            result["comments"].append(f"{role}: {check['comment']}")

    # Degenerate scales are invalid independently of OCR.  Distinct points are
    # necessary, but their number alone does not establish the intended axes.
    for dim in ("X", "Y"):
        first, last = by_role.get(dim + "1"), by_role.get(dim + "2")
        if first is None or last is None:
            continue
        first_value, last_value = _number(first.displayed_value), _number(last.displayed_value)
        if first_value is not None and last_value is not None and _equal_value(first_value, last_value):
            values.append("FAIL")
            result["comments"].append(f"{dim} calibration has identical endpoint values and cannot define a scale.")
        first_px = _number(first.px if dim == "X" else first.py)
        last_px = _number(last.px if dim == "X" else last.py)
        if first_px is not None and last_px is not None and abs(first_px - last_px) < 1e-6:
            geometry.append("FAIL")
            result["comments"].append(f"{dim} calibration endpoints have the same calibrated pixel coordinate.")
        # A tilted pair can change WPD's rotation transform.  It is not enough
        # to match independent X/Y projections; flag that additional evidence.
        orth_first = _number(first.py if dim == "X" else first.px)
        orth_last = _number(last.py if dim == "X" else last.px)
        if not axis.get("no_rotation") and orth_first is not None and orth_last is not None:
            upright = bool(source_cal.get("rect") and source_cal.get("rect_status") == "PASS")
            # Each reference may lie on the same interior grid line.  It is
            # the pair's direction, not proximity to a frame border, that can
            # introduce unintended rotation.  Three pixels is an explicit
            # conservative raster/placement screening allowance, not a proof
            # that a smaller tilt has no numerical effect.
            orientation_allowance = 3.0 if upright else 4.0
            offset = abs(orth_first - orth_last)
            if offset > orientation_allowance:
                geometry.append("REVIEW")
                comment = (
                    f"{dim} calibration pair differs by {offset:.2f} px in its orthogonal direction, "
                    f"exceeding the {orientation_allowance:.1f} px orientation screening allowance. "
                    + ("The independently detected source frame is upright. " if upright else "")
                    + "With WPD rotation enabled, confirm that this tilt is intended; correct paired marker alignment if it is accidental."
                )
                result["comments"].append(comment)
                for check in result["checks"]:
                    if check["role"].startswith(dim):
                        check["orientation_error_px"] = offset
                        check["orientation_tolerance_px"] = orientation_allowance
                        check["orientation_status"] = "REVIEW"
                        check["geometry_status"] = _aggregate([check["geometry_status"], "REVIEW"])
                        check["status"] = _aggregate([check["geometry_status"], check["value_status"]])
                        check["comment"] += " " + comment
    result["marker_geometry_status"] = _aggregate(geometry)
    result["marker_value_status"] = _aggregate(values)
    return result


def _archive_schema(tar_path, image_exts):
    """Retain schema evidence which the legacy convenience parser normalizes."""
    with tarfile.open(tar_path, "r:*") as archive:
        members = [member for member in archive.getmembers() if member.isfile()]
        jsons = [member for member in members if Path(member.name).name.lower() == "wpd.json"]
        images = [member for member in members if Path(member.name).suffix.lower() in image_exts]
        data = json.loads(archive.extractfile(jsons[0]).read().decode("utf-8-sig")) if jsons else {}
    return data, len(jsons), len(images)


def verify_project_impl(tar_path, out_dir, api):
    """Return the application's ProjectResult without modifying any input file."""
    tar_path, out_dir = Path(tar_path), Path(out_dir)
    family = api.classify_family(tar_path.name)
    pr = api.ProjectResult(project=tar_path.name, family=family)
    from wpd_inventory import graph_family_warning
    physical_warning = graph_family_warning(tar_path.name, family)
    if physical_warning:
        pr.comments.append(physical_warning)
    try:
        wp = api.read_tar_project(tar_path)
        raw_schema, json_count, image_count = _archive_schema(tar_path, api.IMAGE_EXTS)
    except Exception as exc:
        pr.comments.append(f"Cannot read the required WPD project: {exc}")
        pr.graph_type_status = pr.calibration_status = pr.dataset_status = "FAIL"
        pr.trace_status = pr.completeness_status = pr.overall_status = "FAIL"
        return pr

    axes = list(wp.get("axes", []))
    raw_datasets = list(wp.get("datasets", []))
    pr.image_name = wp.get("image_name", "")
    image_bytes = wp.get("image_bytes")
    image_size = None
    if image_bytes:
        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                image_size = image.size
                image.verify()
        except Exception as exc:
            pr.comments.append(f"Embedded source image cannot be decoded: {exc}")
            image_bytes = None
    if not image_bytes:
        pr.comments.append("Embedded source-image evidence is unavailable; calibration and visual curve correctness require review.")

    if axes:
        pr.graph_type = "; ".join(str(axis.get("type", "")) for axis in axes)
        pr.x_scale = "log" if axes[0].get("is_log_x") else "linear"
        pr.y_scale = "log" if axes[0].get("is_log_y") else "linear"
        pr.calibration = list(axes[0].get("calibration", []))
        pr.graph_type_status = "PASS" if all(str(axis.get("type", "")).upper() in {"XYAXES", "XY", "2D XY"} for axis in axes) else "FAIL"
        if pr.graph_type_status == "FAIL":
            pr.comments.append(f"Unsupported or incorrect graph type for CC/OC/TC XY verification: {pr.graph_type}.")
        else:
            pr.comments.append("XY graph type confirms the coordinate-system setting only; physical axis quantities, units and graph-family identity still require source confirmation.")
    else:
        pr.graph_type_status = "FAIL"
        pr.comments.append("No WPD axes definition was found.")

    source_cal = {}
    ambiguous_source = json_count != 1 or image_count > 1
    if ambiguous_source:
        pr.comments.append(
            f"Archive contains {json_count} wpd.json files and {image_count} images; "
            "the correct project/image association must be confirmed before an automatic source comparison."
        )
    unassociated_axes = len(axes) > 1
    if unassociated_axes:
        pr.comments.append(
            "MULTIPLE_AXES_SOURCE_ASSOCIATION_UNCONFIRMED: the project contains multiple axes definitions, "
            "but their individual source panels or tick sets have not been independently associated. "
            "A single image mapping is not reused for all axes. Source-based calibration and scale checks remain REVIEW; "
            "missing or invalid stored calibration fields are still checked separately."
        )
    if image_bytes:
        # Exclude invalid coordinates from drawing only, and retain their FAIL
        # in the audit.  No source project or stored coordinate is changed.
        visual_axes = [dict(axis, calibration=[cp for cp in axis.get("calibration", []) if _number(cp.px) is not None and _number(cp.py) is not None]) for axis in axes]
        visual_datasets = [dict(ds, points=[p for p in ds.get("points", []) if len(p) >= 2 and _number(p[0]) is not None and _number(p[1]) is not None]) for ds in raw_datasets]
        try:
            cal_path = out_dir / "level1" / f"{tar_path.stem}__calibration.png"
            ext_path = out_dir / "level1" / f"{tar_path.stem}__extraction.png"
            api.make_calibration_visual(image_bytes, visual_axes, cal_path)
            api.make_extraction_visual(image_bytes, visual_datasets, ext_path)
            pr.calibration_visual, pr.extraction_visual = str(cal_path), str(ext_path)
        except Exception as exc:
            pr.comments.append(f"Visual evidence could not be rendered: {exc}")
        if not ambiguous_source and not unassociated_axes:
            try:
                from wpd_mapping import detect_plot_rect_and_ticks
                with tempfile.TemporaryDirectory() as temp_dir:
                    image_path = Path(temp_dir) / ("embedded" + (Path(pr.image_name).suffix or ".png"))
                    image_path.write_bytes(image_bytes)
                    source_cal = detect_plot_rect_and_ticks(image_path, csv_y=None)
            except Exception as exc:
                pr.comments.append(f"Independent source-axis recognition could not run: {exc}")

    summaries = []
    for dim in ("x", "y"):
        fit = (source_cal or {}).get(dim)
        if _fit_is_confirmed(fit):
            summaries.append(f"{dim.upper()}={fit['kind']}, {len(fit.get('points', []))} observed ticks")
        else:
            summaries.append(f"{dim.upper()}=unconfirmed")
    pr.source_axis_summary = "Independent embedded-image evidence: " + "; ".join(summaries)
    scale_statuses, geometry_statuses, value_statuses = [], [], []
    for index, axis in enumerate(axes):
        raw_axes = raw_schema.get("axesColl", []) or []
        raw_axis = raw_axes[index] if index < len(raw_axes) else {}
        axis_for_audit = dict(axis)
        for key, dim in (("isLogX", "x"), ("isLogY", "y")):
            if not isinstance(raw_axis.get(key), bool):
                axis_for_audit["_scale_unknown_" + dim] = True
        audit = audit_calibration(axis_for_audit, source_cal, image_size)
        # Report the original TAR flags separately from independent source
        # evidence. Absent/nonboolean flags must not be displayed as linear
        # merely because the compatibility parser supplies a false default.
        pr.axis_settings.append({
            "name": str(axis.get("name", "") or f"Axis {index + 1}"),
            "type": str(axis.get("type", "")),
            "is_log_x": raw_axis.get("isLogX") if isinstance(raw_axis.get("isLogX"), bool) else None,
            "is_log_y": raw_axis.get("isLogY") if isinstance(raw_axis.get("isLogY"), bool) else None,
            "source_x_scale": source_cal["x"]["kind"] if _fit_is_confirmed(source_cal.get("x")) else None,
            "source_y_scale": source_cal["y"]["kind"] if _fit_is_confirmed(source_cal.get("y")) else None,
            "scale_status": audit["scale_status"],
        })
        scale_statuses.append(audit["scale_status"])
        geometry_statuses.append(audit["marker_geometry_status"])
        value_statuses.append(audit["marker_value_status"])
        pr.calibration_checks.extend(audit["checks"])
        prefix = f"Axes {axis.get('name', index+1)}: " if len(axes) > 1 else ""
        pr.comments.extend(prefix + comment for comment in audit["comments"])
    pr.scale_status = _aggregate(scale_statuses)
    pr.marker_geometry_status = _aggregate(geometry_statuses) if axes else "FAIL"
    pr.marker_value_status = _aggregate(value_statuses) if axes else "FAIL"
    pr.calibration_status = _aggregate([pr.scale_status, pr.marker_geometry_status, pr.marker_value_status])

    # A label is a claim made by the digitizer; it is not independent proof that
    # the points follow that labelled physical curve.
    axis_names = {axis.get("name", "") for axis in axes}
    pr.dataset_status = "REVIEW" if raw_datasets else "FAIL"
    if not raw_datasets:
        pr.comments.append("No WPD datasets were found.")
    else:
        pr.comments.append("Dataset-to-source-curve identity is not automatically established by dataset names; inspect every labelled overlay.")
        names = [str(ds.get("name", "")).upper() for ds in raw_datasets]
        if family == "CC":
            for expected in ("COSS", "CRSS"):
                if not any(expected in name for name in names):
                    pr.comments.append(
                        f"No WPD dataset is clearly labelled {expected}. Confirm whether the corresponding source curve "
                        "is unavailable, missing from extraction, or present under a different dataset name."
                    )
        if len(set(names)) != len(names):
            pr.comments.append("Dataset names are duplicated; confirm each dataset's physical curve and exported-file association.")
    trace_statuses, completeness_statuses = [], []
    schema_datasets = raw_schema.get("datasetColl", []) or []
    populated_peer = any(
        ds.get("points") and (api.detect_temperature(ds.get("name", "")) or 0) >= 0
        for ds in raw_datasets
    )
    for ds_index, ds in enumerate(raw_datasets):
        name = str(ds.get("name", "") or "(unnamed dataset)")
        if (api.detect_temperature(name) or 0) < 0:
            pr.comments.append(f"{name}: negative-temperature dataset excluded from the requested trace/export checks.")
            continue
        dataset_family_warning = graph_family_warning(name, family)
        if dataset_family_warning and dataset_family_warning != physical_warning:
            pr.comments.append(f"{name}: {dataset_family_warning}")
        points = list(ds.get("points", []))
        finite_points = [p for p in points if len(p) >= 2 and _number(p[0]) is not None and _number(p[1]) is not None]
        schema_ds = schema_datasets[ds_index] if ds_index < len(schema_datasets) else {}
        recorded_points = list(schema_ds.get("data", []) or [])
        invalid_recorded = sum(not isinstance(point, dict) or _number(point.get("x")) is None or _number(point.get("y")) is None for point in recorded_points)
        raw_values = ds.get("exported_values")
        if raw_values is None:
            raw_values = [point.get("value") if isinstance(point, dict) else None for point in recorded_points]
        exported_values = []
        for value in raw_values:
            pair = [_number(value[0]), _number(value[1])] if isinstance(value, (list, tuple)) and len(value) >= 2 else [None, None]
            exported_values.append(pair if all(v is not None for v in pair) else None)
        reasons = []
        trace_status = "REVIEW"
        completeness = "REVIEW"
        if not points:
            placeholder = bool(re.fullmatch(r"\s*Default[\s_]+Dataset(?:[\s_]+\d+)?\s*", name, re.IGNORECASE))
            if populated_peer:
                trace_status = completeness = "REVIEW"
                if placeholder:
                    reasons.append("EMPTY_DEFAULT_DATASET: an empty default placeholder accompanies populated curves. Confirm it is unused; it is not treated as a missing required curve.")
                else:
                    reasons.append("EMPTY_DATASET_WITH_POPULATED_PEERS: this dataset has no points, but other datasets are populated. Confirm whether it is an unused extra or its intended curve is under another name; missing physical extraction is not established by the empty label alone.")
            else:
                trace_status = completeness = "FAIL"
                reasons.append("Dataset has no usable digitized points. A lone empty project or an empty named curve is incomplete; this does not by itself establish source-graph availability.")
        elif len(finite_points) != len(points) or invalid_recorded:
            trace_status = "FAIL"
            reasons.append("Dataset contains missing, non-numeric or non-finite point coordinates; invalid records are not silently discarded from the audit.")
        outside = 0
        if image_size:
            outside = sum(not (0 <= x < image_size[0] and 0 <= y < image_size[1]) for x, y in finite_points)
            if outside:
                trace_status = "FAIL"
                reasons.append(f"{outside} point(s) lie outside the embedded source image.")
        if len({(float(p[0]), float(p[1])) for p in finite_points}) < 2 and points:
            completeness = "FAIL"
            reasons.append("Fewer than two distinct finite points cannot represent a curve.")
        elif points:
            reasons.append(f"{len(points)} points are present; the count does not prove full curve coverage or adequate knee sampling.")
        if ds.get("axes_name") and ds.get("axes_name") not in axis_names:
            pr.dataset_status = "FAIL"
            reasons.append(f"Dataset references undefined axes {ds.get('axes_name')!r}.")
        elif not ds.get("axes_name") and len(axes) > 1:
            reasons.append("Dataset does not identify which of the multiple axes definitions it uses.")
        if family == "OC" and api.detect_vgs(name) is None:
            reasons.append("Gate voltage is not clearly identified in the dataset name; confirm VGS against the source trace label.")
        if family == "TC" and api.detect_temperature(name) is None:
            reasons.append("Temperature is not explicit in the dataset name; confirm it from the source figure or trace legend.")

        med = p95 = None
        if image_bytes and finite_points:
            try:
                med, p95 = api.edge_distance_metrics(image_bytes, finite_points)
            except Exception:
                pass
        if p95 is not None:
            reasons.append(f"Generic-ink proximity p95={p95:.2f} px is diagnostic only: grid lines, text and other curves can also match.")
        reasons.append("Correct trace identity, every-point alignment and the full useful source range still require visual confirmation.")
        pr.datasets.append({
            "name": name, "points": len(recorded_points) if recorded_points else len(points), "trace_status": trace_status,
            "median_error_px": med, "p95_error_px": p95,
            "completeness_status": completeness, "mapping_status": pr.dataset_status,
            "outside_image_points": outside, "invalid_point_records": invalid_recorded,
            "finite_points": len(finite_points), "comments": reasons,
            "exported_values": exported_values,
            "exported_value_count": sum(value is not None for value in exported_values),
        })
        trace_statuses.append(trace_status)
        completeness_statuses.append(completeness)
        pr.comments.extend(f"{name}: {reason}" for reason in reasons if "Generic-ink" not in reason)
    pr.trace_status = _aggregate(trace_statuses) if raw_datasets else "FAIL"
    pr.completeness_status = _aggregate(completeness_statuses) if raw_datasets else "FAIL"
    pr.overall_status = _aggregate([
        pr.graph_type_status, pr.calibration_status, pr.dataset_status,
        pr.trace_status, pr.completeness_status,
    ])
    return pr
