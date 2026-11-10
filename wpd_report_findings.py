"""Presentation-only findings for compact WPD reports.

The saved records remain authoritative.  This module performs no verification,
reads no files, and never mutates an input record.  Only recognised repeated
messages are grouped or replaced by a concise explanation.  Unknown comments
and explicit failures are retained without a length limit.  Full diagnostics
remain in the original records and the full report.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any


FAMILIES = ("CC", "OC", "TC")
_SUCCESS = {"PASS", "OK"}
_ACTIONS = {
    "availability": "Check the device folder and add the identified missing file if the source provides it.",
    "filename": "Check the part number, graph label and operating condition, then correct the filename or dataset label if needed.",
    "export": "Open the named WPD dataset and export it again, or confirm the intended CSV precision.",
    "calibration": "Check the printed tick and saved WPD setting; change the calibration only when an error is confirmed.",
    "curve": "Compare the points with the named source curve and check the full useful range.",
    "mapping": "Check the source axis labels or supply a clearer graph image, then repeat the CSV comparison.",
    "runtime": "Resolve the reported execution or file-access problem and rerun the affected check.",
    "other": "Check the named item against the source and resolve the issue described here.",
}
_CURVE_MESSAGES = {
    "Dataset-to-source-curve identity is not automatically established by dataset names; inspect every labelled overlay.",
    "Correct trace identity, every-point alignment and the full useful source range still require visual confirmation.",
    "Full curve identity and coverage need visual review against the legend and source trace. Ink proximity and the number of samples do not establish PASS.",
    "XY graph type confirms the coordinate-system setting only; physical axis quantities, units and graph-family identity still require source confirmation.",
    "XY axes describe coordinate geometry; physical OC/TC/CC family still requires source-label confirmation.",
}
_AXIS_MESSAGES = {
    "Independent tick evidence is insufficient to confirm marker placement and its assigned value.",
    "At least three distinct, unambiguous labelled ticks are needed to establish scale independently.",
    "A complete plot rectangle with space for external tick labels could not be established from the image.",
    "No CSV overlay was presented as verified because a reliable independent image mapping could not be established.",
    "No confidently read tick matches this marker/value pair. An interpolated axis estimate is shown only for review; inspect the intended minor tick/grid line.",
}
_MISSING_SOURCE_NUMERIC_NOTE = "CSV numeric content was checked independently of source availability. Source-axis mapping, curve identity and source-trace agreement remain REVIEW because no unambiguous standalone source image is selected."
_ROUTINE_EXACT = {
    "Independent calibration uses observed tick values and detected tick/grid positions. No CSV range or WPD calibration is used.",
    "All applicable recorded checks passed.",
    "No additional finding was recorded.",
}
_ROUTINE_PATTERNS = tuple(re.compile(x) for x in (
    r"Read \d+ numeric rows, preserving the first numeric row at line \d+\. First point \[[^\]]*\]; last point \[[^\]]*\]\.",
    r"Blue marker: [A-Za-z_]+; pixel coordinates \([^)]*\); data coordinates \([^)]*\)\. The axis coefficients are not shifted to this marker\.",
    r"Distance to any visible source ink: median [-+\d.eE]+ px, 95th percentile [-+\d.eE]+ px\. Gridlines, labels, and other curves can also produce small distances\.",
    r"Generic-ink proximity p95=[-+\d.eE]+ px is diagnostic only: grid lines, text and other curves can also match\.",
    r"Rejected nonnumeric OCR text: .+\. No numeric value was inferred\. Full token positions remain in device_result\.json\.",
))


def _get(record: Any, key: str, default: Any = None) -> Any:
    return record.get(key, default) if isinstance(record, dict) else getattr(record, key, default)


def _status(record: Any, key: str = "status", default: str = "REVIEW") -> str:
    return str(_get(record, key, default) or default).upper()


def _items(value: Any) -> list:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def _unique(values: Any) -> list[str]:
    return list(dict.fromkeys(str(v).strip() for v in _items(values) if v is not None and str(v).strip()))


def _family(value: Any) -> str:
    value = str(value or "General").strip()
    return "General" if value.upper() in {"GENERAL", "UNKNOWN", "", "NONE"} else value.upper()


def _combined_status(record: Any, fields: tuple[str, ...], fallback: str = "REVIEW") -> str:
    """Select the severity of existing component findings, not a new verdict."""
    values = [_status(record, key, "") for key in fields if _get(record, key) is not None]
    if "FAIL" in values:
        return "FAIL"
    if "REVIEW" in values:
        return "REVIEW"
    if "WARNING" in values:
        return "WARNING"
    return values[0] if values else fallback


def folder_presence(value: Any) -> str:
    """Describe identified folder files, never infer datasheet graph coverage."""
    return {
        "PRESENT": "Present", "MISSING": "Absent", "ABSENT": "Absent",
        "AMBIGUOUS": "Present; selection unclear", "NOT_AVAILABLE": "Absent",
        "NOT_CONFIRMED": "Not confirmed", "NOT_CHECKED": "Not checked",
        "": "Not recorded", "NONE": "Not recorded",
    }.get(str(value or "").upper(), str(value))


_PLAIN_FILE_COMMENTS = {
    "Supported image extension and expected source-image stem.": "Image name follows the expected convention.",
    "Filename follows the expected convention. Curve identity is checked separately.": "Name follows the expected convention.",
    "PDF presence is recorded. Filename alone does not verify its device identity or graph coverage.": "Open the PDF and confirm its part number matches this device.",
    "Filename convention or operating-condition label needs review; this alone does not prove wrong device data.": "Confirm the graph and operating condition, then use the expected filename if appropriate.",
    "Graph family is not identifiable from the image filename.": "Open the image, identify CC, OC or TC, and label it clearly.",
    "Cannot identify CC OC or TC from the filename.": "The filename does not identify CC, OC or TC. Check the contents and label the file.",
    "Source image has a nonstandard name. Confirm the graph family and rename the stem if appropriate.": "Confirm the graph family, then use the expected image name if appropriate.",
    "Negative-temperature data is excluded from the requested verification scope.": "Outside the requested temperature range; no correction is required for this exclusion.",
}


def plain_file_comment(audit: Any) -> str:
    """Translate recognised file-audit prose and retain unknown text in full."""
    text = str(_get(audit, "comment", "") or "").strip()
    if text in _PLAIN_FILE_COMMENTS:
        return _PLAIN_FILE_COMMENTS[text]
    if not text:
        return "Name follows the expected convention." if _status(audit) in _SUCCESS else "No explanation was saved; check the file contents and expected name."
    return _plain_known_text(text)


def _plain_known_text(text: str) -> str:
    """Replace only known complete clauses; keep every unrecognised remainder."""
    replacements = {
        "Labels provide one compatible curve association. An unspecified temperature is not assumed to be 25 C; this association does not establish source-curve identity.": "One CSV matches this dataset's labels. Confirm the physical curve separately.",
        "The CSV-to-WPD association is ambiguous or absent; numeric differences are not graded as export errors without an unambiguous pairing.": "The intended CSV for this WPD dataset is unclear or absent. Confirm which file belongs to the dataset.",
        "CSV-to-WPD identity is not established by unambiguous labels.": "The CSV labels do not identify one WPD dataset. Confirm the intended dataset.",
        "This is a potential filename or dataset-label correction, not proof of physical curve identity; no rename was made.": "Check whether the CSV or dataset has the wrong label before renaming it.",
        "Check swapped or incorrect filename/dataset labels; no file was renamed.": "Check for swapped CSV or dataset labels.",
        "WPD has no saved numeric value sequence; pixel coordinates alone do not establish CSV export consistency.": "Saved numeric WPD values are unavailable, so the CSV export cannot be confirmed. Save and export the intended WPD dataset.",
        "Some WPD points have no saved numeric X Y values; the full export cannot be confirmed.": "Some WPD points have no saved X and Y values. Save and export the complete dataset.",
        "WPD saved numeric values contain an unreadable entry; export consistency requires review.": "A saved WPD value is unreadable. Check that entry and export the dataset again.",
        "WPD saved numeric values contain a non-finite entry; export consistency requires review.": "A saved WPD value is not a finite number. Check that entry and export the dataset again.",
        "CSV has additional numeric columns; the intended paired X Y export is ambiguous.": "The CSV has more than two numeric columns. Identify the intended X and Y columns or export one dataset per file.",
        "X values are not ascending. Inspect point order; this alone does not establish incorrect curve coordinates. Source data were not sorted.": "X values are not in ascending order. Check the point order before using the CSV.",
        "Datasheet is not available in the folder. This is an availability flag, not a finding of wrong digitization.": "The device folder has no datasheet PDF. Add it if available so the source can be checked.",
        "No digitization files are available. Check graph availability in the datasheet before classifying missing extraction as an error.": "The folder has no source images, WPD projects or CSV files. Check the datasheet and add the available digitization files.",
        "Project temperature is not explicit; a combined TC project may contain 25 C, which was not assumed.": "The project does not state its temperature. Check its datasets and label the 25 C data if present.",
        "A 25 C WPD project is not identified. Different-temperature or unspecified source evidence does not prove that a 25 C source graph is available.": "No 25 C WPD project is identified. Check whether the source provides a 25 C graph before adding an extraction.",
        "CSV temperature is not stated; 25 C was not assumed.": "The CSV does not state its temperature. Confirm the condition and label it.",
        "Higher-temperature extraction is optional. Its absence does not change the verification status.": "The optional 150/175 C CSV is absent. No mandatory correction is required.",
    }
    for original, replacement in replacements.items():
        text = text.replace(original, replacement)
    text = re.sub(
        r"Only one CSV is compatible, but the default WPD dataset and project do not declare a temperature\. The CSV's ([-+\d.]+) C identity is therefore unconfirmed; uniqueness alone does not establish that this is the intended export\.",
        r"The WPD dataset does not state its temperature, but the CSV is labelled \1 C. Confirm that they describe the same condition.", text)
    text = re.sub(
        r"Filename/dataset prefix (.+?) differs from device-folder identity (.+?)\. Confirm whether this is a naming error or a different device; numerical and source identity are not inferred from the mismatch\.",
        r"The file or dataset names part \1, but the folder names part \2. Check the source part number before correcting the name or moving the file.", text)
    text = re.sub(
        r"No (CC|OC|TC) WPD project or source graph is available; establish source availability first\.",
        r"The folder has no identified \1 WPD project or source image. Check for the graph before adding an extraction.", text)
    text = re.sub(
        r"(Coss|Crss) CSV is not available\. A generic CC graph/project does not establish that this particular physical curve is supplied\. Confirm individual source-curve availability or a populated matching WPD dataset before assigning a missing-extraction error\.",
        r"The \1 CSV is absent. Check whether the source or WPD project contains this curve before adding an export.", text)
    text = text.replace("This is an export-availability finding, not proof of source-curve correctness.",
                        "Export the named dataset and confirm its source curve.")
    text = text.replace("Numerical discrepancy evidence is retained below for review; it cannot establish an export error until the intended dataset/temperature association is confirmed.",
                        "Confirm the dataset and temperature before judging the numeric difference.")
    text = re.sub(
        r"([XY]) calibration pair differs by [-+\d.eE]+ ?px in its orthogonal direction, exceeding (?:the )?[-+\d.eE]+ ?px orientation screening allowance\."
        r"(?: The independently detected source frame is upright\.)?"
        r"(?: With WPD rotation enabled, confirm that this tilt is intended; correct paired marker alignment if it is accidental\.)?",
        lambda match: ("X1 and X2 are not aligned horizontally as expected; check their placement and the image rotation."
                       if match[1] == "X" else "Y1 and Y2 are not aligned vertically as expected; check their placement and the image rotation."), text)
    return text.strip()


_EXPORT_BASE = re.compile(
    r"CSV rows=(\d+); saved WPD rows=(\d+); strict paired XY matches=(\d+)\. "
    r"Full multiset comparison allows reordered rows and uses rtol=[-+\d.eE]+, atol=[-+\d.eE]+ per coordinate; duplicate multiplicity is preserved\. "
    r"CSV first/last rows are retained\. This checks export consistency only, not datasheet truth\.")
_EXPORT_ROUNDING = re.compile(
    r"CSV_ROUNDING_REVIEW: all \d+ paired rows agree when the saved values are rounded within half of each CSV coordinate's last written decimal place, with floating-point roundoff only\. "
    r"Recorded CSV precision explains the differences, but its acceptability has not been established; confirm the intended export precision\. "
    r"The strict tolerance remains unchanged, and no axis range is used\. "
    r"Rounding examples \(CSV tokens, saved values and decimal quantum\)=\[[^\n]*?\]\.(?=\s|$)")
_EXPORT_SHORT_COUNTS = re.compile(r"CSV rows=(\d+); saved WPD rows=(\d+)[.;]\s*")
_EXPORT_SHORT_ROUNDING = re.compile(r"CSV_ROUNDING_REVIEW: all \d+ paired rows agree\.")
_EXPORT_ROUNDING_EXAMPLES = re.compile(r"Rounding examples \(CSV tokens, saved values and decimal quantum\)=\[[^\n]*?\]\.(?=\s|$)")


def _export_summary(req: Any) -> tuple[str, str]:
    text = str(_get(req, "comment", "") or "").strip()
    base = _EXPORT_BASE.search(text)
    status, code = _status(req), str(_get(req, "issue_code", ""))
    if not base:
        # Earlier saved reports used this exact shorter diagnostic template.
        # Recognise its category/code and clauses; never discard an unknown
        # suffix simply because a comment mentions rounding.
        counts = _EXPORT_SHORT_COUNTS.search(text)
        if (str(_get(req, "category", "")) == "CSV export consistency"
                and code == "CSV_ROUNDING_REVIEW" and status == "REVIEW"
                and counts and _EXPORT_SHORT_ROUNDING.search(text)):
            remainder = _EXPORT_SHORT_COUNTS.sub("", text)
            remainder = _EXPORT_SHORT_ROUNDING.sub("", remainder)
            remainder = _EXPORT_ROUNDING_EXAMPLES.sub("", remainder)
            return ("The CSV agrees with the saved WPD values only after rounding to the CSV's written precision. Confirm whether that precision is acceptable.",
                    _plain_known_text(remainder))
        return "", _plain_known_text(text)
    if code == "CSV_ROUNDING_REVIEW":
        summary = "The CSV agrees with the saved WPD values only after rounding to the CSV's written precision. Confirm whether that precision is acceptable."
    elif code == "CSV_WPD_IDENTITY_UNCONFIRMED":
        summary = "The CSV values differ from the saved WPD values. The dataset or temperature pairing must be confirmed before this can be judged as an export error."
    elif status in _SUCCESS:
        summary = "CSV values agree with the saved WPD dataset."
    elif base[1] != base[2]:
        summary = "The CSV and saved WPD dataset contain different numbers of points. Check that the complete intended dataset was exported."
    else:
        summary = "Some CSV values do not match the saved WPD dataset. Check the intended dataset and export it again."
    remainder = text[:base.start()] + text[base.end():]
    remainder = _EXPORT_ROUNDING.sub("", remainder)
    remainder = _EXPORT_SHORT_ROUNDING.sub("", remainder)
    remainder = _EXPORT_ROUNDING_EXAMPLES.sub("", remainder)
    remainder = re.sub(r"Unmatched CSV examples=\[[^\n]*?\]; unmatched WPD examples=\[[^\n]*?\]\.(?=\s|$)", "", remainder)
    return summary, _plain_known_text(remainder)


def plain_requirement_comment(req: Any) -> str:
    """Plain explanation of a saved requirement; never changes its status."""
    if str(_get(req, "category", "")) == "CSV export consistency":
        summary, remainder = _export_summary(req)
        if summary or remainder:
            return " ".join(part for part in (summary, remainder) if part)
    text = _plain_known_text(str(_get(req, "comment", "") or ""))
    if text:
        return text
    return "The recorded requirement is satisfied." if _status(req) in _SUCCESS else "No explanation was saved. Check the required item against the files found."


def marker_reason(check: Any) -> str:
    """Explain a saved marker result without turning estimates into offsets."""
    text = str(_get(check, "comment", "") or "").strip()
    remaining = text
    reasons = []
    replacements = {
        "Required calibration marker is missing.": "This marker is missing. Place it on the intended source tick.",
        "Marker pixel coordinates are missing or non-finite.": "The marker has no valid saved position. Set it again in WPD.",
        "Marker is outside the embedded source image.": "The saved marker is outside the image. Move it to the intended source tick.",
        "Stored calibration value is missing, non-numeric or non-finite.": "The saved value is missing or invalid. Enter the printed tick value.",
        "A logarithmic WPD axis has a non-positive calibration value.": "The saved value is zero or negative on a logarithmic axis. Check the scale and printed tick value.",
        "Independent tick evidence is insufficient to confirm marker placement and its assigned value.": "The source tick could not be confirmed reliably. Check the marker and printed tick value.",
        "No confidently read tick matches this marker/value pair. An interpolated axis estimate is shown only for review; inspect the intended minor tick/grid line.": "The intended source tick was not identified confidently. Check the minor tick or grid line and its value; the shown value is an estimate.",
        "The marker is outside the independently detected plot frame in its orthogonal direction; confirm that it represents an intended tick/grid reference rather than label text.": "The marker appears outside the detected plot area. Confirm it marks a tick or grid line rather than label text.",
    }
    for original, replacement in replacements.items():
        if original in remaining:
            reasons.append(replacement)
            remaining = remaining.replace(original, "")
    uncertainty = re.compile(r"The stored/read numbers disagree, but their projected separation \(([-+\d.eE]+) px\) is within the ([-+\d.eE]+) px source-reading uncertainty\. Confirm the printed digits and stored number visually; this is not a PASS or a confirmed calibration error\.")
    if uncertainty.search(remaining):
        reasons.append("The saved value and read tick differ, but the image is not precise enough to confirm an error. Check the printed digits.")
        remaining = uncertainty.sub("", remaining)
    observed = re.compile(r"Marker is ([-+\d.eE]+) px from an independently observed [XY] tick/grid coordinate\. OCR-read tick value=[-+\d.eE]+; stored value=[-+\d.eE]+\.")
    # Use the saved matched-tick fields, not a projected interpolation error.
    if _get(check, "source_tick_pixel") is not None and _get(check, "value_error") is not None:
        matched = observed.search(remaining)
        if matched:
            if _status(check, "value_status") == "FAIL":
                reasons.append(f"Saved value {_get(check, 'assigned')} disagrees with read tick {_get(check, 'independent_value')}. Confirm the printed value and correct the saved value if needed.")
            remaining = observed.sub("", remaining)
    displaced = re.compile(r"Stored value (.+?) appears on the source axis at pixel (.+?); marker coordinate is (.+?) \(([-+\d.eE]+) px away\)\. The clicked position is not on a confidently matched labelled tick\.")
    match = displaced.search(remaining)
    if match and _get(check, "source_tick_pixel") is not None:
        reasons.append(f"The marker is {match[4]} pixels from the tick read as the saved value. Confirm that tick, then correct the marker position if needed.")
        remaining = displaced.sub("", remaining)
    remaining = remaining.strip()
    if remaining:
        reasons.append(_plain_known_text(remaining))
    if reasons:
        return " ".join(_unique(reasons))
    if _combined_status(check, ("status", "geometry_status", "value_status", "orientation_status")) in _SUCCESS:
        return "Recorded marker checks passed."
    return "No reason was saved. Check this marker's position and value against the printed tick."


def axis_setting_rows(project: Any) -> tuple[list[list[str]], bool]:
    """Return chosen/source scale rows and whether only legacy summary exists."""
    settings = _items(_get(project, "axis_settings", []))
    rows = []
    def mode(value: Any) -> str:
        return {"linear": "Linear", "log": "Logarithmic", "logarithmic": "Logarithmic"}.get(str(value).lower(), "Not recorded")
    def flag(value: Any) -> str:
        return "Logarithmic" if value is True else ("Linear" if value is False else "Not recorded")
    if settings:
        for item in settings:
            label = str(_get(item, "name", "") or "Unnamed axes")
            axis_type = str(_get(item, "type", "") or "")
            if axis_type:
                label += "\n" + axis_type
            chosen = {direction: flag(_get(item, "is_log_" + direction.lower())) for direction in ("X", "Y")}
            source = {direction: mode(_get(item, "source_" + direction.lower() + "_scale")) for direction in ("X", "Y")}
            reasons = []
            missing_flags = [direction for direction in ("X", "Y") if chosen[direction] == "Not recorded"]
            if missing_flags:
                reasons.append("The " + "/".join(missing_flags) + " TAR scale flag is not recorded as a valid setting. Check it in WPD.")
            missing_source = [direction for direction in ("X", "Y") if source[direction] == "Not recorded"]
            if missing_source:
                reasons.append("The source " + "/".join(missing_source) + " scale is unconfirmed. Check the printed tick progression or use a clearer image.")
            for direction in ("X", "Y"):
                if chosen[direction] != "Not recorded" and source[direction] != "Not recorded" and chosen[direction] != source[direction]:
                    reasons.append(direction + " is saved as " + chosen[direction].lower() + " but the source was read as " + source[direction].lower() + ". Confirm the source and correct the setting if needed.")
            if not reasons:
                reasons.append("Recorded scale check passed." if _status(item, "scale_status") in _SUCCESS else "The recorded scale check needs review. Check the saved setting against the source ticks.")
            rows.append([label, "X: " + chosen["X"] + "\nY: " + chosen["Y"],
                         "X: " + (source["X"] if source["X"] != "Not recorded" else "Unconfirmed") + "\nY: " + (source["Y"] if source["Y"] != "Not recorded" else "Unconfirmed"),
                         _status(item, "scale_status"), " ".join(reasons)])
        return rows, False
    names = _unique(str(_get(check, "axis_name", "") or "Unnamed axes")
                    for check in _items(_get(project, "calibration_checks", []))) or ["First axes"]
    for index, name in enumerate(names):
        rows.append([name, ("X: " + mode(_get(project, "x_scale")) + "\nY: " + mode(_get(project, "y_scale"))) if index == 0 else "Not recorded",
                     "Not recorded", _status(project, "scale_status") if index == 0 else "Not recorded",
                     "This older result contains a first-axis summary only. Run the check again to record exact TAR flags and independent source modes for every axis set."])
    return rows, True


def _known_kind(text: str) -> str | None:
    if text in _ROUTINE_EXACT or any(pattern.fullmatch(text) for pattern in _ROUTINE_PATTERNS):
        return "routine"
    if text in _CURVE_MESSAGES or re.fullmatch(r"\d+ points are present; the count does not prove full curve coverage or adequate knee sampling\.", text):
        return "curve"
    if text in _AXIS_MESSAGES or re.fullmatch(r"[XY] linear/log setting could not be independently confirmed from source ticks\.", text):
        return "axis"
    # The mapping module joins the same independently unresolved X/Y reasons.
    atom = r"[XY] axis: (?:At least three distinct, unambiguous labelled ticks are needed to establish scale independently|No linear or logarithmic mapping fits at least three observed tick values over enough of the axis)\."
    if re.fullmatch(atom + r"(?: " + atom + r")?", text):
        return "axis"
    category = r"(?:L1 (?:calibration|curve identity|full-curve coverage)|L2 (?:independent mapping|point order|trace confirmation)) (?:REVIEW|PASS|WARNING)"
    if re.fullmatch(category + r"(?:; " + category + r")*\.", text):
        return "routine"
    return None


def collect_findings(dr: Any) -> list[dict]:
    """Return {status,title,detail,action,files,family,section} dictionaries.

    Files retain their stored relative paths.  A grouped finding can name more
    than one family; grouping never asserts that files depict the same curve.
    Unknown/new diagnostic text is preserved, including under a PASS container.
    """
    findings: OrderedDict[tuple, dict] = OrderedDict()
    groups: OrderedDict[tuple, dict] = OrderedDict()
    remembered: set[str] = set()
    audits = _items(_get(dr, "files", []))
    known_files = _unique(_get(item, "current_filename", "") for item in audits)
    unclassified_images = [str(_get(item, "current_filename", "")) for item in audits
                           if str(_get(item, "file_type", "")).lower() == "source image"
                           and _family(_get(item, "family")) == "General"]
    missing_image_families = {_family(_get(req, "family")) for req in _items(_get(dr, "requirements", []))
                             if _status(req) == "REVIEW" and str(_get(req, "issue_code", "")) in {f"{f}_IMAGE_MISSING" for f in FAMILIES}}

    def remember(text: Any) -> None:
        for line in str(text or "").splitlines():
            if line.strip():
                remembered.add(line.strip())

    def files_for(value: Any) -> list[str]:
        """Use original paths; resolve a stored basename to all possible paths."""
        result = []
        for name in _unique(value):
            if name in known_files:
                result.append(name)
                continue
            candidates = [p for p in known_files if re.split(r"[/\\]", p)[-1] == name]
            if candidates:
                result.extend(candidates)
            elif re.search(r"\.(?:csv|tar|pdf|png|jpe?g|tiff?|bmp|webp)$", name, re.I):
                result.append(name)
        return _unique(result)

    def add(status: str, title: str, detail: str, family: str = "General", section: str = "other",
            files: Any = (), action: str | None = None) -> None:
        data = dict(status=status, title=str(title), detail=str(detail or ""),
                    action=action if action is not None else _ACTIONS[section],
                    files=_unique(files), family=family, section=section)
        key = tuple(data[k] for k in ("status", "title", "detail", "action", "family", "section"))
        if key in findings:
            findings[key]["files"] = _unique(findings[key]["files"] + data["files"])
        else:
            findings[key] = data

    def group(kind: str, status: str, family: str, files: Any, context: str = "", rows: str = "") -> None:
        key = (kind, status)
        value = groups.setdefault(key, dict(families=[], files=[], contexts=[], rows=[], count=0))
        value["count"] += 1
        value["families"] = _unique(value["families"] + [family])
        value["files"] = _unique(value["files"] + _unique(files))
        value["contexts"] = _unique(value["contexts"] + ([context] if context else []))
        if rows:
            value["rows"].append(rows)

    def comment(text: Any, status: str, family: str, section: str, files: Any,
                title: str, context: str = "") -> None:
        text = str(text or "").strip()
        if not text:
            return
        remember(text)
        kind = _known_kind(text)
        # A FAIL attached even to a familiar explanation is never discarded.
        if status != "FAIL" and kind == "routine":
            return
        if status == "REVIEW" and kind in {"axis", "curve"}:
            group("mapping" if kind == "axis" and section == "mapping" else kind,
                  status, family, files, context)
            return
        image_error = re.fullmatch(
            r"Source-image check could not be completed \(.+\)\. Completed CSV numeric findings are retained\. "
            r"Unfinished source-image, curve-identity and trace checks require REVIEW; this image error alone does not establish wrong digitization\. "
            r"Other available CSVs are checked independently\.", text, flags=re.S)
        if image_error:
            add("REVIEW", "Source image check could not finish",
                "The source image could not be read or processed. Completed CSV numeric checks are retained; the unfinished image checks need review.",
                family, "runtime", files,
                "Check that the source image opens correctly and repeat the affected check. The full report contains the technical error.")
            return
        add(status, title, _plain_known_text(text), family, section, files)

    fatal = str(_get(dr, "fatal_error", "") or "")
    if fatal:
        add("REVIEW", "Verification was interrupted", fatal, section="runtime")
        remember(fatal)

    for req in _items(_get(dr, "requirements", [])):
        status, family = _status(req), _family(_get(req, "family"))
        title, detail = str(_get(req, "requirement", "") or "Recorded requirement"), str(_get(req, "comment", "") or "")
        code, category = str(_get(req, "issue_code", "") or ""), str(_get(req, "category", "") or "")
        remember(detail)
        remember(f"{status}: {title}. {detail}")
        if status in _SUCCESS:
            continue
        files = files_for(_get(req, "found", ""))
        if status == "REVIEW" and code == "CSV_ROUNDING_REVIEW":
            summary, remainder = _export_summary(req)
            if summary and not remainder:
                group("rounding", status, family, files, title)
                continue
        if (status == "WARNING" and code == "OPTIONAL_TEMPERATURE_MISSING"
                and detail == "Higher-temperature extraction is optional. Its absence does not change the verification status."):
            group("optional_temperature", status, family, files, title)
            continue
        if code == "VERIFIER_RUNTIME_ERROR" or category == "Execution":
            section = "runtime"
            if fatal and detail == fatal and status == "REVIEW":
                continue
        elif category == "CSV export consistency":
            section = "export"
        elif category == "Dataset filename correspondence":
            section = "filename"
        elif category in {"Source availability", "Required output", "Optional output"}:
            section = "availability"
        else:
            section = "other"
        detail = plain_requirement_comment(req)
        action = _ACTIONS[section]
        if status == "REVIEW" and code in {f"{f}_IMAGE_MISSING" for f in FAMILIES}:
            title = f"{family} standalone source image is not identified"
            remainder = str(_get(req, "comment", "") or "")
            for known in (
                    f"{family} source image is not available in the folder.",
                    f"No {family} source image was identified in the folder.",
                    "This does not establish that the graph is absent from the datasheet.",
                    "Level 2 is blocked.",
                    "Its CSV cannot be compared with a graph image.",
                    "CSV numeric checks and checks of other available files continue."):
                remainder = remainder.replace(known, "")
            detail = f"The folder has no image identified as {family}, so the CSV-to-image comparison cannot be confirmed."
            if remainder.strip():
                detail += " " + remainder.strip()
            if unclassified_images:
                detail += " Unclassified image files are present."
                files = _unique(files + unclassified_images)
                action = "Open the listed image(s), check the graph labels, and assign CC, OC or TC only after confirming the graph family."
            else:
                action = "Check the supplied files and datasheet for this graph; supply a clearly identified source image if available."
        elif status == "REVIEW" and section == "availability" and family in FAMILIES and unclassified_images:
            availability = _get(dr, "availability", {}) or {}
            if availability.get(family + "_IMAGE") == "MISSING":
                action = "Inspect the unclassified image(s) before deciding source availability. " + action
                files = _unique(files + unclassified_images)
        expected = str(_get(req, "expected", "") or "")
        found = str(_get(req, "found", "") or "")
        if not detail:
            detail = f"Recorded requirement status: {status}. Expected: {expected or 'Not recorded'}. Found: {found or 'Not established'}."
        add(status, title, detail, family, section, files, action)

    for audit in audits:
        status, family = _status(audit), _family(_get(audit, "family"))
        name = str(_get(audit, "current_filename", "") or "Unnamed file")
        detail = str(_get(audit, "comment", "") or "")
        remember(detail)
        remember(f"Filename {name}: {detail}")
        if status in _SUCCESS:
            continue
        if (status == "REVIEW" and name in unclassified_images
                and detail == "Graph family is not identifiable from the image filename."
                and any(item["section"] == "availability" and item["status"] == "REVIEW"
                        and item["title"] in {f"{f} standalone source image is not identified" for f in FAMILIES}
                        and name in item["files"] for item in findings.values())):
            # The same file and required family-confirmation action are already
            # explicit in the source-availability finding. Unknown text or FAIL
            # never enters this narrow consolidation.
            continue
        detail = plain_file_comment(audit)
        expected = str(_get(audit, "expected_filename", "") or "")
        if expected:
            detail += (" " if detail else "") + "Expected label: " + expected + "."
        action = _ACTIONS["filename"]
        if name in unclassified_images:
            action = "Open this image and confirm its device and graph family before renaming or assigning it to a missing characteristic."
        elif str(_get(audit, "file_type", "")).lower() == "datasheet":
            action = "Check the PDF title or part number against this device; filename presence alone does not confirm identity."
        elif status == "EXCLUDED":
            action = "No correction is required solely for this documented exclusion; check the full record if the requested scope changes."
        add(status, "Unresolved filename or file identity", detail or "The file audit did not establish an acceptable label.", family, "filename", [name], action)

    for project in _items(_get(dr, "projects", [])):
        name = str(_get(project, "project", "") or "Unnamed WPD project")
        family = _family(_get(project, "family"))
        files = files_for(name) or [name]
        overall = _status(project, "overall_status")
        represented = set()
        for check in _items(_get(project, "calibration_checks", [])):
            role, axis = str(_get(check, "role", "") or "Marker"), str(_get(check, "axis_name", "") or "")
            detail = str(_get(check, "comment", "") or "")
            for rendered in (detail, f"{role}: {detail}", f"{axis}: {role}: {detail}"):
                represented.add(rendered.strip())
            status = _combined_status(check, ("status", "geometry_status", "value_status", "orientation_status"))
            if status not in _SUCCESS:
                context = f"{name}: " + (axis + " " if axis else "") + role
                if status == "REVIEW" and _known_kind(detail) == "axis":
                    comment(detail, status, family, "calibration", files,
                            "Calibration marker: " + context, context)
                else:
                    remember(detail)
                    add(status, "Check calibration marker " + role + (" (" + axis + ")" if axis else ""),
                        marker_reason(check), family, "calibration", files)
        for dataset in _items(_get(project, "datasets", [])):
            label = str(_get(dataset, "name", "") or "Unnamed dataset")
            status = _combined_status(dataset, ("mapping_status", "trace_status", "completeness_status"))
            notes = _items(_get(dataset, "comments", []))
            for note in notes:
                represented.add(f"{label}: {note}".strip())
                comment(note, status, family, "curve", files, f"WPD dataset: {label}", f"{name}: {label}")
            if status == "FAIL" and not notes:
                add("FAIL", f"WPD dataset: {label}", "A recorded dataset identity, trace or coverage check failed; no explanatory comment was saved.", family, "curve", files)
        for note in _items(_get(project, "comments", [])):
            note = str(note or "").strip()
            remember(f"L1 {name}: {note}")
            remember(f"Level 1 {name}: {note}")
            if note in represented:
                remember(note)
                continue
            # Known limitations can coexist with a different definite error.
            kind = _known_kind(note)
            status = overall
            if kind == "axis":
                status = _status(project, "scale_status")
            elif kind == "curve":
                status = _status(project, "dataset_status")
            section = "calibration" if kind == "axis" or re.match(r"[XY] (?:scale|calibration)\b", note) else "curve"
            comment(note, status, family, section, files, f"WPD project: {name}", name)
        if overall == "FAIL":
            fields = (("graph_type_status", "graph type"), ("scale_status", "axis scale"),
                      ("marker_geometry_status", "marker placement"), ("marker_value_status", "marker values"),
                      ("dataset_status", "dataset identity"), ("trace_status", "trace"), ("completeness_status", "coverage"))
            failed = [label for key, label in fields if _status(project, key) == "FAIL"]
            add("FAIL", f"WPD project result: {name}", "Recorded failed checks: " + (", ".join(failed) or "project overall status; supporting detail was not supplied") + ".", family, "calibration", files)

    for csv in _items(_get(dr, "level2", [])):
        name = str(_get(csv, "csv_file", "") or "Unnamed CSV")
        family = _family(_get(csv, "family"))
        files = files_for(name) or [name]
        overall = _status(csv, "overall_status")
        for note in _items(_get(csv, "comments", [])):
            note = str(note or "").strip()
            remember(f"L2 {name}: {note}")
            remember(f"Level 2 {name}: {note}")
            missing_note = note == f"No standalone {family} source image is available in the folder." or note == _MISSING_SOURCE_NUMERIC_NOTE
            if overall != "FAIL" and family in missing_image_families and missing_note:
                remember(note)
                continue
            kind = _known_kind(note)
            status = _status(csv, "axis_status") if kind == "axis" else (_status(csv, "trace_status") if kind == "curve" else overall)
            comment(note, status, family, "mapping", files, f"CSV check: {name}", name)
        if overall == "FAIL":
            failed = [label for key, label in (("axis_status", "independent mapping"), ("x_order_status", "point order"), ("trace_status", "trace or numeric data")) if _status(csv, key) == "FAIL"]
            add("FAIL", f"CSV result: {name}", "Recorded failed checks: " + (", ".join(failed) or "dataset overall status; supporting detail was not supplied") + ".", family, "mapping", files)

    for warning in _items(_get(dr, "warnings", [])):
        comment(warning, "WARNING", "General", "other", [], "Additional recorded warning")

    saved = _get(dr, "family_comments", {}) or {}
    if isinstance(saved, dict):
        for family, text in saved.items():
            for line in str(text or "").splitlines():
                line = line.strip()
                if not line or line in remembered or _known_kind(line) == "routine":
                    continue
                explicit = re.match(r"^(PASS|OK|FAIL|REVIEW|WARNING|EXCLUDED)\s*:", line)
                status = explicit[1] if explicit else _status(dr, str(family).lower() + "_status")
                add(status, "Check the recorded " + str(family) + " issue", _plain_known_text(line), _family(family), "other")

    for (kind, status), data in groups.items():
        family = ", ".join(data["families"])
        if kind == "rounding":
            title = "Confirm CSV export precision"
            detail = "The listed CSV values agree with the saved WPD values only after rounding to the precision written in the CSV."
            action = "Confirm the allowed export precision, or export more digits. Confirm any unresolved dataset or temperature pairing first."
            section = "export"
        elif kind == "optional_temperature":
            title = "Optional high-temperature exports are absent"
            detail = "The optional 150/175 C exports are not present."
            action = "No mandatory correction is required. Add these exports only when requested and available in the source."
            section = "availability"
        elif kind in {"axis", "mapping"}:
            title = "Source axes need visual confirmation" if kind == "axis" else "Independent CSV mapping could not be confirmed"
            detail = "The image did not provide enough clear tick or axis information to confirm the check."
            action = "Check the printed tick labels and linear/log scale, or provide a clearer image. Change a saved setting only if the source confirms an error."
            section = "calibration" if kind == "axis" else "mapping"
        else:
            title = "Curve identity and coverage still need visual review"
            detail = "The intended curve, point alignment and full useful range still need a visual check against the graph labels."
            action = _ACTIONS["curve"]
            section = "curve"
        add(status, title, detail, family, section, data["files"], action)

    # A saved FAIL must remain visible even in sparse/legacy result records.
    for family in FAMILIES:
        if _status(dr, family.lower() + "_status") == "FAIL" and not any(
                f["status"] == "FAIL" and family in f["family"].split(", ") for f in findings.values()):
            add("FAIL", f"{family} recorded result is FAIL", "No corresponding detailed failure was available in the supplied record; inspect the full audit before correcting files.", family)
    if _status(dr, "overall_status") == "FAIL" and not any(f["status"] == "FAIL" for f in findings.values()):
        add("FAIL", "Device recorded result is FAIL", "The saved device verdict is FAIL; supporting failure detail was not supplied in this record.")
    ranks = {"FAIL": 0, "REVIEW": 1, "WARNING": 2, "EXCLUDED": 3, "PASS": 4, "OK": 4}
    return sorted(findings.values(), key=lambda f: (ranks.get(f["status"], 3), 0 if f["section"] == "runtime" else 1))


def _brief_availability(item: dict) -> str | None:
    """Shorten only exact known REVIEW explanations for the intern table.

    Device findings remain detailed. Unknown suffixes cannot match this table,
    and explicit FAIL entries never enter this presentation-only shortcut.
    """
    if item.get("status") != "REVIEW" or item.get("section") != "availability":
        return None
    phrases = {
        ("Datasheet PDF", "The device folder has no datasheet PDF. Add it if available so the source can be checked."):
            "Datasheet PDF absent; add it if available.",
        ("Digitization evidence", "The folder has no source images, WPD projects or CSV files. Check the datasheet and add the available digitization files."):
            "Source images, WPD TARs and CSVs are absent; confirm which source graphs are available.",
    }
    for family in FAMILIES:
        phrases[(f"{family} WPD project", f"The folder has no identified {family} WPD project or source image. Check for the graph before adding an extraction.")] = (
            "WPD TAR not identified; confirm the source graph before extracting.")
        phrases[(f"{family} WPD project", f"{family} source image exists but the WPD project is missing.")] = (
            "WPD TAR absent although a source image is present; confirm the graph and create the project.")
        phrases[(f"{family} WPD project at 25 C", "No 25 C WPD project is identified. Check whether the source provides a 25 C graph before adding an extraction.")] = (
            "25 C WPD TAR not identified; confirm a 25 C source graph is available.")
        phrases[(f"{family} WPD project at 25 C", "A 25 C source image is available, but the mandatory 25 C WPD project is missing.")] = (
            "25 C WPD TAR absent although its source image is present; create the project.")
        phrases[(f"{family} WPD project at 25 C", "The project does not state its temperature. Check its datasets and label the 25 C data if present.")] = (
            "WPD temperature is unstated; confirm whether its datasets include 25 C and label them.")
        phrases[(f"{family} CSV at 25 C", "Required 25 C CSV is not available. Confirm source graph availability first.")] = (
            "25 C CSV absent; confirm a 25 C source graph is available.")
        phrases[(f"{family} CSV at 25 C", "Required 25 C CSV is not available. Source graph evidence exists.")] = (
            "25 C CSV absent although source evidence exists; confirm the dataset and export it.")
        phrases[(f"{family} CSV at 25 C", "The CSV does not state its temperature. Confirm the condition and label it.")] = (
            "CSV temperature is unstated; confirm and label the operating condition.")
    for curve in ("Coss", "Crss"):
        phrases[(f"{curve} CSV", f"The {curve} CSV is absent. Check whether the source or WPD project contains this curve before adding an export.")] = (
            f"{curve} CSV absent; confirm this curve is supplied before exporting.")
    phrase = phrases.get((str(item.get("title", "")), str(item.get("detail", ""))))
    if phrase and item.get("files"):
        # A temperature-unknown file or unclassified source remains identifiable.
        phrase += " Check files: " + "; ".join(item["files"]) + "."
    return phrase


def brief_findings(dr: Any) -> list[str]:
    """Intern-level text, with full explicit failure and unknown detail retained."""
    output = []
    grouped_titles = {
        "Confirm CSV export precision", "Optional high-temperature exports are absent",
        "Source axes need visual confirmation", "Independent CSV mapping could not be confirmed",
        "Curve identity and coverage still need visual review",
    }
    known_file_notes = tuple(_PLAIN_FILE_COMMENTS[note] for note in (
        "PDF presence is recorded. Filename alone does not verify its device identity or graph coverage.",
        "Filename convention or operating-condition label needs review; this alone does not prove wrong device data.",
        "Graph family is not identifiable from the image filename.",
    ))
    for item in collect_findings(dr):
        availability_phrase = _brief_availability(item)
        if availability_phrase:
            output.append(f"{item['family']} REVIEW: {availability_phrase}")
            continue
        prefix = f"{item['family']} {item['status']}: {item['title']}."
        known_file = item["title"] == "Unresolved filename or file identity" and any(
            re.fullmatch(re.escape(note) + r"(?: Expected label: .+\.)?", item["detail"]) for note in known_file_notes)
        known_availability = any(
            item["title"] == f"{family} standalone source image is not identified"
            and item["detail"] in {
                f"The folder has no image identified as {family}, so the CSV-to-image comparison cannot be confirmed.",
                f"The folder has no image identified as {family}, so the CSV-to-image comparison cannot be confirmed. Unclassified image files are present.",
                f"The folder has no image identified as {family}, so the CSV-to-image comparison cannot be confirmed. An image with an unclassified name is present.",
            } for family in FAMILIES)
        if item["status"] != "FAIL" and (item["title"] in grouped_titles or known_file or known_availability):
            compact = {
                "Confirm CSV export precision": "Confirm intended CSV export precision.",
                "Optional high-temperature exports are absent": "Optional 150/175 C exports absent; no mandatory correction.",
                "Source axes need visual confirmation": "Check the source ticks and markers; automatic reading was inconclusive.",
                "Independent CSV mapping could not be confirmed": "Check the source axes before accepting the CSV overlay.",
                "Curve identity and coverage still need visual review": "Visually confirm curve identity, alignment and coverage.",
            }
            phrase = compact.get(item["title"], "")
            if known_availability:
                phrase = ("Source mapping unresolved; inspect " + "; ".join(item["files"]) + " and confirm its graph family."
                          if item["files"] else "No identified source image; check available files and the datasheet.")
            elif known_file:
                if item["detail"].startswith(known_file_notes[0]):
                    phrase = "Confirm PDF identity against the device part number."
                elif item["detail"].startswith(known_file_notes[1]):
                    phrase = "Confirm project temperature/condition filename labels."
                else:
                    phrase = "Classify " + "; ".join(item["files"]) + " before assigning a graph family."
            output.append(f"{item['family'].replace(', ', '/')} {item['status']}: {phrase}")
        else:
            files = " Files: " + "; ".join(item["files"]) + "." if item["files"] else ""
            output.append(prefix + " " + item["detail"] + files + " " + item["action"])
    return output
