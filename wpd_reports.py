"""Word reports for the WPD verifier.

Report functions consume the verifier's result records. They never infer a PASS,
change a recorded status, or modify source projects. Both dataclasses and dictionaries
are accepted so saved JSON results can be rendered without rerunning verification.
"""
from __future__ import annotations

import math
import os
import re
import hashlib
from collections import Counter
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from PIL import Image

try:
    from docx import Document
    from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt, RGBColor
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
except ImportError as exc:
    raise ImportError(
        "Word reports require python-docx in the existing verifier interpreter. "
        "Install python-docx into that interpreter; do not create a new environment."
    ) from exc


FAMILIES = ("CC", "OC", "TC")
BODY_WIDTH = 7.1
INK = "000000"
HEADER_FILL = "243F55"
BORDER_COLOR = "D9D9D9"
PALE_FILL = "F3F6F8"
STATUS_FILL = {"PASS": "E7F2E6", "OK": "E7F2E6", "FAIL": "FBE5E5", "REVIEW": "FFF2CD", "WARNING": "FFF2CD"}


def _get(record: Any, key: str, default: Any = None) -> Any:
    if isinstance(record, dict):
        return record.get(key, default)
    return getattr(record, key, default)


def _text(value: Any, missing: str = "Not established") -> str:
    if value is None or value == "":
        return missing
    if isinstance(value, float):
        return f"{value:.6g}" if math.isfinite(value) else missing
    if isinstance(value, (tuple, list)):
        return ", ".join(_text(x, "") for x in value)
    return str(value)


def _status(record: Any, key: str = "status") -> str:
    value = _get(record, key, "REVIEW")
    return str(value or "REVIEW").upper()


def _unique(items: Iterable[Any]) -> list[str]:
    result, seen = [], set()
    for item in items:
        value = str(item).strip() if item is not None else ""
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _safe_filename(value: str) -> str:
    # Match the runner's safe_component(device, 70), including long-name hashes.
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    cleaned = re.sub(r"\s+", "_", cleaned) or "unnamed"
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{s}{i}" for s in ("COM", "LPT") for i in range(1, 10)}
    if cleaned.upper() in reserved:
        cleaned = "_" + cleaned
    if len(cleaned) > 70:
        cleaned = cleaned[:61] + "_" + hashlib.sha256(value.encode()).hexdigest()[:8]
    return cleaned


def _basename(value: Any) -> str:
    # Accept Windows paths even when report generation runs on another OS.
    return re.split(r"[/\\]", str(value))[-1] if value else "Not available"


def _aggregate(statuses: Iterable[str]) -> str:
    values = [str(s).upper() for s in statuses if s]
    if "FAIL" in values:
        return "FAIL"
    if not values or any(s not in {"PASS", "OK", "WARNING"} for s in values):
        return "REVIEW"
    return "PASS"


def _remove_children(element: Any, names: Iterable[str]) -> None:
    for name in names:
        for child in list(element.findall(qn(name))):
            element.remove(child)


def _set_font(run: Any, size: float | None = None, bold: bool | None = None, color: str = INK) -> None:
    run.font.name = "Arial"
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    run.font.color.rgb = RGBColor.from_string(color)
    rpr = run._element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.insert(0, fonts)
    for attr in ("ascii", "hAnsi", "cs", "eastAsia"):
        fonts.set(qn("w:" + attr), "Arial")


def _document(title: str, subject: str) -> Any:
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Inches(8.5), Inches(11)
    sec.top_margin, sec.bottom_margin = Inches(.65), Inches(.65)
    sec.left_margin = sec.right_margin = Inches(.7)
    sec.footer_distance = Inches(.27)
    for name, size in [("Normal", 10.5), ("Title", 20), ("Subtitle", 12), ("Heading 1", 14), ("Heading 2", 11.5), ("Heading 3", 10.5), ("Caption", 9)]:
        style = doc.styles[name]
        style.font.name = "Arial"
        style.font.size = Pt(size)
        style.font.color.rgb = RGBColor.from_string(INK)
        rpr = style.element.get_or_add_rPr()
        color = rpr.find(qn("w:color"))
        if color is not None:
            for attr in ("themeColor", "themeTint", "themeShade"):
                color.attrib.pop(qn("w:" + attr), None)
        ppr = style.element.get_or_add_pPr()
        _remove_children(ppr, ["w:pBdr", "w:shd"])
        style.paragraph_format.space_after = Pt(5 if name == "Normal" else 6)
        style.paragraph_format.line_spacing = 1.05
        if name.startswith("Heading"):
            style.font.bold = True
            style.paragraph_format.space_before = Pt(10)
            style.paragraph_format.keep_with_next = True
        elif name == "Title":
            style.paragraph_format.space_after = Pt(5)
            style.paragraph_format.keep_with_next = True
    doc.core_properties.title = title
    doc.core_properties.subject = subject
    doc.core_properties.author = "WPD Verifier"
    p = doc.add_paragraph(title, "Title")
    for run in p.runs:
        _set_font(run, 20, False)
    footer = sec.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = footer.add_run("Page ")
    _set_font(run, 8)
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), "PAGE")
    footer._p.append(fld)
    return doc


def _p(doc: Any, text: str, bold: bool = False, small: bool = False) -> Any:
    p = doc.add_paragraph()
    if bold:
        p.paragraph_format.keep_with_next = True
    run = p.add_run(text)
    _set_font(run, 9 if small else 10.5, bold)
    return p


def _label(doc: Any, label: str, value: Any, small: bool = False, keep_next: bool = False) -> Any:
    p = doc.add_paragraph()
    if keep_next:
        p.paragraph_format.keep_with_next = True
    _set_font(p.add_run(label + " "), 9 if small else 10.5, True)
    _set_font(p.add_run(_text(value)), 9 if small else 10.5)
    return p


def _table(doc: Any, headers: list[str], rows: Iterable[Iterable[Any]], widths: list[float] | None = None, font_size: float = 9.5, keep_together: bool = False) -> Any:
    rows = list(rows)
    table = doc.add_table(rows=1, cols=len(headers))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    widths = widths or [BODY_WIDTH / len(headers)] * len(headers)
    for i, width in enumerate(widths):
        table.columns[i].width = Inches(width)
    tblpr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for name in ("top", "left", "bottom", "right", "insideH", "insideV"):
        edge = OxmlElement("w:" + name)
        edge.set(qn("w:val"), "single")
        edge.set(qn("w:sz"), "4")
        edge.set(qn("w:color"), BORDER_COLOR)
        borders.append(edge)
    tblpr.append(borders)
    header = table.rows[0]
    repeat = OxmlElement("w:tblHeader")
    repeat.set(qn("w:val"), "true")
    header._tr.get_or_add_trPr().append(repeat)
    all_rows = [headers] + [list(row) for row in rows]
    for row_index, data in enumerate(all_rows):
        row = header if row_index == 0 else table.add_row()
        # Keep a single record intact when it fits a page. Long records remain
        # breakable so an extensive comment cannot create a blank page.
        if row_index == 0 or sum(len(str(item)) for item in data) < 1500:
            no_split = OxmlElement("w:cantSplit")
            row._tr.get_or_add_trPr().append(no_split)
        for col, cell in enumerate(row.cells):
            cell.width = Inches(widths[col])
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            tcpr = cell._tc.get_or_add_tcPr()
            margins = OxmlElement("w:tcMar")
            for side, value in [("top", "70"), ("bottom", "70"), ("left", "90"), ("right", "90")]:
                node = OxmlElement("w:" + side)
                node.set(qn("w:w"), value)
                node.set(qn("w:type"), "dxa")
                margins.append(node)
            tcpr.append(margins)
            shading = OxmlElement("w:shd")
            value = _text(data[col] if col < len(data) else "", "")
            fill = HEADER_FILL if row_index == 0 else (STATUS_FILL.get(value) or (PALE_FILL if row_index % 2 == 0 else "FFFFFF"))
            shading.set(qn("w:fill"), fill)
            tcpr.append(shading)
            p = cell.paragraphs[0]
            p.paragraph_format.space_after = Pt(0)
            p.paragraph_format.line_spacing = 1.03
            p.paragraph_format.keep_with_next = row_index == 0 or (keep_together and row_index < len(all_rows) - 1)
            if row_index == 0 or value in STATUS_FILL or re.fullmatch(r"[0-9]+", value):
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            else:
                p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            _set_font(p.add_run(value), font_size, row_index == 0 or value in STATUS_FILL, "FFFFFF" if row_index == 0 else INK)
    # A short spacer separates the table from the next paragraph.
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(1)
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.line_spacing = 0.25
    _set_font(p.add_run(""), 2)
    return table


def _link(paragraph: Any, target: str | Path, label: str, report_path: Path) -> None:
    target = Path(target)
    try:
        address = os.path.relpath(target, report_path.parent).replace(os.sep, "/")
    except ValueError:
        address = target.as_uri()
    rid = paragraph.part.relate_to(quote(address, safe="/:._-"), RT.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), rid)
    run = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "174D75")
    rpr.append(color)
    size = OxmlElement("w:sz")
    size.set(qn("w:val"), "19")
    rpr.append(size)
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    rpr.append(underline)
    run.append(rpr)
    text = OxmlElement("w:t")
    text.text = label
    run.append(text)
    link.append(run)
    paragraph._p.append(link)


def _picture(cell: Any, image_path: Any, caption: str, width: float) -> None:
    p = cell.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(4)
    _set_font(p.add_run(caption), 9, True)
    path = Path(str(image_path)) if image_path else None
    if path is None or not path.is_file():
        p = cell.add_paragraph("Evidence image not available.")
        for run in p.runs:
            _set_font(run, 9)
        return
    try:
        with Image.open(path) as im:
            iw, ih = im.size
        # A native-pixel limit at 96 px/in avoids enlarging small source graphs.
        scale = min(1.0, width * 96.0 / iw, 3.7 * 96.0 / ih)
        p = cell.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_after = Pt(0)
        shape = p.add_run().add_picture(str(path), width=Inches(iw * scale / 96.0))
        shape._inline.docPr.set("descr", caption + ". " + path.name)
    except Exception as exc:
        p = cell.add_paragraph("Evidence image could not be embedded: " + path.name + ". " + type(exc).__name__)
        for run in p.runs:
            _set_font(run, 9)


def _picture_pair(doc: Any, left: Any, right: Any, left_caption: str, right_caption: str) -> None:
    table = doc.add_table(rows=1, cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    for i in range(2):
        table.columns[i].width = Inches(BODY_WIDTH / 2)
        cell = table.cell(0, i)
        cell.width = Inches(BODY_WIDTH / 2)
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        margins = OxmlElement("w:tcMar")
        for side in ("top", "bottom", "left", "right"):
            node = OxmlElement("w:" + side)
            node.set(qn("w:w"), "45")
            node.set(qn("w:type"), "dxa")
            margins.append(node)
        cell._tc.get_or_add_tcPr().append(margins)
    borders = OxmlElement("w:tblBorders")
    for name in ("top", "left", "bottom", "right", "insideH", "insideV"):
        edge = OxmlElement("w:" + name)
        edge.set(qn("w:val"), "single")
        edge.set(qn("w:sz"), "4")
        edge.set(qn("w:color"), BORDER_COLOR)
        borders.append(edge)
    table._tbl.tblPr.append(borders)
    cant_split = OxmlElement("w:cantSplit")
    table.rows[0]._tr.get_or_add_trPr().append(cant_split)
    _picture(table.cell(0, 0), left, left_caption, BODY_WIDTH / 2 - .12)
    _picture(table.cell(0, 1), right, right_caption, BODY_WIDTH / 2 - .12)
    doc.add_paragraph().paragraph_format.space_after = Pt(1)


def _comments(doc: Any, comments: Iterable[str], empty: str | None = None) -> None:
    values = _unique(comments)
    if not values:
        if empty:
            _p(doc, empty)
        return
    for text in values:
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Inches(.12)
        p.paragraph_format.first_line_indent = Inches(-.12)
        p.paragraph_format.space_after = Pt(4)
        _set_font(p.add_run("\u2022 "), 10.5)
        _set_font(p.add_run(text), 10.5)


def family_comments(dr: Any, family: str) -> str:
    """Return report comments without inventing conclusions beyond stored records."""
    saved = _get(dr, "family_comments", {}) or {}
    if isinstance(saved, dict) and saved.get(family):
        return str(saved[family])
    comments = []
    for req in _get(dr, "requirements", []) or []:
        if _get(req, "family") == family and _status(req) not in {"PASS", "OK"}:
            comments.append(f"{_status(req)}: {_get(req, 'requirement', '')}. {_get(req, 'comment', '')}".strip())
    for p in _get(dr, "projects", []) or []:
        if _get(p, "family") == family:
            for comment in _get(p, "comments", []) or []:
                comments.append(f"Level 1 {_get(p, 'project', '')}: {comment}")
    for p in _get(dr, "level2", []) or []:
        if _get(p, "family") == family:
            for comment in _get(p, "comments", []) or []:
                comments.append(f"Level 2 {_get(p, 'csv_file', '')}: {comment}")
    return "\n".join(_unique(comments)) or "No additional finding was recorded."


def _identity(doc: Any, dr: Any, purpose: str) -> None:
    _label(doc, "Device", _get(dr, "device", ""))
    _label(doc, "Intern", _get(dr, "intern", ""))
    _label(doc, "Folder", _get(dr, "device_relpath") or _get(dr, "device", ""), small=True)
    _p(doc, purpose)
    snapshot = str(_get(dr, "source_snapshot", "") or "")
    if snapshot and not any(snapshot in str(note) for note in (_get(dr, "source_notes", []) or [])):
        _label(doc, "Source snapshot", snapshot, small=True)
    for note in _get(dr, "source_notes", []) or []:
        _p(doc, str(note), small=True)
    if _get(dr, "fatal_error"):
        _label(doc, "Verification interrupted", _get(dr, "fatal_error"))


def _summary(doc: Any, dr: Any) -> None:
    _p(doc, "Combined device result", bold=True)
    _table(doc, ["CC", "OC", "TC", "Overall"], [[_status(dr, "cc_status"), _status(dr, "oc_status"), _status(dr, "tc_status"), _status(dr, "overall_status")]], [1.775] * 4, 10)
    _p(doc, "PASS means the required check is supported by the available evidence. FAIL identifies a definite error or a missing required output. REVIEW identifies an unresolved check. Availability flags describe missing source material separately.", small=True)


def _availability(doc: Any, dr: Any) -> None:
    from wpd_report_findings import folder_presence, plain_requirement_comment
    doc.add_heading("Source and file availability", 1)
    inv = _get(dr, "inventory", {}) or {}
    _table(doc, ["Datasheets", "WPD projects", "CSV files", "Graph images"], [[inv.get("PDF", len(_get(dr, "datasheets", []) or [])), inv.get("TAR", ""), inv.get("CSV", ""), inv.get("Images", "")]], [1.775] * 4)
    sheets = _get(dr, "datasheets", []) or []
    _label(doc, "Datasheet files", "Present — " + "; ".join(_basename(x) for x in sheets) if sheets else "Absent from the supplied folder", small=True)
    av = _get(dr, "availability", {}) or {}
    if av:
        rows = []
        for fam in FAMILIES:
            rows.append([fam, folder_presence(av.get(fam + "_IMAGE")), folder_presence(av.get(fam + "_TAR")), folder_presence(av.get(fam + "_CSV"))])
        # This compact table fits on one page. Keeping its rows together also
        # prevents the Word-to-PDF renderer from dropping trailing rows when
        # only the header and first family fit at the bottom of a page.
        _table(doc, ["Family", "Folder image", "WPD project", "CSV export"], rows, [.7, 2.25, 2.1, 2.05], keep_together=True)
        _p(doc, "Present/Absent refers to files identified in this device folder.", small=True)
    flags = _get(dr, "availability_flags", []) or []
    if flags:
        _label(doc, "Availability flags", "; ".join(str(f) for f in flags), small=True)
    reqs = [req for req in (_get(dr, "requirements", []) or [])
            if _get(req, "category", "") not in {"Dataset filename correspondence", "CSV export consistency"}]
    if reqs:
        counts = Counter(_status(r) for r in reqs)
        _p(doc, "Folder and output requirements: " + "; ".join(f"{counts[status]} {status}" for status in ("PASS", "OK", "FAIL", "REVIEW", "WARNING") if counts[status]) + ".", small=True)
        unresolved = [r for r in reqs if _status(r) not in {"PASS", "OK"}]
        if unresolved:
            doc.add_heading("Missing or incomplete evidence", 2)
            rows = [[_get(r, "family", ""), _get(r, "requirement", ""), _status(r), _get(r, "found", "") or "Not found"] for r in unresolved]
            _table(doc, ["Family", "Required item", "Status", "Found"], rows, [.65, 2.0, 1.0, 3.45])
            for r in unresolved:
                prefix = f"{_get(r, 'family', '')} {_get(r, 'requirement', '')}".strip()
                issue = _get(r, "issue_code", "")
                detail = plain_requirement_comment(r)
                expected = _get(r, "expected", "")
                _p(doc, f"{_status(r)}  {prefix}" + (f"  {issue}" if issue else ""), bold=True)
                _p(doc, str(detail))
                if expected:
                    _label(doc, "Expected", expected, small=True)
    _comments(doc, _get(dr, "warnings", []) or [])


def _filename_audit(doc: Any, dr: Any, types: set[str] | None = None, include_export_details: bool = True) -> None:
    from wpd_report_findings import plain_file_comment, plain_requirement_comment
    doc.add_heading("Filename and dataset correspondence", 1)
    files = [f for f in (_get(dr, "files", []) or []) if types is None or _get(f, "file_type", "") in types]
    if not files:
        _p(doc, "No files were available for this filename audit.")
    else:
        rows = []
        for item in files:
            current = str(_get(item, "current_filename", ""))
            expected = str(_get(item, "expected_filename", "") or "Not recorded")
            if expected == current or expected == _basename(current):
                expected = "Same filename"
            family = str(_get(item, "family", "") or "")
            if family.upper() in {"UNKNOWN", "GENERAL"}:
                family = "Unclassified"
            label = current + "\n" + " ".join(part for part in (family, str(_get(item, "file_type", ""))) if part)
            rows.append([label, expected, _status(item), plain_file_comment(item)])
        _table(doc, ["Current file", "Expected name / identity", "Result", "Comment / next action"], rows, [2.05, 1.8, .75, 2.5], 9)
    requirements = _get(dr, "requirements", []) or []
    for category, title in (("Dataset filename correspondence", "Dataset-to-CSV label checks"),
                            ("CSV export consistency", "CSV export value checks")):
        items = [req for req in requirements if _get(req, "category", "") == category]
        doc.add_heading(title, 2)
        if not items:
            _p(doc, "No separate " + ("dataset-to-CSV association" if category == "Dataset filename correspondence" else "CSV export value") + " check was saved.", small=True)
            continue
        rows = [[_get(req, "requirement", ""), _get(req, "found", "") or "Not established",
                 _status(req), plain_requirement_comment(req)] for req in items]
        _table(doc, ["Recorded check", "CSV / matching file", "Result", "Reason / next action"], rows, [1.85, 2.05, .75, 2.45], 9)
        if include_export_details and category == "CSV export consistency":
            for req in items:
                raw = str(_get(req, "comment", "") or "")
                if raw and _status(req) not in {"PASS", "OK"} and raw != plain_requirement_comment(req):
                    _label(doc, "Technical export detail — " + str(_get(req, "requirement", "")), raw, small=True)


def _axis_settings(doc: Any, project: Any) -> None:
    from wpd_report_findings import axis_setting_rows
    rows, legacy = axis_setting_rows(project)
    _p(doc, "Axis scale settings", bold=True)
    _table(doc, ["Axis set", "Chosen in TAR", "Read from source", "Check", "Reason / next action"],
           rows, [1.0, 1.1, 1.2, .75, 3.05], 9, keep_together=len(rows) <= 2)
    if legacy:
        _p(doc, "Chosen modes above are the saved first-axis summary. Exact TAR flags and per-axis source readings were not included in this older record.", small=True)


def _level1(doc: Any, dr: Any) -> None:
    from wpd_reports_compact import _marker_rows
    doc.add_heading("Level 1 WPD project verification", 1)
    _p(doc, "Calibration-marker positions and assigned values are checked against the embedded source graph. X and Y scale settings are checked independently. Exported CSV values are not used to establish the source calibration.")
    projects = _get(dr, "projects", []) or []
    if not projects:
        _p(doc, "No WPD project was available. Level 1 is not completed for the missing projects.")
        return
    for n, pr in enumerate(projects, 1):
        doc.add_heading(f"WPD project {n}", 2)
        _p(doc, str(_get(pr, "project", "")), bold=True)
        _label(doc, "Family and project result", f"{_get(pr, 'family', '')}  {_status(pr, 'overall_status')}", keep_next=True)
        rows = [
            ["Graph type", _get(pr, "graph_type", "") or "Not established", _status(pr, "graph_type_status")],
            ["X and Y scale check", "Chosen TAR modes and independent source readings are listed below", _status(pr, "scale_status")],
            ["Marker placement", "X1 X2 Y1 Y2", _status(pr, "marker_geometry_status")],
            ["Marker values", "Assigned values versus source ticks", _status(pr, "marker_value_status")],
            ["Calibration", "Placement values and scales", _status(pr, "calibration_status")],
            ["Dataset identity", "Label and intended source curve", _status(pr, "dataset_status")],
            ["Extracted trace", "Recorded trace assessment", _status(pr, "trace_status")],
            ["Curve completeness", "Recorded coverage assessment", _status(pr, "completeness_status")],
        ]
        _table(doc, ["Check", "Evidence or setting", "Status"], rows, [1.65, 4.6, .85])
        _axis_settings(doc, pr)
        _label(doc, "Independent source axes", _get(pr, "source_axis_summary", "") or "Not established")
        _label(doc, "Embedded source image", _get(pr, "image_name", "") or "Not available", small=True)
        _picture_pair(doc, _get(pr, "calibration_visual", ""), _get(pr, "extraction_visual", ""), "Calibration markers", "WPD extraction points")
        checks = list(_get(pr, "calibration_checks", []) or [])
        old_points = {str(_get(cp, "role", "")): cp for cp in (_get(pr, "calibration", []) or [])}
        first_axis = str(_get(checks[0], "axis_name", "")) if checks else ""
        if not checks:
            checks = [{"role": role} for role in old_points]
        detailed_comments = set()
        calrows = []
        for check in checks:
            role = _get(check, "role", "")
            cp = old_points.get(str(role), {}) if str(_get(check, "axis_name", "")) == first_axis else {}
            legacy_assigned = _get(cp, "displayed_value", _get(cp, "dx") if role in {"X1", "X2"} else _get(cp, "dy"))
            assigned = _get(check, "assigned", legacy_assigned)
            axis_name = _get(check, "axis_name", "")
            role_label = str(role) + ("\n" + str(axis_name) if axis_name else "")
            placement_value = f"{_status(check, 'geometry_status')}\n{_status(check, 'value_status')}"
            independent = _text(_get(check, "independent_value"), "Not read")
            if _get(check, "independent_value") is not None and _get(check, "value_error") is None and _status(check, "value_status") not in {"PASS", "OK"}:
                independent += "\nEstimate"
            coordinate = _text(_get(check, "source_tick_pixel"), "Not read")
            if _get(check, "geometry_error_px") is not None:
                coordinate += f"\n{_text(_get(check, 'geometry_error_px'))} / {_text(_get(check, 'alignment_tolerance_px'))}"
            calrows.append([role_label, f"{_text(_get(check, 'px', _get(cp, 'px')))}, {_text(_get(check, 'py', _get(cp, 'py')))}", _text(assigned), independent, coordinate, placement_value, _status(check)])
        if calrows:
            _table(doc, ["Marker", "Assigned", "Source value", "Position", "Value", "Reason / next action"],
                   _marker_rows(pr), [.6, .8, 1.2, .8, .7, 3.0], 9, keep_together=len(calrows) <= 4)
            _p(doc, "Marker measurement details", bold=True)
            _table(doc, ["Marker", "Pixel X Y", "Assigned", "Source value", "Tick px\nDistance / limit", "Position\nValue", "Status"], calrows, [.6, 1.15, .9, 1.05, 1.5, 1.15, .75], 9)
            _p(doc, "Distance and alignment limit are in source-image pixels. An Estimate is interpolated for review; it is not a confirmed printed tick value.", small=True)
            for check in checks:
                role = _get(check, "role", "")
                comment = _get(check, "comment", "")
                if comment:
                    detailed_comments.add(f"{role}: {comment}")
                    if _status(check) not in {"PASS", "OK"}:
                        axis_prefix = str(_get(check, "axis_name", "")) + " " if len({str(_get(c, "axis_name", "")) for c in checks}) > 1 else ""
                        _p(doc, f"{axis_prefix}{role}: {comment}", small=True)
        else:
            _p(doc, "Calibration-marker records are unavailable.")
        datasets = _get(pr, "datasets", []) or []
        if datasets:
            dsrows = [[_get(d, "name", ""), _get(d, "points", ""), _status(d, "mapping_status"), _status(d, "trace_status"), _status(d, "completeness_status"), _text(_get(d, "median_error_px")), _text(_get(d, "p95_error_px"))] for d in datasets]
            _table(doc, ["Dataset", "Points", "Identity", "Trace", "Coverage", "Median px", "P95 px"], dsrows, [2.2, .6, .85, .75, .9, .85, .95], 9)
            _p(doc, "Point counts do not prove coverage or adequate knee sampling. Ink distances can include grid lines, text and other curves. For datasets marked REVIEW, confirm the intended labelled curve, every-point alignment and the full useful source range visually.", small=True)
            for dataset in datasets:
                notes = _get(dataset, "comments", []) or []
                detailed_comments.update(f"{_get(dataset, 'name', '')}: {note}" for note in notes)
                # Counts and proximity measurements are already tabulated, and
                # their shared limitations are stated immediately above.
                unique_notes = [note for note in notes if not any(fragment in note for fragment in ("points are present; the count does not prove", "Generic-ink proximity p95=", "Correct trace identity, every-point alignment"))]
                if unique_notes:
                    _p(doc, str(_get(dataset, "name", "Dataset")), bold=True)
                    _comments(doc, unique_notes)
        doc.add_heading("Project findings", 3)
        remaining = [comment for comment in (_get(pr, "comments", []) or []) if comment not in detailed_comments]
        _comments(doc, remaining, "All project findings are recorded with their markers and datasets above.")


def _level2(doc: Any, dr: Any) -> None:
    doc.add_heading("Level 2 independent CSV verification", 1)
    _p(doc, "Each available CSV is checked against its standalone source graph. When the image provides enough tick evidence, the overlay uses independent numerical axes. Otherwise, a clearly marked WPD-derived visual preview may be shown; it is not an independent verification and remains REVIEW. Unresolved tick values and scale settings are never promoted to PASS.")
    _p(doc, "The blue diagnostic marker identifies the mapped reference anchor; it does not shift the fitted axes. Distances measure proximity to any visible ink, including grid lines, labels and other curves. Curve identity and full useful range coverage require visual review against the legend and source trace.", small=True)
    records = _get(dr, "level2", []) or []
    if not records:
        _p(doc, "No CSV check was available. Level 2 is not completed for the missing exports.")
        return
    shown_mappings = {}
    for n, lr in enumerate(records, 1):
        doc.add_heading(f"CSV dataset {n}", 2)
        _p(doc, str(_get(lr, "csv_file", "")), bold=True)
        _label(doc, "Family and dataset result", f"{_get(lr, 'family', '')}  {_status(lr, 'overall_status')}", keep_next=True)
        _label(doc, "Standalone source", _basename(_get(lr, "source_image", "")), small=True, keep_next=True)
        _table(doc, ["Independent mapping", "CSV order", "Trace", "Dataset result"], [[_status(lr, "axis_status"), _status(lr, "x_order_status"), _status(lr, "trace_status"), _status(lr, "overall_status")]], [1.775] * 4)
        evidence_rows = []
        reasons = []
        omitted_words = []
        for axis, key in [("X", "x_axis_evidence"), ("Y", "y_axis_evidence")]:
            for ev in _get(lr, key, []) or []:
                raw = str(_get(ev, "raw_text", ""))
                if not re.search(r"\d", raw) and _get(ev, "interpreted") is None and _get(ev, "raw_value") is None:
                    omitted_words.append(f"{axis}: {raw}")
                    continue
                reason = str(_get(ev, "reason", "") or "No reason recorded")
                if reason not in reasons:
                    reasons.append(reason)
                evidence_rows.append([axis, raw, _text(_get(ev, "interpreted"), "Not read"), "YES" if _get(ev, "used", False) else "NO", _text(_get(ev, "pos")), str(reasons.index(reason) + 1)])
        signature = repr((_get(lr, "source_image", ""), _get(lr, "x_mapping", ""), _get(lr, "y_mapping", ""), evidence_rows, reasons, omitted_words))
        first_mapping = signature not in shown_mappings
        if first_mapping:
            _label(doc, "X mapping", _get(lr, "x_mapping", "") or "Not established", small=True)
            _label(doc, "Y mapping", _get(lr, "y_mapping", "") or "Not established", small=True)
            origin = _get(lr, "origin_px")
            anchor = f"({_text(origin[0])}, {_text(origin[1])}) px" if origin and len(origin) >= 2 else "Not established"
            anchor_values = _get(lr, "anchor_values")
            if anchor_values is not None:
                anchor += f"; data values ({_text(anchor_values)})"
            if _get(lr, "origin_kind"):
                anchor += "; " + str(_get(lr, "origin_kind"))
            _label(doc, "Mapping anchor", anchor, small=True)
        else:
            _p(doc, f"The independent mapping, anchor and accepted/rejected tick evidence are identical to CSV dataset {shown_mappings[signature]}; that mapping applies here.", small=True)
        caption = "CSV overlay on source graph"
        if _get(lr, "overlay_kind") == "WPD_VISUAL_PREVIEW":
            caption = "VISUAL PREVIEW — WPD-based axes, NOT independent"
        _picture_pair(doc, _get(lr, "overlay_visual", ""), _get(lr, "axis_debug_visual", "") or _get(lr, "diagnostic_visual", ""), caption, "Independent image-axis evidence")
        if evidence_rows and signature not in shown_mappings:
            _table(doc, ["Axis", "Raw OCR", "Interpreted value", "Used", "Pixel", "Reason"], evidence_rows, [.5, 1.55, 1.5, .7, 1.25, 1.6], 9)
            for number, reason in enumerate(reasons, 1):
                _p(doc, f"Reason {number}: {reason}", small=True)
            if omitted_words:
                _p(doc, "Rejected nonnumeric OCR text: " + "; ".join(_unique(omitted_words)) + ". No numeric value was inferred. Full token positions remain in device_result.json.", small=True)
            shown_mappings[signature] = n
        elif not evidence_rows and first_mapping:
            _p(doc, "No accepted OCR mapping evidence was recorded.", small=True)
            shown_mappings[signature] = n
        median, p95 = _get(lr, "median_error_px"), _get(lr, "p95_error_px")
        _label(doc, "Trace proximity", f"Median {_text(median)} px; P95 {_text(p95)} px" if median is not None and p95 is not None else "Not measured; see the mapping status and recorded comments.", small=True)
        notes = [comment for comment in (_get(lr, "comments", []) or []) if not any(comment.startswith(prefix) for prefix in ("Blue marker:", "Independent calibration uses", "Distance to any visible source ink:", "Full curve identity and coverage need visual review"))]
        _comments(doc, notes, "No additional dataset comment was recorded.")


def _save(doc: Any, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    return str(path)


def write_device_reports(dr: Any, device_out: str | Path, separate_levels: bool = False, report_detail: str = "compact") -> dict[str, str]:
    """Create a self-contained device report and optional separate L1/L2 reports."""
    if report_detail not in {"reviewer", "compact", "full"}:
        raise ValueError("report_detail must be 'reviewer', 'compact' or 'full'")
    if report_detail == "reviewer":
        from wpd_reports_reviewer import write_reviewer_device_reports
        return write_reviewer_device_reports(dr, device_out, separate_levels)
    if report_detail == "compact":
        from wpd_reports_compact import write_compact_device_reports
        return write_compact_device_reports(dr, device_out, separate_levels)
    out = Path(device_out)
    stem = _safe_filename(str(_get(dr, "device", "Device")))
    paths = {
        "level1": out / f"{stem}_Level1_Report.docx",
        "level2": out / f"{stem}_Level2_Report.docx",
        "consolidated": out / f"{stem}_Consolidated_Device_Report.docx",
    }
    for key, attr in [("level1", "level1_report_docx"), ("level2", "level2_report_docx"), ("consolidated", "report_docx")]:
        if isinstance(dr, dict):
            dr[attr] = str(paths[key]) if separate_levels or key == "consolidated" else ""
        else:
            setattr(dr, attr, str(paths[key]) if separate_levels or key == "consolidated" else "")
    for kind, title, purpose in [
        ("level1", "WPD calibration and project verification", "This report records Level 1 calibration, scale, dataset and trace checks for the supplied WPD projects."),
        ("level2", "Independent CSV verification", "This report records Level 2 checks of the CSV exports against independent source images, with mapping evidence and unresolved findings."),
        ("consolidated", "Device digitization verification", "This report combines file availability, filename checks, WPD project verification and independent CSV verification. Use the findings below to locate corrections and unresolved checks."),
    ]:
        if kind != "consolidated" and not separate_levels:
            continue
        doc = _document(title, str(_get(dr, "device", "")))
        _identity(doc, dr, purpose)
        _summary(doc, dr)
        if kind == "consolidated":
            doc.add_heading("Findings by characteristic", 1)
            for family in FAMILIES:
                _p(doc, f"{family}  {_status(dr, family.lower() + '_status')}", bold=True)
                _comments(doc, _overview_reasons(dr, only_family=family))
            _p(doc, "The sections below contain the complete per-file findings, marker checks, image evidence and unresolved mapping details.", small=True)
            if separate_levels:
                p = doc.add_paragraph()
                _link(p, paths["level1"], "Separate Level 1 report", paths[kind])
                p.add_run("     ")
                _link(p, paths["level2"], "Separate Level 2 report", paths[kind])
        _availability(doc, dr)
        _filename_audit(doc, dr, {"WPD project", "Source image"} if kind == "level1" else ({"CSV", "Source image"} if kind == "level2" else None))
        if kind in {"level1", "consolidated"}:
            _level1(doc, dr)
        if kind in {"level2", "consolidated"}:
            _level2(doc, dr)
        _save(doc, paths[kind])
    return {key: (str(value) if separate_levels or key == "consolidated" else "") for key, value in paths.items()}


def _intern_issue_flags(records: list[Any]) -> list[tuple[str, int]]:
    counts: Counter[str] = Counter()
    for dr in records:
        for flag in set(str(f) for f in (_get(dr, "availability_flags", []) or [])):
            counts[flag] += 1
    return sorted(counts.items(), key=lambda x: (-x[1], x[0]))


def _overview_reasons(dr: Any, only_family: str | None = None) -> list[str]:
    """Keep the intern overview actionable; complete per-check text is in device reports."""
    result = []
    for family in ([only_family] if only_family else FAMILIES):
        value = family_comments(dr, family)
        lines = _unique(value.splitlines())
        # Do not duplicate routine counts, mapping-method descriptions and generic
        # coverage limitations from every dataset in a short device overview.
        routine = (
            "numeric rows, preserving the first numeric row", "Blue marker:",
            "Independent calibration uses", "Distance to any visible source ink:",
            "Full curve identity and coverage need visual review", "points are present; the count does not prove",
            "Correct trace identity, every-point alignment", "Dataset-to-source-curve identity is not automatically",
        )
        concrete = [line for line in lines if not any(fragment in line for fragment in routine)]
        # Preserve all explicit failures. Include at most two distinct remaining
        # reasons; the detailed sections retain all actual per-file comments.
        failures = [line for line in concrete if re.search(r"\bFAIL\b", line)]
        projects = [p for p in (_get(dr, "projects", []) or []) if _get(p, "family") == family]
        csvs = [p for p in (_get(dr, "level2", []) or []) if _get(p, "family") == family]
        direct_failures = []
        # Calibration comments do not necessarily spell out FAIL, so obtain the
        # failed records themselves instead of relying on keyword selection.
        for req in (_get(dr, "requirements", []) or []):
            if _get(req, "family") == family and _status(req) == "FAIL":
                direct_failures.append(f"FAIL {_get(req, 'requirement', '')}: {_get(req, 'comment', '')}")
        for audit in (_get(dr, "files", []) or []):
            if _get(audit, "family") == family and _status(audit) == "FAIL":
                direct_failures.append(f"FAIL filename {_get(audit, 'current_filename', '')}: {_get(audit, 'comment', '')}")
        for project in projects:
            project_name = str(_get(project, "project", ""))
            represented_comments = set()
            for check in (_get(project, "calibration_checks", []) or []):
                if "FAIL" in {_status(check), _status(check, "geometry_status"), _status(check, "value_status")}:
                    role = str(_get(check, "role", ""))
                    axis = str(_get(check, "axis_name", ""))
                    comment = str(_get(check, "comment", "") or "Marker verification failed; inspect the marker evidence table.")
                    direct_failures.append(f"FAIL L1 {project_name}, {axis} {role}: {comment}")
                    represented_comments.add(f"{role}: {comment}")
            for dataset in (_get(project, "datasets", []) or []):
                if "FAIL" in {_status(dataset, key) for key in ("mapping_status", "trace_status", "completeness_status")}:
                    for comment in (_get(dataset, "comments", []) or []):
                        if not any(fragment in comment for fragment in routine + ("Generic-ink proximity p95=",)):
                            direct_failures.append(f"FAIL L1 {project_name}, dataset {_get(dataset, 'name', '')}: {comment}")
                            represented_comments.add(f"{_get(dataset, 'name', '')}: {comment}")
            if _status(project, "overall_status") == "FAIL":
                for comment in (_get(project, "comments", []) or []):
                    if comment not in represented_comments and not any(fragment in comment for fragment in routine):
                        direct_failures.append(f"L1 {project_name} result FAIL; recorded finding: {comment}")
        for csv in csvs:
            if _status(csv, "overall_status") == "FAIL":
                for comment in (_get(csv, "comments", []) or []):
                    if not any(fragment in comment for fragment in routine):
                        direct_failures.append(f"L2 {_get(csv, 'csv_file', '')} result FAIL; recorded finding: {comment}")
        # The family summary may repeat the same structured failure using
        # different punctuation ("FAIL: Item. reason" versus "FAIL Item: reason").
        # Retain each structured item and only add genuinely additional text.
        payloads = [line.split(": ", 1)[1] for line in direct_failures if ": " in line]
        additional_failures = [line for line in failures if not any(len(payload) >= 20 and payload in line for payload in payloads)]
        selected = _unique(direct_failures + additional_failures) or concrete[:2]
        unresolved = []
        if projects:
            for key, label in [("calibration_status", "calibration"), ("dataset_status", "curve identity"), ("completeness_status", "full-curve coverage")]:
                status = _aggregate(_status(p, key) for p in projects)
                if status != "PASS":
                    unresolved.append(f"L1 {label} {status}")
        if csvs:
            for key, label in [("axis_status", "independent mapping"), ("x_order_status", "point order"), ("trace_status", "trace confirmation")]:
                status = _aggregate(_status(p, key) for p in csvs)
                if status != "PASS":
                    unresolved.append(f"L2 {label} {status}")
        if unresolved:
            result.append(f"{family}: " + "; ".join(unresolved) + ".")
        for line in selected:
            result.append(f"{family}: {line}")
        if not selected and not unresolved:
            result.append(f"{family}: " + (lines[0] if lines else "No additional finding was recorded."))
    return result


def write_intern_report(intern: str, records: Iterable[Any], path: str | Path, output_root: str | Path, report_detail: str = "compact") -> str:
    """Create an intern overview with one status record per discovered device."""
    if report_detail not in {"reviewer", "compact", "full"}:
        raise ValueError("report_detail must be 'reviewer', 'compact' or 'full'")
    if report_detail == "reviewer":
        from wpd_reports_reviewer import write_reviewer_intern_report
        return write_reviewer_intern_report(intern, records, path, output_root)
    if report_detail == "compact":
        from wpd_reports_compact import write_compact_intern_report
        return write_compact_intern_report(intern, records, path, output_root)
    path = Path(path)
    records = sorted(list(records), key=lambda r: str(_get(r, "device", "")).casefold())
    counts = Counter(_status(r, "overall_status") for r in records)
    doc = _document("Intern digitization verification overview", intern)
    _label(doc, "Intern", intern)
    _p(doc, "The device table identifies every recorded PASS, FAIL and REVIEW. Device findings and report links below locate the detailed calibration, scale, CSV mapping and missing-file evidence.")
    notes = _unique(note for r in records for note in (_get(r, "source_notes", []) or []))
    for note in notes:
        _p(doc, note, small=True)
    _table(doc, ["Devices", "PASS", "FAIL", "REVIEW"], [[len(records), counts.get("PASS", 0), counts.get("FAIL", 0), counts.get("REVIEW", 0)]], [1.775] * 4, 10)
    doc.add_heading("Device results", 1)
    rows = [[_get(r, "device", ""), _status(r, "cc_status"), _status(r, "oc_status"), _status(r, "tc_status"), _status(r, "overall_status")] for r in records]
    _table(doc, ["Device", "CC", "OC", "TC", "Overall"], rows or [["No device records", "", "", "", ""]], [3.5, .9, .9, .9, .9])
    flags = _intern_issue_flags(records)
    if flags:
        doc.add_heading("Source and file availability flags", 1)
        _p(doc, "Counts below identify affected devices. A device can have more than one availability flag, so these counts may overlap.", small=True)
        _table(doc, ["Flag", "Devices"], flags, [6.1, 1.0])
    doc.add_heading("Device findings and detailed reports", 1)
    for n, dr in enumerate(records, 1):
        doc.add_heading(f"Device {n}", 2)
        _p(doc, f"{_get(dr, 'device', '')}  {_status(dr, 'overall_status')}", bold=True)
        flags = _get(dr, "availability_flags", []) or []
        if flags:
            _label(doc, "Availability", "; ".join(str(f) for f in flags), small=True, keep_next=True)
        report = _get(dr, "report_docx", "")
        p = doc.add_paragraph()
        p.paragraph_format.keep_with_next = True
        if report:
            _link(p, report, _basename(report), path)
        else:
            _set_font(p.add_run("Detailed device report path is unavailable."), 9)
        _comments(doc, _overview_reasons(dr))
    return _save(doc, path)
