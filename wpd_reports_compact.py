"""Concise Word reports using recorded WPD verification findings.

This presentation layer never runs a check, changes a verdict, or edits source
files. The full reporter supplies document/layout helpers through lazy imports;
the public writers in that module are deliberately not called here.
"""
from __future__ import annotations

from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any, Iterable


FAMILIES = ("CC", "OC", "TC")
_ROUTINE_SOURCE_NOTES = {
    "Availability fields describe files found in this device folder.",
    "A missing graph image does not prove the manufacturer omitted the graph. "
    "Graph presence or absence in the complete datasheet has not been established by this folder audit.",
    "PASS FAIL REVIEW applies to recorded checks. Unresolved curve identity and full-trace coverage remain REVIEW.",
}


def _helpers():
    # The existing module dispatches to this module, so keep the import lazy.
    import wpd_reports
    return wpd_reports


def _source_notes(dr: Any) -> list[str]:
    r = _helpers()
    notes = r._unique(
        str(note) for note in (r._get(dr, "source_notes", []) or [])
        if str(note).strip() not in _ROUTINE_SOURCE_NOTES
    )
    snapshot = str(r._get(dr, "source_snapshot", "") or "").strip()
    if snapshot and not any(snapshot in note for note in notes):
        notes.insert(0, "Source snapshot: " + snapshot)
    return notes


def _availability_text(value: Any) -> str:
    from wpd_report_findings import folder_presence
    return folder_presence(value)


def _header(doc: Any, dr: Any) -> None:
    r = _helpers()
    r._label(doc, "Device", r._get(dr, "device", ""), keep_next=True)
    r._label(doc, "Intern", r._get(dr, "intern", ""), small=True)
    folder = r._get(dr, "device_relpath", "") or r._get(dr, "path", "")
    if folder:
        r._label(doc, "Source folder", folder, small=True)
    for note in _source_notes(dr):
        r._p(doc, note, small=True)
    fatal = r._get(dr, "fatal_error", "")
    if fatal:
        r._label(doc, "Verification interrupted", fatal)
    r._table(
        doc, ["Overall", "CC", "OC", "TC"],
        [[r._status(dr, "overall_status"), r._status(dr, "cc_status"),
          r._status(dr, "oc_status"), r._status(dr, "tc_status")]],
        [1.775] * 4, 10, keep_together=True,
    )
    r._p(doc, "PASS: supported check. FAIL: requires correction. REVIEW: needs confirmation.", small=True)


def _availability(doc: Any, dr: Any) -> None:
    r = _helpers()
    doc.add_heading("Source and file availability", 1)
    available = r._get(dr, "availability", {}) or {}
    datasheets = r._get(dr, "datasheets", []) or []
    sheet_state = available.get("DATASHEET")
    if sheet_state is None and datasheets:
        sheet_state = "PRESENT"
    sheet_detail = _availability_text(sheet_state)
    if datasheets:
        sheet_detail += " — " + "; ".join(str(value) for value in datasheets)
    r._label(doc, "Datasheet PDF", sheet_detail, small=True)
    rows = [[family,
             _availability_text(available.get(family + "_IMAGE")),
             _availability_text(available.get(family + "_TAR")),
             _availability_text(available.get(family + "_CSV"))]
            for family in FAMILIES]
    r._table(doc, ["Family", "Source image", "WPD TAR", "CSV"],
             rows, [.7, 2.25, 2.1, 2.05], 9, keep_together=True)
    r._p(doc, "Present/Absent refers to files identified in this device folder.", small=True)


def _findings(doc: Any, findings: list[dict]) -> None:
    r = _helpers()
    doc.add_heading("Actions and findings", 1)
    if not findings:
        r._p(doc, "File and dataset checks are listed below; no other action was recorded.")
        return
    for finding in findings:
        status = str(finding.get("status") or "REVIEW")
        title = str(finding.get("title") or "Recorded finding")
        family = str(finding.get("family") or "").strip()
        status_label = status + (" " + family if family and family.casefold() != "general" else "")
        detail = str(finding.get("detail") or "").strip()
        action = str(finding.get("action") or "").strip()
        raw_files = finding.get("files") or []
        if isinstance(raw_files, str):
            raw_files = [raw_files]
        files = r._unique(raw_files)
        # File references already present in the prose need no second listing.
        prose = " ".join((title, detail, action))
        extra_files = [name for name in files if name not in prose]
        p = doc.add_paragraph()
        p.paragraph_format.keep_with_next = bool(action or extra_files)
        p.paragraph_format.space_after = r.Pt(3)
        r._set_font(p.add_run(status_label + " — " + title), 10.5, True)
        if detail:
            r._set_font(p.add_run(". " + detail), 10.5)
        if action:
            p = r._label(doc, "Action", action)
            p.paragraph_format.keep_with_next = bool(extra_files)
            p.paragraph_format.space_after = r.Pt(3)
        if extra_files:
            r._label(doc, "Files", "; ".join(extra_files), small=True)


def _method_note(doc: Any, include_level1: bool, include_level2: bool) -> None:
    r = _helpers()
    notes = []
    if include_level1:
        notes.append("WPD checks use the graph stored in each TAR.")
    if include_level2:
        notes.append("CSV checks use an independent source image; CSV values do not determine its axes.")
    notes.append("Estimate marks an interpolated value, not a confirmed tick reading. Unreadable ticks remain unresolved. Point counts alone do not confirm curve identity or full coverage.")
    r._p(doc, " ".join(notes), small=True)


def _marker_rows(project: Any) -> list[list[Any]]:
    r = _helpers()
    from wpd_report_findings import marker_reason
    checks = list(r._get(project, "calibration_checks", []) or [])
    old_points = list(r._get(project, "calibration", []) or [])
    legacy = {str(r._get(point, "role", "")): point for point in old_points}
    first_axis = str(r._get(checks[0], "axis_name", "")) if checks else ""
    if not checks:
        checks = [{"role": r._get(point, "role", "")} for point in old_points]
    axis_names = {str(r._get(check, "axis_name", "")) for check in checks}
    multiple_axes = len(axis_names) > 1
    rows = []
    for check in checks:
        role = str(r._get(check, "role", ""))
        axis = str(r._get(check, "axis_name", ""))
        old = legacy.get(role, {}) if axis == first_axis else {}
        fallback = r._get(old, "displayed_value", r._get(old, "dx") if role in {"X1", "X2"} else r._get(old, "dy"))
        assigned = r._get(check, "assigned", fallback)
        value = r._get(check, "independent_value")
        source = r._text(value, "Not confirmed")
        # Without a saved read-tick value comparison this number is an axis
        # estimate, even if the marker's position check recorded FAIL.
        estimated = bool(r._get(check, "is_estimate", False)) or (
            value is not None and r._get(check, "value_error") is None
            and r._status(check, "value_status") not in {"PASS", "OK"})
        if value is not None and estimated:
            source += " (Estimate)"
        label = role + ("\n" + (axis or "Unnamed axis") if multiple_axes else "")
        reason = marker_reason(check)
        if r._status(check) == "FAIL" and all(r._status(check, key) != "FAIL" for key in ("geometry_status", "value_status")):
            reason = "FAIL — " + reason
        rows.append([label, r._text(assigned, "Not recorded"), source,
                     r._status(check, "geometry_status"), r._status(check, "value_status"), reason])
    return rows


def _point_count(record: Any, field: str) -> Any:
    r = _helpers()
    value = r._get(record, field)
    return len(value) if isinstance(value, (tuple, list)) else r._text(value, "Not recorded")


def _existing_images(items: Iterable[tuple[Any, str]]) -> list[tuple[Path, str]]:
    result, seen = [], set()
    for value, caption in items:
        if not value:
            continue
        path = Path(str(value))
        try:
            if not path.is_file():
                continue
            key = str(path.resolve())
        except OSError:
            continue
        if key not in seen:
            result.append((path, caption))
            seen.add(key)
    return result


def _images(doc: Any, items: Iterable[tuple[Any, str]]) -> None:
    """Embed available evidence once, without manufacturing empty panels."""
    r = _helpers()
    images = _existing_images(items)
    for index in range(0, len(images), 2):
        pair = images[index:index + 2]
        if len(pair) == 2:
            r._picture_pair(doc, pair[0][0], pair[1][0], pair[0][1], pair[1][1])
            continue
        path, caption = pair[0]
        table = doc.add_table(rows=1, cols=1)
        table.alignment = r.WD_TABLE_ALIGNMENT.CENTER
        table.autofit = False
        table.columns[0].width = r.Inches(r.BODY_WIDTH)
        table.cell(0, 0).width = r.Inches(r.BODY_WIDTH)
        no_split = r.OxmlElement("w:cantSplit")
        table.rows[0]._tr.get_or_add_trPr().append(no_split)
        r._picture(table.cell(0, 0), path, caption, r.BODY_WIDTH - .12)
        doc.add_paragraph().paragraph_format.space_after = r.Pt(1)


def _level1(doc: Any, dr: Any) -> None:
    r = _helpers()
    doc.add_heading("WPD project checks", 1)
    projects = list(r._get(dr, "projects", []) or [])
    if not projects:
        r._p(doc, "No WPD project check was recorded.")
        return
    for project in projects:
        family = str(r._get(project, "family", "") or "Unclassified")
        filename = str(r._get(project, "project", "") or "Unnamed project")
        doc.add_heading(f"{family} — {filename}", 2)
        r._p(doc, f"Result: {r._status(project, 'overall_status')}. "
             f"Graph type: {r._text(r._get(project, 'graph_type'))} ({r._status(project, 'graph_type_status')}).", small=True)
        r._axis_settings(doc, project)
        r._label(doc, "Calibration result", r._status(project, "calibration_status"), small=True, keep_next=True)
        checks = _marker_rows(project)
        if checks:
            r._table(doc, ["Marker", "Assigned", "Source value", "Position", "Value", "Reason / next action"],
                     checks, [.6, .8, 1.2, .8, .7, 3.0], 9, keep_together=len(checks) <= 4)
        else:
            r._p(doc, "Calibration-marker records are unavailable.", small=True)
        _images(doc, [(r._get(project, "calibration_visual", ""), "Saved WPD calibration markers — X1, X2, Y1, Y2"),
                      (r._get(project, "extraction_visual", ""), "Saved WPD extraction points — compare with each source curve")])
        datasets = list(r._get(project, "datasets", []) or [])
        if datasets:
            rows = [[r._get(dataset, "name", ""), _point_count(dataset, "points"),
                     r._status(dataset, "mapping_status"), r._status(dataset, "trace_status"),
                     r._status(dataset, "completeness_status")] for dataset in datasets]
            r._table(doc, ["Dataset", "Points", "Identity", "Trace", "Coverage"], rows,
                     [3.0, .6, 1.15, 1.1, 1.25], 9)
        else:
            r._p(doc, "No extracted dataset record is available.", small=True)
        # Preserve aggregate failures even when legacy per-marker/dataset
        # records are incomplete or do not expose the same failed check.
        failures = [label for key, label in (
            ("marker_geometry_status", "marker position"), ("marker_value_status", "marker values"),
            ("dataset_status", "dataset identity"), ("trace_status", "trace"),
            ("completeness_status", "coverage"),
        ) if r._status(project, key) == "FAIL"]
        if failures:
            r._p(doc, "FAIL — " + "; ".join(failures) + ".", small=True)


def _level2(doc: Any, dr: Any) -> None:
    r = _helpers()
    doc.add_heading("Level 2 — CSV over standalone source image", 1)
    r._p(doc, "Each available CSV is plotted separately on its source graph when the axes can be mapped. "
              "Independent: axes established from the standalone image. Preview only: axes inherited from the TAR; "
              "this helps visual review but does not verify the CSV independently.", small=True)
    records = list(r._get(dr, "level2", []) or [])
    if not records:
        r._p(doc, "No independent CSV check was recorded.")
        return
    groups: OrderedDict[tuple[str, str], list[Any]] = OrderedDict()
    for record in records:
        key = (str(r._get(record, "family", "") or "Unclassified"),
               str(r._get(record, "source_image", "") or ""))
        groups.setdefault(key, []).append(record)
    source_names = Counter(r._basename(source) for _, source in groups if source)
    for (family, source), grouped in groups.items():
        source_label = r._basename(source) if source else "No identified source image"
        doc.add_heading(f"{family} — {source_label}", 2)
        if source and source_names[r._basename(source)] > 1:
            r._label(doc, "Source path", source, small=True)
        names = {"INDEPENDENT_AXES": "Independent", "WPD_VISUAL_PREVIEW": "Preview only", "UNAVAILABLE": "No plot"}
        rows = [[r._get(record, "csv_file", ""), _point_count(record, "point_count"),
                 r._status(record, "axis_status"),
                 names.get(str(r._get(record, "overlay_kind", "UNAVAILABLE")), "No plot"),
                 r._status(record, "trace_status"), r._status(record, "overall_status")]
                for record in grouped]
        r._table(doc, ["CSV file", "Points", "Image axes", "Overlay", "Trace", "Result"], rows,
                 [2.75, .5, .85, 1.0, .85, 1.15], 9)
        # One overlay per CSV. A source-only debug image is not a CSV overlay.
        plots = [(r._get(record, "overlay_visual", ""),
                  str(r._get(record, "csv_file", "")) +
                  (" — independently mapped" if r._get(record, "overlay_kind", "") == "INDEPENDENT_AXES" else
                   " — WPD-derived visual preview, NOT independent"))
                 for record in grouped]
        _images(doc, plots)
        unplotted = [str(r._get(record, "csv_file", "")) for record in grouped
                     if not r._get(record, "overlay_visual", "")]
        if unplotted:
            r._p(doc, "No overlay could be drawn for " + "; ".join(unplotted) +
                  ". The source image is missing, unreadable, or cannot be assigned numerical X/Y axes. "
                  "The CSV numeric checks still ran; source agreement remains REVIEW.", small=True)


def write_compact_device_reports(dr: Any, device_out: str | Path, separate_levels: bool = False) -> dict[str, str]:
    """Write a concise device report and optional separate level reports."""
    r = _helpers()
    from wpd_report_findings import collect_findings

    out = Path(device_out)
    stem = r._safe_filename(str(r._get(dr, "device", "Device")))
    paths = {
        "level1": out / f"{stem}_Level1_Report.docx",
        "level2": out / f"{stem}_Level2_Report.docx",
        "consolidated": out / f"{stem}_Consolidated_Device_Report.docx",
    }
    for key, attribute in (("level1", "level1_report_docx"), ("level2", "level2_report_docx"),
                           ("consolidated", "report_docx")):
        value = str(paths[key]) if separate_levels or key == "consolidated" else ""
        if isinstance(dr, dict):
            dr[attribute] = value
        else:
            setattr(dr, attribute, value)
    findings = list(collect_findings(dr))
    for kind, title in (("level1", "WPD project verification"),
                        ("level2", "Independent CSV verification"),
                        ("consolidated", "Device verification report")):
        if kind != "consolidated" and not separate_levels:
            continue
        doc = r._document(title, str(r._get(dr, "device", "")))
        _header(doc, dr)
        _availability(doc, dr)
        # File and dataset-label findings have explicit per-item rows below.
        # Their full plain-language reasons stay with the affected item.
        _findings(doc, [finding for finding in findings if finding.get("section") not in {"filename", "export"}])
        r._filename_audit(doc, dr, include_export_details=False)
        has_level1 = kind in {"level1", "consolidated"}
        has_level2 = kind in {"level2", "consolidated"}
        _method_note(doc, has_level1, has_level2)
        if has_level1:
            _level1(doc, dr)
        if has_level2:
            _level2(doc, dr)
        r._save(doc, paths[kind])
    return {key: str(value) if separate_levels or key == "consolidated" else ""
            for key, value in paths.items()}


def write_compact_intern_report(intern: str, records: Iterable[Any], path: str | Path, output_root: str | Path) -> str:
    """Write one linked, commented table covering every recorded device."""
    r = _helpers()
    from docx.enum.section import WD_ORIENT
    from wpd_report_findings import brief_findings

    destination = Path(path)
    records = sorted(list(records), key=lambda record: (
        str(r._get(record, "device", "")).casefold(), str(r._get(record, "device_relpath", ""))))
    counts = Counter(r._status(record, "overall_status") for record in records)
    doc = r._document("Intern verification overview", intern)
    # The comparative intern table needs more width than device evidence pages.
    # Override this document's section only; device reports stay portrait.
    section = doc.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = r.Inches(11), r.Inches(8.5)
    section.left_margin = section.right_margin = r.Inches(.7)
    section.top_margin = section.bottom_margin = r.Inches(.65)
    r._label(doc, "Intern", intern)
    r._p(doc, f"Devices: {len(records)}. PASS: {counts.get('PASS', 0)}; "
         f"FAIL: {counts.get('FAIL', 0)}; REVIEW: {counts.get('REVIEW', 0)}.")
    note_devices: OrderedDict[str, list[str]] = OrderedDict()
    for record in records:
        for note in _source_notes(record):
            note_devices.setdefault(note, []).append(str(r._get(record, "device", "")))
    for note, devices in note_devices.items():
        context = note if len(devices) == len(records) else note + " Devices: " + "; ".join(devices)
        r._p(doc, context, small=True)
    r._p(doc, "Open a device report for its calibration, source-image evidence and complete findings.", small=True)
    duplicate_names = Counter(str(r._get(record, "device", "")) for record in records)
    rows = []
    for record in records:
        device = str(r._get(record, "device", ""))
        label = device
        if duplicate_names[device] > 1:
            label += "\n" + str(r._get(record, "device_relpath", "") or r._get(record, "path", ""))
        notes = list(brief_findings(record))
        fatal = str(r._get(record, "fatal_error", "") or "")
        if fatal and not any(fatal in note for note in notes):
            notes.append("Verification interrupted: " + fatal)
        rows.append([label, r._status(record, "cc_status"), r._status(record, "oc_status"),
                     r._status(record, "tc_status"), r._status(record, "overall_status"),
                     "\n".join(notes) or "No additional actionable finding was recorded."])
    table = r._table(doc, ["Device / report", "CC", "OC", "TC", "Overall", "Comment / action"],
                     rows or [["No device records", "", "", "", "", ""]],
                     [1.75, .65, .65, .65, .7, 5.2], 9)
    for index, record in enumerate(records, 1):
        cell = table.rows[index].cells[0]
        cell.paragraphs[0].paragraph_format.keep_with_next = True
        p = cell.add_paragraph()
        p.paragraph_format.space_after = r.Pt(0)
        target = r._get(record, "report_docx", "")
        if target:
            r._link(p, target, "Open report", destination)
        else:
            r._set_font(p.add_run("Report path unavailable"), 9)
    return r._save(doc, destination)
