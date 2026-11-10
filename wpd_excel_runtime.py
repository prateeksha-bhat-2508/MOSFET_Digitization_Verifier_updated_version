"""Local Excel export for the Windows verifier (openpyxl runtime backend).

``write_workbook(records, path)`` consumes the JSON-safe device dictionaries
returned by wpd_runner. Save the workbook beside ``devices`` and
``intern_reports`` so report hyperlinks remain relative when the output folder
is moved. This module reports stored findings; it never upgrades a verification
status or treats a missing folder image as proof of an absent datasheet graph.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import ntpath
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.workbook.properties import CalcProperties
from openpyxl.worksheet.hyperlink import Hyperlink

from wpd_inventory import intern_report_filename


FAMILIES = ("OC", "TC", "CC")
STATUSES = ("PASS", "FAIL", "REVIEW")
HEADER_ROW = 8
FIRST_DATA_ROW = HEADER_ROW + 1
# Stay below Excel's 32,767 UTF-16-unit cell limit, including supplementary
# Unicode characters. Continuation columns preserve the entire recorded text.
TEXT_LIMIT = 30_000
NAVY = "17365D"
TEAL = "DDEBF7"
LIGHT = "F3F6FA"
STATUS_COLORS = {
    "PASS": ("E2F0D9", "375623"),
    "FAIL": ("FCE4D6", "9C0006"),
    "REVIEW": ("FFF2CC", "7F6000"),
}


def _xml_safe(value: Any) -> str:
    """Represent XML-illegal characters visibly instead of losing them."""
    text = "" if value is None else str(value)
    result = []
    for char in text:
        cp = ord(char)
        legal = cp in (9, 10, 13) or 0x20 <= cp <= 0xD7FF or 0xE000 <= cp <= 0xFFFD or 0x10000 <= cp <= 0x10FFFF
        result.append(char if legal else f"[U+{cp:04X}]")
    return "".join(result)


def _split_text(value: Any, limit: int = TEXT_LIMIT) -> list[str]:
    """Split complete, XML-safe text without exceeding Excel's cell limit."""
    if not 2 <= limit <= 32_767:
        raise ValueError("The Excel text limit must be between 2 and 32,767.")
    text = _xml_safe(value)
    if not text:
        return [""]
    parts, current, units = [], [], 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > limit:
            parts.append("".join(current))
            current, units = [], 0
        current.append(char)
        units += size
    if current:
        parts.append("".join(current))
    return parts


def _text_cell(cell, value: Any) -> None:
    """Write literal text, including strings beginning with =, +, -, or @."""
    text = _xml_safe(value)
    if len(text.encode("utf-16-le")) // 2 > 32_767:
        raise ValueError(f"Text at {cell.coordinate} exceeds Excel's limit; use continuation columns.")
    cell.value = text
    # openpyxl otherwise interprets an initial '=' as a formula. Explicit
    # string typing preserves the original identifier or comment safely.
    cell.data_type = "s"
    cell.number_format = "@"


def _sheet_title(intern: str, used: set[str]) -> str:
    clean = re.sub(r"[\\/*?:\[\]]", "_", _xml_safe(intern)).strip(" '") or "Intern"
    base = clean[:31]
    candidate = base
    suffix = 0
    while candidate.casefold() in used:
        suffix += 1
        digest = hashlib.sha256((str(intern) + ":" + str(suffix)).encode("utf-8", errors="backslashreplace")).hexdigest()[:6]
        candidate = clean[:24] + "_" + digest
    used.add(candidate.casefold())
    return candidate


def _quote_sheet(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def _is_windows_absolute(value: str) -> bool:
    return bool(ntpath.splitdrive(value)[0]) or value.startswith("\\\\")


def _relative_report_link(target: Any, workbook: Any) -> str | None:
    """Build local relative links on both Windows and POSIX hosts.

    No HYPERLINK formula is generated and no URL from a data record is accepted.
    A moved standard report tree can be rebound to the workbook's own folder
    when the corresponding new local file actually exists.
    """
    raw = str(target or "").strip()
    book = str(workbook)
    if not raw or not raw.lower().endswith(".docx") or _xml_safe(raw) != raw:
        return None
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", raw) and not re.match(r"^[A-Za-z]:[\\/]", raw):
        return None
    if _is_windows_absolute(book):
        base = ntpath.dirname(book)
        if _is_windows_absolute(raw):
            try:
                return ntpath.relpath(raw, base).replace("\\", "/")
            except ValueError:
                # A different drive needs an existing copy in the moved
                # report tree before a relative hyperlink can be supplied.
                pass
        if not _is_windows_absolute(raw) and not raw.startswith("/"):
            return ntpath.normpath(raw).replace("\\", "/")
    else:
        base = Path(book).expanduser().absolute().parent
        if not _is_windows_absolute(raw):
            target_path = Path(raw.replace("\\", "/")).expanduser()
            if not target_path.is_absolute():
                return target_path.as_posix()
            relative = os.path.relpath(target_path, base)
            if not relative.startswith(".." + os.sep):
                return relative.replace(os.sep, "/")
            if target_path.is_file():
                return relative.replace(os.sep, "/")
    # Rebase a path from a prior machine only against an existing standard
    # output file. The fixed depths disambiguate an intern named 'devices'.
    parts = raw.replace("\\", "/").split("/")
    tails = []
    if len(parts) >= 4 and parts[-4] == "devices":
        tails.append(parts[-4:])
    if len(parts) >= 2 and parts[-2] == "intern_reports":
        tails.append(parts[-2:])
    for tail in tails:
        if (Path(book).expanduser().absolute().parent.joinpath(*tail)).is_file():
            return "/".join(tail)
    return None


def _as_lines(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "\n".join(str(item) for item in value)
    return str(value)


def _status(record: dict, key: str, notes: list[str]) -> str:
    raw = str(record.get(key, "REVIEW")).upper().strip()
    if raw not in STATUSES:
        notes.append(f"The stored {key} value was {raw!r}; REVIEW is displayed because this status is not recognized.")
        return "REVIEW"
    return raw


def _availability(record: dict, key: str) -> str:
    value = (record.get("availability") or {}).get(key)
    if value is None and key == "DATASHEET" and record.get("datasheets"):
        return "PRESENT"
    return str(value).upper() if value is not None else "NOT_CHECKED"


def _family_comment(record: dict, family: str) -> str:
    saved = (record.get("family_comments") or {}).get(family)
    if saved:
        return _as_lines(saved)
    parts = []
    for item in record.get("requirements", []) or []:
        if item.get("family") == family and item.get("status") != "PASS":
            parts.append(f"{item.get('status', 'REVIEW')}: {item.get('requirement', '')}. {item.get('comment', '')}")
    for field, label, filename in (("projects", "Level 1", "project"), ("level2", "Level 2", "csv_file")):
        for item in record.get(field, []) or []:
            if item.get("family") == family:
                parts.extend(f"{label} {item.get(filename, '')}: {comment}" for comment in item.get("comments", []) or [])
    for item in record.get("files", []) or []:
        if item.get("family") == family and item.get("status") in {"FAIL", "REVIEW"}:
            parts.append(f"Filename {item.get('current_filename', '')}: {item.get('comment', '')}")
    return "\n".join(dict.fromkeys(parts)) or "No detailed family comment was recorded. Consult the individual Word report."


def _intern_report(records: list[dict], intern: str, workbook: Path) -> str:
    for record in records:
        for key in ("intern_report_docx", "intern_report"):
            if record.get(key):
                return str(record[key])
    planned = workbook.parent / "intern_reports" / intern_report_filename(intern)
    return str(planned) if planned.is_file() else ""


# Column keys are stable; formulas below resolve their coordinates by key.
BASE_COLUMNS = [
    ("device", "Device", 27),
    ("oc_status", "OC", 11), ("tc_status", "TC", 11),
    ("cc_status", "CC", 11), ("overall_status", "Overall", 12),
    ("DATASHEET", "Datasheet PDF", 18),
    *[(f"{family}_{kind}", f"{family} {label}", 16) for family in FAMILIES for kind, label in (("IMAGE", "image"), ("TAR", "WPD TAR"), ("CSV", "CSV"))],
    *[(f"{family}_comment", f"{family} reason / comment", 68) for family in FAMILIES],
    ("availability_flags", "Availability flags", 40),
    ("report_docx", "Individual Word report", 36),
    ("intern_report_docx", "Intern Word overview", 36),
    ("source_snapshot", "Source snapshot", 30),
    ("device_relpath", "Device source folder", 45),
    ("datasheets", "Datasheet filenames", 42),
    ("any_graph_missing", "Any source image missing", 21),
    ("runtime_error", "Verification runtime error", 42),
    ("warnings", "Additional recorded comments", 56),
    ("source_notes", "Scope / source notes", 56),
    ("export_notes", "Workbook export notes", 48),
]


def _row_values(record: dict, intern_report: str, workbook: Path) -> tuple[dict[str, str], dict[str, str]]:
    notes: list[str] = []
    values = {key: _status(record, key, notes) for key in ("oc_status", "tc_status", "cc_status", "overall_status")}
    values["device"] = str(record.get("device") or "Device name not recorded")
    for key in ["DATASHEET"] + [f"{f}_{k}" for f in FAMILIES for k in ("IMAGE", "TAR", "CSV")]:
        values[key] = _availability(record, key)
    for family in FAMILIES:
        from wpd_reports_reviewer import family_review_comment
        values[f"{family}_comment"] = family_review_comment(record, family)
    flags = _as_lines(record.get("availability_flags"))
    values["availability_flags"] = flags or ("None recorded" if record.get("availability") else "Availability checks not recorded")
    images = [values[f"{f}_IMAGE"] for f in FAMILIES]
    values["any_graph_missing"] = "YES" if "MISSING" in images else ("NO" if all(v == "PRESENT" for v in images) else "NOT_CHECKED")
    values["source_snapshot"] = str(record.get("source_snapshot") or "Not specified")
    values["device_relpath"] = str(record.get("device_relpath") or record.get("path") or "Not recorded")
    values["datasheets"] = _as_lines(record.get("datasheets")) or "None recorded"
    values["runtime_error"] = str(record.get("fatal_error") or "")
    values["warnings"] = _as_lines(record.get("warnings"))
    values["source_notes"] = _as_lines(record.get("source_notes"))
    links = {}
    for key, raw in (("report_docx", record.get("report_docx")), ("intern_report_docx", intern_report)):
        link = _relative_report_link(raw, workbook)
        if link:
            values[key] = "Open device report" if key == "report_docx" else "Open intern overview"
            links[key] = link
        else:
            values[key] = "Report link unavailable"
            if raw:
                notes.append(f"Could not form a relative local Word link for {key}: {raw}")
    for key, value in values.items():
        if _xml_safe(value) != value:
            notes.append(f"XML-illegal characters in {key} are shown as [U+XXXX]; no text was silently discarded.")
    values["export_notes"] = "\n".join(notes)
    return values, links


def _header(ws, row: int, labels: list[str]) -> None:
    for col, label in enumerate(labels, 1):
        cell = ws.cell(row, col)
        _text_cell(cell, label)
        cell.fill = PatternFill("solid", fgColor=NAVY)
        cell.font = Font(name="Calibri", size=10, color="FFFFFF", bold=True)
        cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    ws.row_dimensions[row].height = 34


def _banner(ws, title: str, subtitle: str, width: int) -> None:
    ws.sheet_view.showGridLines = False
    ws.sheet_view.zoomScale = 85
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=min(width, 12))
    _text_cell(ws.cell(1, 1), title)
    ws.cell(1, 1).font = Font(name="Calibri", size=20, color=NAVY, bold=True)
    ws.row_dimensions[1].height = 34
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=min(width, 12))
    _text_cell(ws.cell(2, 1), subtitle)
    ws.cell(2, 1).font = Font(name="Calibri", size=10, color="526478")
    ws.cell(2, 1).alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[2].height = 30


def _status_format(ws, cell_range: str) -> None:
    for status, (bg, fg) in STATUS_COLORS.items():
        ws.conditional_formatting.add(cell_range, CellIsRule(operator="equal", formula=[f'"{status}"'],
            fill=PatternFill("solid", fgColor=bg), font=Font(color=fg, bold=True)))


def _intern_sheet(wb, intern: str, title: str, records: list[dict], workbook: Path) -> dict:
    ws = wb.create_sheet(title)
    intern_report = _intern_report(records, intern, workbook)
    prepared = [_row_values(record, intern_report, workbook) for record in records]
    columns = [(key, label, width, 0) for key, label, width in BASE_COLUMNS]
    split_rows = []
    counts = {key: 1 for key, _, _ in BASE_COLUMNS}
    for values, links in prepared:
        chunks = {key: _split_text(value) for key, value in values.items()}
        split_rows.append((chunks, links))
        for key, parts in chunks.items():
            counts[key] = max(counts.get(key, 1), len(parts))
    # Continuations are appended rather than shifting the main status columns.
    for key, label, width in BASE_COLUMNS:
        for number in range(1, counts[key]):
            columns.append((key, f"{label} (continued {number + 1}/{counts[key]})", width, number))
    if len(columns) > 16_384:
        raise ValueError(f"Comments for {intern!r} exceed Excel's column limit. No partial workbook was saved.")
    if len(records) > 1_048_576 - HEADER_ROW:
        raise ValueError(f"The number of devices for {intern!r} exceeds one Excel worksheet.")
    _banner(ws, "WPD verification | " + intern, "Each row represents a device folder. Full reasons continue in extra columns when required; all reports use the recorded evidence.", len(columns))
    indexes = {key: n for n, (key, _, _, part) in enumerate(columns, 1) if part == 0}
    last = max(FIRST_DATA_ROW, HEADER_ROW + len(records))
    def area(key):
        col = get_column_letter(indexes[key])
        return f"${col}${FIRST_DATA_ROW}:${col}${last}"
    for column, label, formula in (
        (1, "Devices", f"=COUNTA({area('device')})"),
        (3, "PASS", f'=COUNTIFS({area("overall_status")},"PASS")'),
        (5, "FAIL", f'=COUNTIFS({area("overall_status")},"FAIL")'),
        (7, "REVIEW", f'=COUNTIFS({area("overall_status")},"REVIEW")'),
    ):
        _text_cell(ws.cell(3, column), label)
        ws.cell(3, column).font = Font(name="Calibri", bold=True, color=NAVY)
        ws.cell(3, column + 1, formula).number_format = "0"
        ws.cell(3, column + 1).font = Font(name="Calibri", size=15, bold=True, color=NAVY)
        ws.cell(3, column + 1).fill = PatternFill("solid", fgColor=TEAL)
    _text_cell(ws.cell(4, 1), "Missing PDF")
    ws.cell(4, 2, f'=COUNTIFS({area("DATASHEET")},"MISSING")')
    _text_cell(ws.cell(4, 3), "Image missing")
    ws.cell(4, 4, f'=COUNTIFS({area("any_graph_missing")},"YES")')
    _text_cell(ws.cell(4, 5), "Run errors")
    ws.cell(4, 6, f'=COUNTIFS({area("runtime_error")},"?*")')
    for cell in ws[4][:6]:
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        cell.font = Font(name="Calibri", size=10, color=NAVY)
    ws.row_dimensions[4].height = 30
    for row, note in (
        (5, "PASS = recorded checks passed; FAIL = a supported error; REVIEW = evidence is incomplete or needs a person to confirm."),
        (6, "PRESENT / MISSING describe files in the folder. Open a Word report for detailed Level 1 and Level 2 evidence."),
        (7, "Source snapshots and source-folder paths are recorded on each row. Very long text is preserved in continuation columns at the right."),
    ):
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=min(len(columns), 15))
        _text_cell(ws.cell(row, 1), note)
        ws.cell(row, 1).font = Font(name="Calibri", size=10, color="526478")
        ws.cell(row, 1).alignment = Alignment(wrap_text=True)
        ws.row_dimensions[row].height = 26
    _header(ws, HEADER_ROW, [label for _, label, _, _ in columns])
    thin = Side(style="hair", color="DAE2EC")
    for row, (chunks, links) in enumerate(split_rows, FIRST_DATA_ROW):
        ws.row_dimensions[row].height = 70
        for col, (key, _, _, part) in enumerate(columns, 1):
            cell = ws.cell(row, col)
            pieces = chunks.get(key, [""])
            _text_cell(cell, pieces[part] if part < len(pieces) else "")
            cell.font = Font(name="Calibri", size=10, color="1F2937")
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            cell.border = Border(bottom=thin)
            if (row - FIRST_DATA_ROW) % 2:
                cell.fill = PatternFill("solid", fgColor=LIGHT)
            if key in links and part == 0:
                cell.hyperlink = links[key]
                cell.font = Font(name="Calibri", size=10, color="0563C1", underline="single")
            if key.endswith("_status"):
                cell.alignment = Alignment(horizontal="center", vertical="top", wrap_text=True)
            if key in {"DATASHEET"} | {f"{f}_{k}" for f in FAMILIES for k in ("IMAGE", "TAR", "CSV")}:
                color = {"MISSING": "FCE4D6", "PRESENT": "E2F0D9"}.get(cell.value, "FFF2CC")
                cell.fill = PatternFill("solid", fgColor=color)
    for col, (_, _, width, _) in enumerate(columns, 1):
        ws.column_dimensions[get_column_letter(col)].width = width
    if records:
        _status_format(ws, f"B{FIRST_DATA_ROW}:E{last}")
    ws.auto_filter.ref = f"A{HEADER_ROW}:{get_column_letter(len(columns))}{last}"
    ws.freeze_panes = f"F{FIRST_DATA_ROW}"
    ws.print_title_rows = f"1:{HEADER_ROW}"
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A3
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.outlinePr.summaryRight = False
    if len(columns) > len(BASE_COLUMNS):
        ws.column_dimensions.group(get_column_letter(len(BASE_COLUMNS) + 1), get_column_letter(len(columns)), outline_level=1, hidden=False)
    return {"sheet": title, "last": last, "indexes": indexes, "intern_report": intern_report}


def write_workbook(records, path) -> str:
    """Write the runtime master workbook; return its path after an atomic save.

    Only interns present in records receive a sheet. Complete comments remain
    available through continuation columns and the Word report links. Formula
    totals are recalculated by Excel on opening; openpyxl does not cache them.
    """
    records = list(records)
    if any(not isinstance(record, dict) for record in records):
        raise TypeError("write_workbook expects serialized device dictionaries from wpd_runner.")
    destination = Path(path).expanduser().absolute()
    if destination.suffix.lower() != ".xlsx":
        raise ValueError("The master workbook path must end with .xlsx.")
    grouped = defaultdict(list)
    for record in records:
        intern = str(record.get("intern") or "Intern not recorded")
        grouped[intern].append(record)
    wb = Workbook()
    overview = wb.active
    overview.title = "Overview"
    wb.properties.title = "WPD verification master"
    wb.properties.subject = "Device status, source availability and detailed report index"
    wb.properties.creator = "WPD Verifier"
    wb.properties.description = "Stored verification results. Source files remain unchanged; missing folder images do not establish missing datasheet graphs."
    wb.calculation = CalcProperties(calcMode="auto", fullCalcOnLoad=True, forceFullCalc=True)
    used = {"overview", "history"}
    sheet_info = []
    for intern in sorted(grouped, key=lambda name: (name.casefold(), name)):
        rows = sorted(grouped[intern], key=lambda r: (str(r.get("device", "")).casefold(), str(r.get("device_relpath", ""))))
        info = _intern_sheet(wb, intern, _sheet_title(intern, used), rows, destination)
        sheet_info.append((intern, info))
    _banner(overview, "WPD verification master", "One worksheet per recorded intern. Counts use the device rows; use the intern sheets and Word reports for the reasons behind each status.", 11)
    for row, note in (
        (3, "PASS = applicable recorded checks passed. FAIL = supported error. REVIEW = unresolved evidence, source availability or human review."),
        (4, "Missing source files are listed separately. The verifier does not check whether each characteristic appears inside a datasheet PDF."),
        (5, "Relative Word links work when this workbook, devices and intern_reports remain together inside the output folder."),
        (6, "Comments are complete; text over Excel's cell limit continues in additional columns. XML-illegal characters are shown visibly as [U+XXXX]."),
        (7, "Generated UTC: " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M") + ". Excel recalculates the formula totals when the workbook is opened."),
    ):
        overview.merge_cells(start_row=row, start_column=1, end_row=row, end_column=11)
        _text_cell(overview.cell(row, 1), note)
        overview.cell(row, 1).font = Font(name="Calibri", size=10, color="526478")
        overview.cell(row, 1).alignment = Alignment(wrap_text=True)
        overview.row_dimensions[row].height = 28
    labels = ["Intern", "Devices", "PASS", "FAIL", "REVIEW", "Missing PDF", "Any source image missing", "Run errors", "Device worksheet", "Intern Word overview", "Source snapshots"]
    _header(overview, HEADER_ROW, labels)
    for row, (intern, info) in enumerate(sheet_info, FIRST_DATA_ROW):
        title = info["sheet"]
        sheet = _quote_sheet(title)
        _text_cell(overview.cell(row, 1), intern)
        formulas = {2: f"={sheet}!B3", 3: f"={sheet}!D3", 4: f"={sheet}!F3", 5: f"={sheet}!H3",
                    6: f"={sheet}!B4", 7: f"={sheet}!D4", 8: f"={sheet}!F4"}
        for col, formula in formulas.items():
            overview.cell(row, col, formula).number_format = "0"
        _text_cell(overview.cell(row, 9), "Open device worksheet")
        overview.cell(row, 9).hyperlink = Hyperlink(ref=overview.cell(row, 9).coordinate, location=f"{sheet}!A1", display="Open device worksheet")
        overview.cell(row, 9).font = Font(color="0563C1", underline="single")
        link = _relative_report_link(info["intern_report"], destination)
        _text_cell(overview.cell(row, 10), "Open intern overview" if link else "Report link unavailable")
        if link:
            overview.cell(row, 10).hyperlink = link
            overview.cell(row, 10).font = Font(color="0563C1", underline="single")
        snapshots = list(dict.fromkeys(str(r.get("source_snapshot") or "Not specified") for r in grouped[intern]))
        snapshot_parts = _split_text("\n".join(snapshots))
        _text_cell(overview.cell(row, 11), snapshot_parts[0] if len(snapshot_parts) == 1 else "Multiple long source descriptions; full source snapshots are preserved in the device worksheet.")
        for cell in overview[row][:11]:
            cell.alignment = Alignment(wrap_text=True, vertical="center")
            if row % 2:
                cell.fill = PatternFill("solid", fgColor=LIGHT)
        overview.row_dimensions[row].height = 48
    last = HEADER_ROW + len(sheet_info)
    total_row = max(FIRST_DATA_ROW, last + 1)
    _text_cell(overview.cell(total_row, 1), "TOTAL")
    for col in range(2, 9):
        letter = get_column_letter(col)
        overview.cell(total_row, col, f"=SUM({letter}{FIRST_DATA_ROW}:{letter}{last})" if sheet_info else 0).number_format = "0"
    for cell in overview[total_row][:11]:
        cell.fill = PatternFill("solid", fgColor=NAVY)
        cell.font = Font(name="Calibri", bold=True, color="FFFFFF")
    for col, width in enumerate((29, 13, 12, 12, 13, 16, 23, 15, 28, 30, 43), 1):
        overview.column_dimensions[get_column_letter(col)].width = width
    overview.auto_filter.ref = f"A{HEADER_ROW}:K{max(HEADER_ROW, last)}"
    overview.freeze_panes = f"B{FIRST_DATA_ROW}"
    overview.print_title_rows = f"1:{HEADER_ROW}"
    overview.print_options.horizontalCentered = True
    overview.page_setup.orientation = "landscape"
    overview.page_setup.paperSize = overview.PAPERSIZE_A3
    overview.sheet_properties.pageSetUpPr.fitToPage = True
    overview.page_setup.fitToWidth = 1
    overview.page_setup.fitToHeight = 1
    overview.print_area = f"A1:K{total_row}"
    wb.active = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".wpd_master_", suffix=".xlsx", dir=destination.parent)
    os.close(descriptor)
    try:
        wb.save(temporary)
        os.replace(temporary, destination)
    finally:
        wb.close()
        if os.path.exists(temporary):
            os.unlink(temporary)
    return str(destination)
