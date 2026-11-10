"""Independent exported-curve checks with explicit evidence limits."""
from __future__ import annotations
import math
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from wpd_inventory import classify_family, read_csv_record
from wpd_mapping import detect_plot_rect_and_ticks, data_to_pixel


def _mapping_text(fit):
    if not fit:
        return "Not established from image evidence"
    coef = fit.get("coef")
    description = f"{fit.get('kind', 'unknown')} with {fit.get('n', 0)} accepted ticks"
    if coef:
        left = "log10(value)" if fit.get("kind") == "log" else "value"
        description += f"; {left} = {coef[0]:.9g} x pixel + {coef[1]:.9g}"
    error = fit.get("pixel_error", fit.get("fit_error"))
    if error is not None:
        description += f"; fit residual {float(error):.3g} px"
    return description


def mapping_visual(source, cal, destination, api):
    """Mark observed anchors without obscuring tick labels or adding false lines."""
    image = Image.open(source).convert("RGB")
    draw = ImageDraw.Draw(image)
    rectangle = cal.get("rect")
    if rectangle:
        draw.rectangle(tuple(rectangle), outline=(110, 110, 110), width=1)
        xl, yt, xr, yb = rectangle
        for key, color in (("x", (200, 30, 30)), ("y", (30, 80, 200))):
            for index, item in enumerate(cal.get(key + "_evidence", []), 1):
                position = item.get("pos")
                if position is None:
                    continue
                px, py = (float(position), yb) if key == "x" else (xl, float(position))
                color_here = color if item.get("used") else (140, 140, 140)
                draw.ellipse((px-2, py-2, px+2, py+2), outline=color_here, width=1)
    origin = cal.get("origin_px")
    if origin:
        x, y = origin
        if 0 <= x < image.width and 0 <= y < image.height:
            draw.ellipse((x-4, y-4, x+4, y+4), outline=(0, 85, 220), width=2)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination)
    return str(destination)


def verify_csv(source, csv_path, destination, api, source_note="", preview_mapping=None):
    """Check CSV content even when independent source-image evidence is absent."""
    result = api.Level2Result(csv_file=Path(csv_path).name, family=classify_family(Path(csv_path).name), source_image=Path(source).name if source is not None else "")
    result.source_visual = str(source) if source is not None else ""
    if source_note:
        result.comments.append(source_note)
    try:
        data = read_csv_record(csv_path)
    except OSError as error:
        result.comments.append(f"CSV could not be read: {error}. File access is unresolved; numeric content and source agreement remain REVIEW.")
        return result
    except Exception as error:
        result.trace_status = result.overall_status = "FAIL"
        result.comments.append(str(error))
        return result
    result.point_count = data["rows"]
    result.first_point = data["first_point"]
    result.last_point = data["last_point"]
    result.comments.append(f"Read {data['rows']} numeric rows, preserving the first numeric row at line {data['first_data_line']}. First point {data['first_point']}; last point {data['last_point']}.")
    malformed = bool(data["invalid_lines"] or data["extra_numeric_columns"])
    if data["invalid_lines"]:
        result.comments.append("Incomplete or nonnumeric CSV data at lines " + ", ".join(map(str, data["invalid_lines"][:12])) + ". These rows are flagged and no clean export is inferred.")
    if data["extra_numeric_columns"]:
        result.comments.append("More than two numeric columns are present; the intended X Y pair is ambiguous. Re-export one dataset per file.")
    x, y = data["x"], data["y"]
    result.x_order_status = "PASS" if np.all(np.diff(x) >= 0) else "REVIEW"
    if result.x_order_status == "REVIEW":
        result.comments.append("X values are not ascending. Inspect point order; this alone does not establish incorrect curve coordinates. Source data were not sorted.")
    if source is None:
        result.x_mapping = result.y_mapping = "Not established: no unambiguous standalone source image is selected"
        result.origin_kind = "not established"
        result.trace_status = "FAIL" if malformed else "REVIEW"
        result.overall_status = api.aggregate([result.axis_status, result.x_order_status, result.trace_status])
        result.comments.append(
            "CSV numeric content was checked independently of source availability. "
            "Source-axis mapping, curve identity and source-trace agreement remain REVIEW because no unambiguous standalone source image is selected."
        )
        return result
    try:
        return _verify_source_image(result, data, source, destination, api, malformed, preview_mapping)
    except Exception as error:
        # Source access/decoding and optional diagnostic rendering must not
        # discard the numeric checks or interrupt other CSVs in this device.
        # A completed malformed-row finding remains FAIL; an unavailable image
        # is evidence uncertainty, not proof of wrong digitization.
        result.trace_status = "FAIL" if malformed or result.invalid_projection_count else "REVIEW"
        result.overall_status = api.aggregate([result.axis_status, result.x_order_status, result.trace_status])
        result.comments.append(
            f"Source-image check could not be completed ({type(error).__name__}: {error}). "
            "Completed CSV numeric findings are retained. Unfinished source-image, "
            "curve-identity and trace checks require REVIEW; this image error alone "
            "does not establish wrong digitization. Other available CSVs are checked independently."
        )
        return result


def _verify_source_image(result, data, source, destination, api, malformed, preview_mapping=None):
    """Extend one CSV result with image evidence without owning batch control."""
    x, y = data["x"], data["y"]
    # There is intentionally no CSV-valued argument in this call.
    cal = detect_plot_rect_and_ticks(Path(source))
    result.axis_status = cal.get("status", "REVIEW")
    result.x_mapping = _mapping_text(cal.get("x"))
    result.y_mapping = _mapping_text(cal.get("y"))
    result.ocr_x_labels = list(cal.get("x_labels") or [])
    result.ocr_y_labels = list(cal.get("y_labels") or [])
    result.x_axis_evidence = list(cal.get("x_evidence") or [])
    result.y_axis_evidence = list(cal.get("y_evidence") or [])
    result.origin_px = cal.get("origin_px")
    result.origin_kind = cal.get("origin_kind", "not established")
    result.anchor_values = cal.get("anchor_values")
    if result.origin_px:
        result.comments.append(f"Blue marker: {result.origin_kind}; pixel coordinates {result.origin_px}; data coordinates {result.anchor_values}. The axis coefficients are not shifted to this marker.")
    diagnostic = Path(destination).with_name(Path(destination).stem + "__AXIS_DEBUG.png")
    result.axis_debug_visual = result.diagnostic_visual = mapping_visual(source, cal, diagnostic, api)
    if cal.get("reason"):
        result.comments.append(cal["reason"])
    if malformed:
        result.trace_status = "FAIL"
        result.overall_status = "FAIL"
        return result
    if result.axis_status != "PASS":
        result.trace_status = "REVIEW"
        result.overall_status = api.aggregate([result.axis_status, result.x_order_status, result.trace_status])
        if preview_mapping is not None:
            plotted = _draw_wpd_preview(source, data, destination, preview_mapping)
            if plotted:
                result.overlay_visual = str(destination)
                result.overlay_kind = "WPD_VISUAL_PREVIEW"
                result.comments.append(
                    "A separate source-image overlay was generated as a visual guide using the stored WPD calibration. "
                    "It is NOT an independent axis check and must remain REVIEW; compare its points and printed ticks manually."
                )
                return result
        result.comments.append(
            "The standalone graph was found, but its X/Y pixel mapping could not be established from image ticks. "
            "No trustworthy CSV overlay is available. Check the source tick labels and provide a readable graph image."
        )
        return result
    image = Image.open(source).convert("RGB")
    draw = ImageDraw.Draw(image)
    plot = cal.get("rect")
    points, outside, invalid = [], [], []
    point_radius = max(3.0, max(image.size) / 145)
    point_edge = max(1, round(point_radius / 4))
    point_halo = max(1, round(point_radius / 3))
    uncertainty = max(3.0, 3.0 * max(float(cal[k].get("pixel_error", 0)) for k in ("x", "y")))
    for index, (xv, yv) in enumerate(zip(x, y), 1):
        px, py = data_to_pixel(float(xv), cal["x"]), data_to_pixel(float(yv), cal["y"])
        if px is None or py is None or not (math.isfinite(px) and math.isfinite(py)):
            invalid.append(index)
            continue
        points.append((px, py))
        if plot and not (plot[0]-uncertainty <= px <= plot[2]+uncertainty and plot[1]-uncertainty <= py <= plot[3]+uncertainty):
            outside.append(index)
        if 0 <= px < image.width and 0 <= py < image.height:
            draw.ellipse((px-point_radius-point_halo, py-point_radius-point_halo,
                          px+point_radius+point_halo, py+point_radius+point_halo), fill=(255, 255, 255))
            draw.ellipse((px-point_radius, py-point_radius, px+point_radius, py+point_radius),
                         fill=(174, 0, 35), outline=(25, 31, 40), width=point_edge)
    result.invalid_projection_count = len(invalid)
    result.outside_plot_count = len(outside)
    # Record completed projection findings before saving optional evidence or
    # computing ink distances, so a later diagnostic error cannot hide them.
    result.trace_status = "FAIL" if invalid else "REVIEW"
    if invalid:
        result.comments.append(f"{len(invalid)} CSV points cannot be mapped on the independently confirmed axis scales, including numeric rows {invalid[:10]}. Check nonpositive values on log axes.")
    if outside:
        result.comments.append(f"{len(outside)} of {len(x)} CSV points lie outside the independently detected plot region by more than {uncertainty:.2f} px, including numeric rows {outside[:10]}. Check calibration, units, and crop completeness.")
    if result.origin_px:
        ox, oy = result.origin_px
        if 0 <= ox < image.width and 0 <= oy < image.height:
            draw.ellipse((ox-4, oy-4, ox+4, oy+4), outline=(0, 85, 220), width=2)
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    image.save(destination)
    result.overlay_visual = str(destination)
    result.overlay_kind = "INDEPENDENT_AXES"
    median, p95 = api.edge_distance_metrics(Path(source).read_bytes(), points)
    result.median_error_px, result.p95_error_px = median, p95
    if p95 is not None:
        result.comments.append(f"Distance to any visible source ink: median {median:.2f} px, 95th percentile {p95:.2f} px. Gridlines, labels, and other curves can also produce small distances.")
    result.comments.append("Full curve identity and coverage need visual review against the legend and source trace. Ink proximity and the number of samples do not establish PASS.")
    result.overall_status = api.aggregate([result.axis_status, result.x_order_status, result.trace_status])
    return result


def _draw_wpd_preview(source, data, destination, fits):
    """Draw each CSV over a pixel-identical graph; grade remains REVIEW.

    The red banner is saved outside the original figure. It makes this
    non-independent visual aid unmistakable even when viewed outside Word.
    """
    base = Image.open(source).convert("RGB")
    image = Image.new("RGB", (base.width, base.height+48), (108, 18, 28))
    image.paste(base, (0, 48))
    draw = ImageDraw.Draw(image)
    draw.text((9, 5), "VISUAL PREVIEW ONLY", fill=(255, 255, 255), font=ImageFont.load_default())
    draw.text((9, 23), "NOT independent (WPD axes)", fill=(255, 255, 255), font=ImageFont.load_default())
    radius = max(2.5, max(base.size)/150)
    placed = 0
    for xv, yv in zip(data["x"], data["y"]):
        px, py = data_to_pixel(float(xv), fits["x"]), data_to_pixel(float(yv), fits["y"])
        if px is None or py is None or not (math.isfinite(px) and math.isfinite(py)):
            continue
        py += 48
        if 0 <= px < image.width and 48 <= py < image.height:
            draw.ellipse((px-radius-1, py-radius-1, px+radius+1, py+radius+1), fill="white")
            draw.ellipse((px-radius, py-radius, px+radius, py+radius), fill=(190, 0, 35), outline=(25, 31, 40), width=1)
            placed += 1
    if not placed:
        return False
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination)
    return True
