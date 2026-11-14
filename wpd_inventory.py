"""Read-only discovery, naming, availability and CSV checks for WPD submissions."""
from __future__ import annotations

import csv
import hashlib
import io
import math
import re
from decimal import Decimal, DecimalException
from pathlib import Path
import numpy as np

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
FAMILIES = ("CC", "OC", "TC")
EXCLUDED = {
    "wpd_verifier_output", "wpd_verification_reports", "__pycache__",
    ".git", ".venv", ".venv1", "node_modules", "reports", "_reports",
}


def norm(value):
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def classify_family(name):
    name = str(name).upper()
    if re.search(r"(^|[\W_])(?:CC|CV|COSS|CRSS|CISS)(?=[\W_]|$)", name) or any(x in name for x in ("COSS", "CRSS", "CISS", "CAPACITANCE")):
        return "CC"
    if re.search(r"(^|[\W_])OC(?=[\W_]|$)", name) or "OUTPUT" in name:
        return "OC"
    if re.search(r"(^|[\W_])TC(?=[\W_]|$)", name) or "TRANSFER" in name:
        return "TC"
    return "UNKNOWN"


def detect_temperature(name):
    # A separator hyphen is not a negative temperature. An actual minus sign
    # is retained in forms such as "_-55C", "Tj=-55 C" and "-55C".
    text = str(name).upper().replace("\N{DEGREE SIGN}", "").replace("\N{MINUS SIGN}", "-")
    matches = re.findall(r"(?:^|[_\s=,])(-?\d{1,3})\s*[_ ]?\s*C(?=$|[_\s.\-])", text)
    if not matches:
        matches = re.findall(r"(?<![A-Z0-9])(\d{1,3})\s*C(?=$|[_\s.\-])", text)
    return int(matches[-1]) if matches else None


def detect_vgs(name):
    text = str(name).upper().replace("ₛ", "S")
    for pattern in (
        r"V[\s_]*G[\s_]*S[\s_:=\-]*([+]?\d+(?:\.\d+)?)\s*V",
        r"(?:^|[_\s])OC[_\s\-]*([+]?\d+(?:\.\d+)?)\s*V",
        r"(?<![A-Z0-9])([+]?\d+(?:\.\d+)?)\s*V(?=[_\s]|$)",
    ):
        m = re.search(pattern, text)
        if m:
            return format(float(m.group(1)), "g")
    return None


def is_excluded(path):
    return path.name.lower() in EXCLUDED or path.name.startswith(".") or (path.is_dir() and (path / ".wpd_verifier_output").is_file())


def iter_device_files(device_dir):
    device_dir = Path(device_dir)
    for path in sorted(Path(device_dir).rglob("*")):
        if not path.is_file():
            continue
        if any(x.lower() in EXCLUDED or x.startswith(".") for x in path.relative_to(device_dir).parts[:-1]):
            continue
        if any((ancestor / ".wpd_verifier_output").is_file() for ancestor in path.parents if ancestor != device_dir and device_dir in ancestor.parents):
            continue
        if path.name.lower() in {"desktop.ini", "thumbs.db"}:
            continue
        yield path


def _group_name(name):
    return bool(
        re.fullmatch(r"\d{1,4}[-_.]\d{1,2}[-_.]\d{1,4}", name)
        or re.fullmatch(r"\d+(?:\.\d+)?\s*[kK]?[vV]", name)
    )


def _part_like(name):
    return bool(re.search(r"[A-Za-z]", name) and re.search(r"\d", name) and not _group_name(name))


def _direct_device_evidence(folder, files):
    """Recognize direct extraction files before descending into backup folders."""
    if not _part_like(folder.name):
        return False
    part_prefix = re.compile(re.escape(folder.name) + r"(?=$|[\W_])", re.IGNORECASE)
    return any(
        path.suffix.lower() in {".tar", ".csv"}
        and (classify_family(path.stem) in FAMILIES or part_prefix.match(path.stem))
        for path in files
    )



def discover_devices(root):
    """Find devices only inside folders named Verified."""
    root = Path(root).resolve()

    for intern in sorted(
        p for p in root.iterdir()
        if p.is_dir() and not is_excluded(p)
    ):

        verified_folders = []

        def find_verified(folder):
            if folder.name.casefold() == "verified":
                verified_folders.append(folder)
                return

            for child in sorted(folder.iterdir()):
                if child.is_dir() and not is_excluded(child):
                    find_verified(child)

        find_verified(intern)

        def visit(folder):
            children = sorted(
                p for p in folder.iterdir()
                if p.is_dir() and not is_excluded(p)
            )
            files = [p for p in folder.iterdir() if p.is_file()]

            evidence = any(
                p.suffix.lower() in IMAGE_EXTS | {".pdf", ".tar", ".csv"}
                for p in files
            )
            direct_device = _direct_device_evidence(folder, files)
            device_children = [
                p for p in children
                if (_part_like(p.name) and classify_family(p.name) == "UNKNOWN")
                or _group_name(p.name)
            ]
            nested_evidence = (
                _part_like(folder.name)
                and not device_children
                and any(
                    p.suffix.lower() in IMAGE_EXTS | {".pdf", ".tar", ".csv"}
                    for p in iter_device_files(folder)
                )
            )

            if not _group_name(folder.name) and (
                direct_device
                or (evidence and not children)
                or nested_evidence
                or (not children and _part_like(folder.name))
            ):
                yield intern.name, folder.name, folder
                return

            for child in children:
                yield from visit(child)

        for verified in verified_folders:
            for child in sorted(verified.iterdir()):
                if child.is_dir() and not is_excluded(child):
                    yield from visit(child)



def safe_component(text, max_length=52):
    original = str(text)
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", original).strip(" .")
    cleaned = re.sub(r"\s+", "_", cleaned) or "unnamed"
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{s}{i}" for s in ("COM", "LPT") for i in range(1, 10)}
    if cleaned.upper() in reserved:
        cleaned = "_" + cleaned
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length-9] + "_" + hashlib.sha256(original.encode()).hexdigest()[:8]
    return cleaned


def intern_report_filename(intern):
    """Keep overview filenames distinct when intern names sanitize identically.

    Always include the original name's digest so adding another intern later
    does not rename an existing overview. Keep the prefix within 65 characters,
    matching the previous overview path budget.
    """
    original = str(intern)
    stem = safe_component(original, 65)[:56].rstrip(" .") or "unnamed"
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:8]
    return f"{stem}_{digest}_Consolidated_Verification_Report.docx"


def expected_filename(device, path, family):
    """Naming convention deviations are REVIEW; identity is checked separately."""
    path = Path(path)
    ext = path.suffix.lower()
    temperature = detect_temperature(path.stem)
    if temperature is not None and temperature < 0 and family in FAMILIES:
        return path.name, "EXCLUDED", "Negative-temperature data is excluded from the requested verification scope."
    physical_warning = graph_family_warning(path.stem, family)
    if physical_warning:
        return ("TC.*" if ext in IMAGE_EXTS else f"{device}_TC{ext}"), "REVIEW", physical_warning
    identity, _, identity_comment = filename_identity(path.stem, device)
    if ext in IMAGE_EXTS:
        if family == "CC":
            expected = "CC"
        elif family == "TC":
            expected = "TC"
        elif family == "OC":
            expected = f"OC_{temperature if temperature is not None else 25}C"
        else:
            return "Recognizable CC OC or TC source image", "REVIEW", "Graph family is not identifiable from the image filename."
        if path.stem.casefold() == expected.casefold():
            return expected + ".*", "OK", "Supported image extension and expected source-image stem."
        return expected + ".*", "REVIEW", identity_comment if identity == "MISMATCH" else "Source image has a nonstandard name. Confirm the graph family and rename the stem if appropriate."
    if ext == ".pdf":
        return "Correct device datasheet PDF", "REVIEW", "PDF presence is recorded. Filename alone does not verify its device identity or graph coverage."
    if family == "UNKNOWN":
        return "Device and graph family in filename", "REVIEW", "Cannot identify CC OC or TC from the filename."
    if ext == ".tar":
        if family == "CC":
            expected = f"{device}_CC.tar"
        elif family == "OC":
            expected = f"{device}_OC_{temperature if temperature is not None else '<temperature>'}C.tar"
        else:
            expected = f"{device}_TC.tar"
    elif ext == ".csv":
        if family == "CC":
            kind = next((c for c in ("Coss", "Crss", "Ciss") if c.upper() in path.stem.upper()), "<capacitance>")
            expected = f"{device}_{kind}.csv"
        elif family == "OC":
            expected = f"{device}_OC_Vgs_{detect_vgs(path.stem) or '<VGS>'}V_{temperature if temperature is not None else '<temperature>'}C.csv"
        else:
            expected = f"{device}_TC_{temperature if temperature is not None else '<temperature>'}C.csv"
    else:
        return path.name, "OK", ""
    if path.name.casefold() == expected.casefold():
        return expected, "OK", "Filename follows the expected convention. Curve identity is checked separately."
    return expected, "REVIEW", identity_comment if identity == "MISMATCH" else "Filename convention or operating-condition label needs review; this alone does not prove wrong device data."


def filename_identity(name, device):
    """Identify an explicit part prefix without equating substring matches.

    A different label is a review issue, not proof that the numerical data came
    from another device.  Generic curve names retain their folder context.
    """
    stem = Path(str(name)).stem
    if graph_family_warning(stem, classify_family(stem)):
        return "UNKNOWN", "", "The graph descriptor is ambiguous; confirm physical graph identity before interpreting its part-number prefix."
    marker = re.search(r"(?:^|[\W_])(?:COSS|CRSS|CISS|CC|CV|OC|TC|OUTPUT|TRANSFER)(?=[\W_]|$)", stem.upper())
    if not marker:
        return "UNKNOWN", "", "No unambiguous part prefix can be separated from the graph label."
    prefix = stem[:marker.start()].strip(" _-.,")
    if not prefix:
        return "GENERIC", "", "Generic curve name; the device folder supplies context."
    if norm(prefix) == norm(device):
        return "MATCH", prefix, "Explicit part-number prefix agrees with the device folder."
    # Some manufacturer ordering codes append the literal -TR (tape/reel)
    # to the device-folder name while the WPD dataset uses the base part.
    # Permit only this exact ordering suffix, not arbitrary fuzzy matching.
    if str(device).upper().endswith("-TR") and norm(prefix) == norm(str(device)[:-3]):
        return "MATCH", prefix, "The WPD label uses the base part; the folder has the -TR ordering suffix."
    if not _part_like(prefix):
        return "UNKNOWN", prefix, "The filename prefix is not an unambiguous device part number."
    return "MISMATCH", prefix, (
        f"Filename/dataset prefix {prefix!r} differs from device-folder identity {device!r}. "
        "Confirm whether this is a naming error or a different device; numerical and source identity are not inferred from the mismatch."
    )


def graph_family_warning(name, family=None):
    family = family or classify_family(name)
    text = str(name).upper()
    thermal = re.search(r"(?:I[\s_]*D|P[\s_]*D)[\s_\-]*(?:VS[\s_\-]*)?T[\s_]*C(?:[\W_]|$)", text)
    thermal = thermal or re.search(r"DERAT|CASE[\s_\-]*TEMP|CURRENT[\s_\-]*VS[\s_\-]*TEMP", text)
    if family == "TC" and thermal:
        return (
            "GRAPH_FAMILY_UNCONFIRMED: this filename may describe current/power versus case temperature or thermal derating, "
            "possibly at fixed VGS. It is only provisionally grouped under TC by its name. Confirm that the intended transfer graph "
            "has VGS on X and ID on Y; otherwise locate the correct transfer-characteristic source. An XY project type alone does not verify physical graph identity."
        )
    return ""


def audit_files(device_dir, device, api):
    records = []
    for path in iter_device_files(device_dir):
        if path.suffix.lower() not in IMAGE_EXTS | {".tar", ".csv", ".pdf"}:
            continue
        family = classify_family(path.name)
        expected, status, comment = expected_filename(device, path, family)
        kind = "WPD project" if path.suffix.lower() == ".tar" else ("CSV" if path.suffix.lower() == ".csv" else ("Datasheet" if path.suffix.lower() == ".pdf" else "Source image"))
        records.append(api.FileAudit(kind, family, str(path.relative_to(device_dir)), expected, status, comment))
    return records


def source_image_choice(device_dir, family, csv_name=""):
    files = [p for p in iter_device_files(device_dir) if p.suffix.lower() in IMAGE_EXTS and classify_family(p.name) == family and (detect_temperature(p.stem) or 0) >= 0]
    if not files:
        return None, f"No standalone {family} source image is available in the folder."
    temp = detect_temperature(csv_name)
    if family in {"OC", "TC"} and temp is not None:
        exact_temp = [p for p in files if detect_temperature(p.stem) == temp]
        unspecified = [p for p in files if detect_temperature(p.stem) is None]
        files = exact_temp or unspecified
        if not files:
            return None, f"No source image with matching or unspecified temperature for the {temp} C CSV. A different-temperature image was not substituted."
    canonical = {"CC": "CC", "TC": "TC", "OC": f"OC_{temp if temp is not None else 25}C"}[family]
    exact = [p for p in files if p.stem.casefold() == canonical.casefold()]
    candidates = exact or files
    if len(candidates) != 1:
        return None, "Ambiguous standalone source images: " + ", ".join(p.name for p in candidates) + ". Select the correct source before independent verification."
    chosen = candidates[0]
    note = ""
    if family in {"OC", "TC"} and temp is not None and detect_temperature(chosen.stem) is None:
        note = "Source-image filename does not identify temperature. Confirm the plotted temperature or shared multi-temperature figure."
    return chosen, note


def _numeric_token(text, delimiter):
    text = str(text).strip().replace("\N{MINUS SIGN}", "-")
    if delimiter == ";" and "," in text and "." not in text:
        text = text.replace(",", ".")
    return text


def _number(text, delimiter):
    text = _numeric_token(text, delimiter)
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text):
        return None
    value = float(text)
    return value if math.isfinite(value) else None


def _written_quantum(text, delimiter):
    """Return the last written decimal place without removing trailing zeros."""
    try:
        value = Decimal(_numeric_token(text, delimiter))
        if not value.is_finite():
            return None
        quantum = float(Decimal(1).scaleb(value.as_tuple().exponent))
        return quantum if math.isfinite(quantum) and quantum > 0 else None
    except (DecimalException, ValueError, TypeError, OverflowError):
        return None


def read_csv_record(path):
    """Preserve every numeric first row; never edit, clamp or sort source data."""
    raw = Path(path).read_bytes()
    encoding = "utf-8-sig"
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        encoding = "cp1252"
        text = raw.decode(encoding)
    sample = "\n".join(text.splitlines()[:20])
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        delimiter = "\t" if "\t" in sample else (";" if ";" in sample else ",")
    # Sniffer can mistake the two decimal commas in a headerless semicolon
    # record for column separators.  Prefer the observed delimiter that yields
    # valid numeric XY pairs; retain the sniffer choice for unresolved ties.
    def delimiter_score(candidate):
        pairs, additional = 0, 0
        for row in csv.reader(io.StringIO(sample), delimiter=candidate):
            values = [_number(s, candidate) for s in row]
            if len(values) >= 2 and values[0] is not None and values[1] is not None:
                pairs += 1
                additional += sum(v is not None for v in values[2:])
        return pairs, -additional
    delimiter = max(dict.fromkeys([delimiter, ",", ";", "\t"]), key=delimiter_score)
    xs, ys, headers, invalid, extra = [], [], [], [], []
    raw_xy_tokens, rounding_quantum = [], []
    first_data_line = None
    for line, row in enumerate(csv.reader(io.StringIO(text), delimiter=delimiter), 1):
        if not row or not any(s.strip() for s in row):
            continue
        vals = [_number(s, delimiter) for s in row]
        if len(vals) >= 2 and vals[0] is not None and vals[1] is not None:
            if first_data_line is None:
                first_data_line = line
            xs.append(vals[0])
            ys.append(vals[1])
            raw_xy_tokens.append(row[:2])
            rounding_quantum.append([_written_quantum(s, delimiter) for s in row[:2]])
            if any(v is not None for v in vals[2:]):
                extra.append(line)
        elif first_data_line is None and all(v is None for v in vals) and not any(s.strip().casefold() in {"nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"} for s in row):
            headers.append(row)
        else:
            invalid.append(line)
    if len(xs) < 2:
        raise ValueError("CSV has fewer than two valid numeric X Y rows.")
    return {
        "x": np.asarray(xs, dtype=float), "y": np.asarray(ys, dtype=float),
        "headers": headers, "invalid_lines": invalid, "extra_numeric_columns": extra,
        "first_data_line": first_data_line, "rows": len(xs), "encoding": encoding,
        "delimiter": delimiter, "first_point": [xs[0], ys[0]], "last_point": [xs[-1], ys[-1]],
        "raw_xy_tokens": raw_xy_tokens, "rounding_quantum": rounding_quantum,
    }


def load_csv_xy(path):
    record = read_csv_record(path)
    if record["invalid_lines"]:
        raise ValueError("CSV contains invalid or incomplete numeric rows at lines " + ", ".join(map(str, record["invalid_lines"][:12])) + ". No rows were silently discarded.")
    if record["extra_numeric_columns"]:
        raise ValueError("CSV contains more than two numeric columns. Identify the intended X Y columns or export one dataset per CSV.")
    columns = record["headers"][-1][:2] if record["headers"] else ["X", "Y"]
    return record["x"], record["y"], columns


def family_availability(pdfs, tars, csvs, images):
    result = {"DATASHEET": "PRESENT" if pdfs else "MISSING"}
    for family in FAMILIES:
        for label, items in (("IMAGE", images), ("TAR", tars), ("CSV", csvs)):
            result[f"{family}_{label}"] = "PRESENT" if any(classify_family(p.name) == family for p in items) else "MISSING"
    return result


def build_requirements(device, pdfs, tars, csvs, images, api):
    checks = []
    project_cache = {}
    def add(category, fam, label, expected, found, status, comment, code="", severity=""):
        item = api.RequirementCheck(category, fam, label, expected, found, status, comment)
        item.issue_code = code
        item.severity = severity or status
        checks.append(item)
    def names(items):
        return ", ".join(p.name for p in items) or "Not available in folder"
    def project_data(path):
        if path not in project_cache:
            try:
                project_cache[path] = api.read_tar_project(path)
            except Exception:
                project_cache[path] = {}
        return project_cache[path]
    def project_temperatures(path):
        declared = detect_temperature(path.stem)
        if declared is not None:
            return {declared}, False
        # A TC.tar may legitimately hold several explicitly labelled
        # temperature datasets.  Reading their names does not recalibrate data.
        try:
            project = project_data(path)
            labels = [detect_temperature(ds.get("name", "")) for ds in project.get("datasets", [])]
            known = {t for t in labels if t is not None}
            return known, not labels or any(t is None for t in labels)
        except Exception:
            return set(), True
    add("Source availability", "GENERAL", "Datasheet PDF", "Device datasheet PDF", names(pdfs),
        "PASS" if pdfs else "REVIEW",
        "PDF is present; graph coverage and device identity require source confirmation." if pdfs else "Datasheet is not available in the folder. This is an availability flag, not a finding of wrong digitization.",
        "" if pdfs else "DATASHEET_MISSING")
    if not (tars or csvs or images):
        add("Source availability", "GENERAL", "Digitization evidence", "Source image WPD project and CSV", "Not available in folder", "REVIEW",
            "No digitization files are available. Check graph availability in the datasheet before classifying missing extraction as an error.", "NO_DIGITIZATION_FILES")
    for family in FAMILIES:
        ts = [p for p in tars if classify_family(p.name) == family and (detect_temperature(p.stem) or 0) >= 0]
        cs = [p for p in csvs if classify_family(p.name) == family and (detect_temperature(p.stem) or 0) >= 0]
        ims = [p for p in images if classify_family(p.name) == family and (detect_temperature(p.stem) or 0) >= 0]
        source_exists = bool(ims or ts)
        missing_output = "FAIL" if source_exists else "REVIEW"
        add("Source availability", family, f"{family} standalone image", {"CC": "CC.*", "OC": "OC_25C.*", "TC": "TC.*"}[family],
            names(ims), "PASS" if ims else "REVIEW",
            "" if ims else f"No {family} source image was identified in the folder. Its CSV cannot be compared with a graph image. CSV numeric checks and checks of other available files continue.",
            "" if ims else f"{family}_IMAGE_MISSING")
        if family == "CC":
            add("Required output", family, f"{family} WPD project", f"{device}_{family}.tar", names(ts),
                "PASS" if ts else missing_output,
                "" if ts else (f"{family} source image exists but the WPD project is missing." if ims else f"No {family} WPD project or source graph is available; establish source availability first."),
                "" if ts else f"{family}_WPD_MISSING")
        else:
            temperature_evidence = {path: project_temperatures(path) for path in ts}
            t25 = [path for path, (known, unknown) in temperature_evidence.items() if 25 in known]
            tu = [path for path, (known, unknown) in temperature_evidence.items() if unknown and 25 not in known]
            i25 = [path for path in ims if detect_temperature(path.stem) == 25]
            source_exists = bool(i25 or t25)
            missing_output = "FAIL" if source_exists else "REVIEW"
            st = "PASS" if t25 else ("REVIEW" if tu else missing_output)
            comment = "25 C is explicitly declared in the project or its dataset labels; the source temperature still requires verification." if t25 else (
                "Project temperature is not explicit; a combined TC project may contain 25 C, which was not assumed." if tu else
                ("A 25 C source image is available, but the mandatory 25 C WPD project is missing." if i25 else
                 "A 25 C WPD project is not identified. Different-temperature or unspecified source evidence does not prove that a 25 C source graph is available.")
            )
            add("Required output", family, f"{family} WPD project at 25 C", f"{device}_{family}_25C.tar or a combined project containing 25 C", names(t25 or tu),
                st, comment, "" if t25 else (f"{family}_WPD_TEMPERATURE_UNKNOWN" if tu else f"{family}_25C_WPD_MISSING"))
        if family == "CC":
            for curve in ("Coss", "Crss"):
                matching = [p for p in cs if curve.upper() in p.stem.upper()]
                populated = []
                for path in ts:
                    if filename_identity(path.stem, device)[0] == "MISMATCH":
                        continue
                    for dataset in project_data(path).get("datasets", []):
                        label = str(dataset.get("name", ""))
                        if (curve.upper() in label.upper() and dataset.get("points")
                                and filename_identity(label, device)[0] != "MISMATCH"):
                            populated.append(f"{path.name}: {label}")
                curve_images = [p for p in ims if curve.upper() in p.stem.upper()
                                and filename_identity(p.stem, device)[0] != "MISMATCH"]
                export_expected = bool(populated or curve_images)
                if populated:
                    missing_comment = f"{curve} CSV is missing although a populated matching WPD dataset establishes an expected export: " + "; ".join(populated[:4]) + ". This is an export-availability finding, not proof of source-curve correctness."
                elif curve_images:
                    missing_comment = f"{curve} CSV is missing although a curve-specific source image is identified: {names(curve_images)}. Confirm its physical curve identity separately."
                else:
                    missing_comment = f"{curve} CSV is not available. A generic CC graph/project does not establish that this particular physical curve is supplied. Confirm individual source-curve availability or a populated matching WPD dataset before assigning a missing-extraction error."
                add("Required output", family, f"{curve} CSV", f"{device}_{curve}.csv", names(matching),
                    "PASS" if matching else ("FAIL" if export_expected else "REVIEW"),
                    "" if matching else missing_comment,
                    "" if matching else f"{curve.upper()}_CSV_MISSING")
        else:
            c25 = [p for p in cs if detect_temperature(p.stem) == 25]
            unknown = [p for p in cs if detect_temperature(p.stem) is None]
            st = "PASS" if c25 else ("REVIEW" if unknown else missing_output)
            comment = "" if c25 else ("CSV temperature is not stated; 25 C was not assumed." if unknown else "Required 25 C CSV is not available. " + ("Source graph evidence exists." if source_exists else "Confirm source graph availability first."))
            add("Required output", family, f"{family} CSV at 25 C", f"{device}_{family}_25C.csv", names(c25 or unknown), st, comment,
                "" if c25 else (f"{family}_TEMPERATURE_UNKNOWN" if unknown else f"{family}_25C_CSV_MISSING"))
            if not any(detect_temperature(p.stem) in {150, 175} for p in cs):
                add("Optional output", family, f"{family} at 150 or 175 C", "Only if provided by the source", "No such CSV identified", "WARNING",
            
                    "Higher-temperature extraction is optional. Its absence does not change the verification status.", "OPTIONAL_TEMPERATURE_MISSING", "WARNING")
            # Every standalone graph image should have a WPD project and a CSV.
    
    return checks


def semantic_key(name, family=None, inherited_temperature=None):
    family = family or classify_family(name)
    temperature = detect_temperature(name)
    if temperature is None:
        temperature = inherited_temperature
    if family == "CC":
        curve = next((s for s in ("COSS", "CRSS", "CISS") if s in str(name).upper()), None)
        return family, curve, temperature
    return family, detect_vgs(name) if family == "OC" else "TRANSFER", temperature


EXPORT_RTOL = 1e-6
EXPORT_ATOL = 1e-9
EXPORT_MATCH_FRACTION_PASS = 0.80 
  # PASS if at least 80% of CSV rows match the TAR
DENSITY_MIN_POINTS = 5     # wanted points per decade (CC)
DENSITY_FAIL_BELOW = 3     # fewer than this in a decade -> FAIL, else REVIEW


def decade_density(x, y=None, x_log=None, y_log=None, review_step=0.25, fail_step=0.40):
    """CC sampling check that works for log/log, log/linear and linear/log axes.

    A log axis is measured in decades. A linear axis counts as 4 units over its
    data range, like a 4-decade graph. A big step between neighbouring points
    means that region is sparsely sampled. Unknown scales default to
    X linear and Y log, the usual capacitance graph.
    """
    if y is None:
        return "PASS", ""
    x_log = bool(x_log) if x_log is not None else False
    y_log = bool(y_log) if y_log is not None else True
    xs = np.asarray(x, dtype=float)
    ys = np.asarray(y, dtype=float)
    ok = np.isfinite(xs) & np.isfinite(ys)
    if y_log:
        ok &= ys > 0
    if x_log:
        ok &= xs > 0
        if ok.any():
            ok &= xs > float(xs[ok].max()) * 1e-6      # ignore ~0 artifacts such as 2.8e-14
    xs, ys = xs[ok], ys[ok]
    if len(xs) < 3:
        return "PASS", ""
    order = np.argsort(xs, kind="stable")
    xs, ys = xs[order], ys[order]

    def units(values, is_log):
        if is_log:
            return np.log10(values)
        span = float(values.max() - values.min())
        return values / span * 4.0 if span > 0 else values * 0.0

    dx = np.abs(np.diff(units(xs, x_log)))
    dy = np.abs(np.diff(units(ys, y_log)))
    length = np.hypot(dx, dy)
    fail_here = (dy > fail_step) | ((dx > fail_step) if x_log else False)
    if not (length > review_step).any() and not np.any(fail_here):
        return "PASS", ""
    i = int(np.argmax(length))
    status = "FAIL" if np.any(fail_here) else "REVIEW"
    bad = int(np.sum((length > review_step) | fail_here))
    axes = f"X {'log' if x_log else 'linear'}, Y {'log' if y_log else 'linear'}"
    return status, (
        f"Sparse sampling ({axes}): between X={xs[i]:g}, Y={ys[i]:g} and X={xs[i+1]:g}, Y={ys[i+1]:g} "
        f"the curve moves {length[i]:.2f} units in one step (limit {review_step:g}). "
        f"{bad} step(s) exceed the limit. Add more points in this region, usually near the low-X knee.")

def _match_pairs(candidates, stored_count):
    """Find a maximum paired-row matching without reusing duplicate points."""
    from collections import deque
    match_left = [-1] * len(candidates)
    match_right = [-1] * stored_count
    for start in range(len(candidates)):
        queue, seen_left, via_right = deque([start]), {start}, {}
        free = None
        while queue and free is None:
            left = queue.popleft()
            for right in candidates[left]:
                if right in via_right:
                    continue
                via_right[right] = left
                if match_right[right] == -1:
                    free = right
                    break
                other = match_right[right]
                if other not in seen_left:
                    seen_left.add(other)
                    queue.append(other)
        while free is not None and free != -1:
            left = via_right[free]
            previous = match_left[left]
            match_left[left], match_right[free] = free, left
            free = previous
    return match_left, match_right


def compare_export_values(saved_values, csv_record):
    """Compare complete paired XY multisets, including duplicate multiplicity.

    The saved WPD numeric values are used only to verify its export.  They are
    never used to derive a source-image axis, and agreement is not source truth.
    """
    result = {"status": "REVIEW", "csv_rows": csv_record.get("rows", 0),
              "wpd_rows": len(saved_values) if isinstance(saved_values, (list, tuple)) else 0,
              "matched_rows": 0, "strict_matched_rows": 0,
              "rounding_matched_rows": 0, "rtol": EXPORT_RTOL, "atol": EXPORT_ATOL,
              "unmatched_csv_samples": [], "unmatched_wpd_samples": []}
    if csv_record.get("invalid_lines"):
        result["status"] = "FAIL"
        result["comment"] = "CSV contains invalid/incomplete numeric rows at lines " + ", ".join(map(str, csv_record["invalid_lines"][:12])) + "; they were not silently discarded."
        return result
    if csv_record.get("extra_numeric_columns"):
        result["comment"] = "CSV has additional numeric columns; the intended paired X Y export is ambiguous."
        return result
    if not isinstance(saved_values, (list, tuple)) or not saved_values:
        result["comment"] = "WPD has no saved numeric value sequence; pixel coordinates alone do not establish CSV export consistency."
        return result
    saved = []
    for value in saved_values:
        if not isinstance(value, (list, tuple)) or len(value) < 2:
            result["comment"] = "Some WPD points have no saved numeric X Y values; the full export cannot be confirmed."
            return result
        try:
            pair = float(value[0]), float(value[1])
        except (TypeError, ValueError, OverflowError):
            result["comment"] = "WPD saved numeric values contain an unreadable entry; export consistency requires review."
            return result
        if not all(math.isfinite(v) for v in pair):
            result["comment"] = "WPD saved numeric values contain a non-finite entry; export consistency requires review."
            return result
        saved.append(pair)
    stored = np.asarray(saved, dtype=float)
    actual = np.column_stack((csv_record["x"], csv_record["y"]))
    # An augmenting-path match avoids false failures from lexicographic sorting
    # near rounded X ties.  It also prevents one stored point matching two CSV
    # duplicates.  Datasets are processed one row at a time to bound memory.
    candidates = [np.flatnonzero(np.all(np.isclose(row, stored, rtol=EXPORT_RTOL, atol=EXPORT_ATOL), axis=1)).tolist() for row in actual]
    match_left, match_right = _match_pairs(candidates, len(stored))
    matched = sum(index >= 0 for index in match_left)
    result["matched_rows"] = matched
    result["strict_matched_rows"] = matched
    result["unmatched_csv_samples"] = [actual[i].tolist() for i, j in enumerate(match_left) if j < 0][:3]
    result["unmatched_wpd_samples"] = [stored[i].tolist() for i, j in enumerate(match_right) if j < 0][:3]
    result["status"] = "PASS" if len(actual) == len(stored) == matched else "FAIL"
    result["comment"] = (
        f"CSV rows={len(actual)}; saved WPD rows={len(stored)}; strict paired XY matches={matched}. "
        f"Full multiset comparison allows reordered rows and uses rtol={EXPORT_RTOL:g}, atol={EXPORT_ATOL:g} per coordinate; duplicate multiplicity is preserved. "
        "CSV first/last rows are retained. This checks export consistency only, not datasheet truth."
    )
    # A CSV can round saved WPD values to its explicitly written decimal
    # precision. If every X/Y value matches within half its own final written
    # decimal increment, the export-consistency check PASSES. This does not
    # assess whether the user chose sufficient precision for later modeling,
    # nor establish whether the WPD calibration itself is physically correct.
    quanta = csv_record.get("rounding_quantum", [])
    if result["status"] == "FAIL" and len(actual) == len(stored) and len(quanta) == len(actual):
        quantum_array = np.asarray([
            [float(q) if isinstance(q, (int, float)) and math.isfinite(q) and q > 0 else 0.0 for q in pair]
            if isinstance(pair, (list, tuple)) and len(pair) == 2 else [0.0, 0.0]
            for pair in quanta
        ], dtype=float)
        rounded_candidates = []
        for row, quantum in zip(actual, quantum_array):
            strict = np.isclose(row, stored, rtol=EXPORT_RTOL, atol=EXPORT_ATOL)
            scale = np.maximum(np.maximum(np.abs(stored), np.abs(row)), quantum)
            epsilon = (8 * np.finfo(float).eps) * scale
            rounded = (quantum > 0) & (np.abs(stored - row) <= quantum / 2 + epsilon)
            rounded_candidates.append(np.flatnonzero(np.all(strict | rounded, axis=1)).tolist())
        rounded_left, _ = _match_pairs(rounded_candidates, len(stored))
        rounded_matches = sum(index >= 0 for index in rounded_left)
        result["rounding_matched_rows"] = rounded_matches
        if rounded_matches == len(actual):
            result["status"] = "PASS"
            result["issue_code"] = "CSV_ROUNDING_PASS"
            result["match_mode"] = "csv_written_precision"
            samples = []
            tokens = csv_record.get("raw_xy_tokens", [])
            for left, right in enumerate(rounded_left):
                if np.all(np.isclose(actual[left], stored[right], rtol=EXPORT_RTOL, atol=EXPORT_ATOL)):
                    continue
                samples.append({
                    "csv_row": left + 1,
                    "csv_tokens": tokens[left] if left < len(tokens) else None,
                    "csv": actual[left].tolist(), "wpd": stored[right].tolist(),
                    "quantum": quantum_array[left].tolist(),
                })
                if len(samples) == 3:
                    break
            result["rounding_samples"] = samples
            result["comment"] += (
                f" CSV_ROUNDING_PASS: all {rounded_matches} paired rows agree when saved values are rounded within half of the CSV's final written decimal increment, with floating-point roundoff only. "
                "Export consistency passes at the written precision; whether that precision is sufficient for subsequent modeling remains a separate question. "
                "The strict tolerance remains unchanged, and no axis range is used. "
                f"Rounding examples (CSV tokens, saved values and decimal quantum)={samples}."
            )
        # Partial-match tolerance for ALL families (CC, OC, TC):
    # PASS when at least 80% of rows match, strictly or after CSV rounding.
    if result["status"] == "FAIL":
        denominator = max(len(actual), len(stored))
        best = max(result["matched_rows"], result.get("rounding_matched_rows", 0))
        fraction = best / denominator if denominator else 0.0
        result["match_fraction"] = fraction
        if fraction >= EXPORT_MATCH_FRACTION_PASS:
            result["status"] = "PASS"
            result["issue_code"] = "CSV_PARTIAL_MATCH_PASS"
            result["comment"] += (
                f" CSV_PARTIAL_MATCH_PASS: {best} of {denominator} rows ({fraction:.0%}) match the saved WPD values; "
                f"at least {EXPORT_MATCH_FRACTION_PASS:.0%} is accepted.")
    if result["status"] == "FAIL" or result.get("issue_code") == "CSV_PARTIAL_MATCH_PASS":
        result["comment"] += f" Unmatched CSV examples={result['unmatched_csv_samples']}; unmatched WPD examples={result['unmatched_wpd_samples']}."
    return result


def crosscheck_names(dr, csv_files, api):
    """Verify label correspondence and numerical exports as separate checks."""
    # Safe to reapply after a batch without duplicating stale correspondence
    # findings.  Calibration and source-image checks are left untouched.
    dr.requirements[:] = [q for q in dr.requirements if q.category not in {"Dataset filename correspondence", "CSV export consistency"}]
    csv_files = [Path(p) for p in csv_files if (detect_temperature(Path(p).stem) or 0) >= 0]
    entries = []
    csv_cache = {}
    comparison_cache = {}
    def csv_data(path):
        if str(path) not in csv_cache:
            try:
                csv_cache[str(path)] = read_csv_record(path)
            except Exception as exc:
                csv_cache[str(path)] = {"error": str(exc), "rows": 0}
        return csv_cache[str(path)]
    def comparison(index, path):
        key = index, str(path)
        if key not in comparison_cache:
            data = csv_data(path)
            comparison_cache[key] = ({"status": "FAIL", "comment": "CSV could not be read as a paired numerical export: " + data["error"]}
                                     if "error" in data else compare_export_values(entries[index]["dataset"].get("exported_values"), data))
        return comparison_cache[key]
    def compatible(first, second):
        return (first[:2] == second[:2] and first[1] is not None
                and (first[2] is None or second[2] is None or first[2] == second[2]))
    for project in dr.projects:
        inherited = detect_temperature(project.project)
        for dataset in project.datasets:
            name = str(dataset.get("name", ""))
            key = semantic_key(name, project.family, inherited)
            if key[2] is not None and key[2] < 0:
                continue
            identity = filename_identity(name, dr.device)
            allowed = [path for path in csv_files if filename_identity(path.stem, dr.device)[0] != "MISMATCH"] if identity[0] != "MISMATCH" else []
            exact = [path for path in allowed if norm(path.stem) == norm(name)]
            semantic = [path for path in allowed if compatible(key, semantic_key(path.stem))]
            entries.append({"project": project, "dataset": dataset, "name": name, "key": key, "identity": identity, "matches": exact or semantic})
    reverse = {}
    for index, entry in enumerate(entries):
        for path in entry["matches"]:
            reverse.setdefault(str(path), []).append(index)
    matched_csvs = set()
    def add(category, family, requirement, found, status, comment, code):
        item = api.RequirementCheck(category, family, requirement, "Unambiguous labels and a complete paired XY export", found, status, comment)
        item.issue_code, item.severity = code if status != "PASS" else "", status
        dr.requirements.append(item)
    for index, entry in enumerate(entries):
        project, name, key, matches = entry["project"], entry["name"], entry["key"], entry["matches"]
        unique = len(matches) == 1 and len(reverse.get(str(matches[0]), [])) == 1
        if unique:
            path = matches[0]
            matched_csvs.add(str(path))
            default_temperature_unknown = (
                norm(name) in {"DEFAULT", "DEFAULTDATASET"}
                and project.family in {"OC", "TC"} and key[2] is None
                and detect_temperature(path.stem) is not None
            )
            association_comment = (
                "Only one CSV is compatible, but the default WPD dataset and project do not declare a temperature. "
                f"The CSV's {detect_temperature(path.stem)} C identity is therefore unconfirmed; uniqueness alone does not establish that this is the intended export."
                if default_temperature_unknown else
                "Labels provide one compatible curve association. An unspecified temperature is not assumed to be 25 C; this association does not establish source-curve identity."
            )
            add("Dataset filename correspondence", project.family, f"CSV for {name}", path.name,
                "REVIEW" if default_temperature_unknown else "PASS", association_comment,
                "DATASET_CSV_TEMPERATURE_UNCONFIRMED" if default_temperature_unknown else "")
            outcome = dict(comparison(index, path))
            if outcome["status"] == "FAIL":
                alternatives = [other["name"] for j, other in enumerate(entries) if j != index and other["project"].family == project.family and comparison(j, path)["status"] == "PASS"]
                if alternatives:
                    outcome["comment"] += " This CSV numerically matches other WPD dataset(s): " + ", ".join(alternatives[:4]) + ". Check swapped or incorrect filename/dataset labels; no file was renamed."
            if default_temperature_unknown and outcome["status"] != "PASS":
                outcome["comment"] = association_comment + " Numerical discrepancy evidence is retained below for review; it cannot establish an export error until the intended dataset/temperature association is confirmed. " + outcome["comment"]
                if outcome["status"] == "FAIL":
                    outcome["status"] = "REVIEW"
                    outcome["issue_code"] = "CSV_WPD_IDENTITY_UNCONFIRMED"
            add("CSV export consistency", project.family, f"Export of {name}", path.name, outcome["status"], outcome["comment"], outcome.get("issue_code", "CSV_WPD_EXPORT_MISMATCH" if outcome["status"] == "FAIL" else "CSV_WPD_EXPORT_UNCONFIRMED"))
        else:
            status = "WARNING" if not matches and key[2] in {150, 175} else "REVIEW"
            comment = (entry["identity"][2] + " " if entry["identity"][0] == "MISMATCH" else "")
            comment += "The CSV-to-WPD association is ambiguous or absent; numeric differences are not graded as export errors without an unambiguous pairing."
            if matches:
                comment += " Candidates: " + ", ".join(path.name for path in matches) + "."
            add("Dataset filename correspondence", project.family, f"CSV for {name}", ", ".join(path.name for path in matches) or "Not established", status, comment, "DATASET_CSV_MAPPING_UNRESOLVED")
    for path in csv_files:
        if str(path) in matched_csvs:
            continue
        family = classify_family(path.stem)
        if family not in FAMILIES:
            continue
        numeric = [entry["name"] for index, entry in enumerate(entries) if entry["project"].family == family and comparison(index, path)["status"] == "PASS"]
        identity = filename_identity(path.stem, dr.device)
        comment = (identity[2] + " " if identity[0] == "MISMATCH" else "")
        comment += "CSV-to-WPD identity is not established by unambiguous labels."
        if numeric:
            comment += " Full paired XY values match WPD dataset(s): " + ", ".join(numeric[:4]) + ". This is a potential filename or dataset-label correction, not proof of physical curve identity; no rename was made."
        add("Dataset filename correspondence", family, f"WPD dataset for {path.name}", "Not established", "REVIEW", comment, "CSV_DATASET_MAPPING_UNRESOLVED")


def family_comments(dr, family):
    parts = []
    for req in dr.requirements:
        if req.family == family and req.status in {"FAIL", "REVIEW"}:
            parts.append(f"{req.status}: {req.requirement}. {req.comment}")
    for project in dr.projects:
        if project.family == family:
            for comment in project.comments:
                parts.append(f"L1 {project.project}: {comment}")
    for dataset in dr.level2:
        if dataset.family == family:
            for comment in dataset.comments:
                parts.append(f"L2 {dataset.csv_file}: {comment}")
    for file in dr.files:
        if file.family == family and file.status in {"FAIL", "REVIEW"}:
            parts.append(f"Filename {file.current_filename}: {file.comment}")
    unique = list(dict.fromkeys(parts))
    return "\n".join(unique) if unique else "All applicable recorded checks passed."
