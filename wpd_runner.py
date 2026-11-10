"""Batch orchestration and output generation for the read-only verifier."""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, fields
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

from wpd_inventory import (
    IMAGE_EXTS, FAMILIES, classify_family, detect_temperature,
    discover_devices, iter_device_files, safe_component, intern_report_filename,
    audit_files, family_availability, build_requirements,
    crosscheck_names, source_image_choice, family_comments,
)


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "item"):
        return _json_safe(value.item())
    if isinstance(value, float):
        import math
        return value if math.isfinite(value) else None
    return value


def write_json(path, value):
    """Replace a complete record atomically, including large batch records."""
    path = Path(path)
    temporary = path.with_name(path.name + ".pending")
    temporary.write_text(json.dumps(_json_safe(value), indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _output_dir(root, intern, device, relative):
    digest = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:8]
    return Path(root) / "devices" / safe_component(intern, 34) / (safe_component(device, 43) + "_" + digest)


def verify_device(intern, device, device_dir, root, output_root, snapshot=""):
    import wpd_verifier as api
    from wpd_csv_check import verify_csv
    relative = str(Path(device_dir).relative_to(root))
    result = api.DeviceResult(intern=intern, device=device, path=relative, device_relpath=relative, source_snapshot=snapshot)
    directory = _output_dir(output_root, intern, device, relative)
    directory.mkdir(parents=True, exist_ok=True)
    result.report_key = directory.name
    files = list(iter_device_files(device_dir))
    tars = [p for p in files if p.suffix.lower() == ".tar"]
    csvs = [p for p in files if p.suffix.lower() == ".csv"]
    images = [p for p in files if p.suffix.lower() in IMAGE_EXTS]
    pdfs = [p for p in files if p.suffix.lower() == ".pdf"]
    result.inventory = {"TAR": len(tars), "CSV": len(csvs), "Images": len(images), "PDF": len(pdfs), "Other": len(files)-len(tars)-len(csvs)-len(images)-len(pdfs)}
    result.datasheets = [str(p.relative_to(device_dir)) for p in pdfs]
    result.no_digitization_files = not bool(tars or csvs or images)
    result.availability = family_availability(pdfs, tars, csvs, images)
    result.availability_flags = [key + "_MISSING" for key, value in result.availability.items() if value == "MISSING"]
    result.graph_in_datasheet = {family: "NOT_CONFIRMED" for family in FAMILIES}
    result.source_notes = [
        "Availability fields describe files found in this device folder.",
        "A missing graph image does not prove the manufacturer omitted the graph. Graph presence or absence in the complete datasheet has not been established by this folder audit.",
        "PASS FAIL REVIEW applies to recorded checks. Unresolved curve identity and full-trace coverage remain REVIEW.",
    ]
    if snapshot:
        result.source_notes.insert(0, f"Source snapshot: {snapshot}. Later changes to the source folders are outside this report.")
    result.files = audit_files(device_dir, device, api)
    result.requirements = build_requirements(device, pdfs, tars, csvs, images, api)
    result.datasheet_status = "PASS" if pdfs else "REVIEW"
    for tar in tars:
        if (detect_temperature(tar.stem) or 0) < 0:
            result.warnings.append(f"Excluded negative-temperature project: {tar.name}")
            continue
        project_token = hashlib.sha256(str(tar.relative_to(device_dir)).encode("utf-8")).hexdigest()[:10]
        project = api.verify_project(tar, directory / "projects" / project_token)
        # Preserve disambiguating paths when identically named projects occur.
        project.project = str(tar.relative_to(device_dir))
        result.projects.append(project)
    crosscheck_names(result, csvs, api)
    for csv_path in csvs:
        family = classify_family(csv_path.name)
        if (detect_temperature(csv_path.stem) or 0) < 0:
            result.warnings.append(f"Excluded negative-temperature CSV: {csv_path.name}")
            continue
        if family not in FAMILIES:
            check = api.Level2Result(csv_file=csv_path.name, family=family)
            check.comments.append("CSV family is not identifiable from its name. Assign a clear graph and dataset label before verification.")
            result.level2.append(check)
            continue
        source, note = source_image_choice(device_dir, family, csv_path.name)
        digest = hashlib.sha256(str(csv_path.relative_to(device_dir)).encode("utf-8")).hexdigest()[:8]
        token = safe_component(csv_path.stem, 52) + "_" + digest
        destination = directory / "level2" / (token + "__CSV_on_SOURCE.png")
        # Level 2 is explicitly independent: never use WPD calibration to
        # draw a substitute source overlay. When image-only calibration fails,
        # record REVIEW and continue with the next available CSV.
        check = verify_csv(source, csv_path, destination, api, note, preview_mapping=None)
        check.csv_file = str(csv_path.relative_to(device_dir))
        result.level2.append(check)
    result.file_completeness_status = api.aggregate([q.status for q in result.requirements if q.category in {"Required output", "Dataset filename correspondence", "CSV export consistency"}])
    for family in FAMILIES:
        projects = [p.overall_status for p in result.projects if p.family == family]
        level2 = [p.overall_status for p in result.level2 if p.family == family]
        requirements = [q.status for q in result.requirements if q.family == family]
        naming = [q.status for q in result.files if q.family == family]
        status = api.aggregate((projects or ["REVIEW"]) + (level2 or ["REVIEW"]) + requirements + naming)
        setattr(result, family.lower() + "_status", status)
        result.family_comments[family] = family_comments(result, family)
    result.overall_status = api.aggregate([result.datasheet_status, result.file_completeness_status, result.cc_status, result.oc_status, result.tc_status])
    # Stable paths are set even during an audit-only run, so summary links agree
    # with subsequently generated Word reports.
    result.report_docx = str(directory / (safe_component(device, 70) + "_Consolidated_Device_Report.docx"))
    return result


def _run_job(arguments):
    intern, device, path, root, output, snapshot = arguments
    import wpd_verifier as api
    try:
        result = verify_device(intern, device, Path(path), Path(root), Path(output), snapshot)
    except Exception as error:
        relative = str(Path(path).relative_to(root))
        result = api.DeviceResult(intern, device, relative, fatal_error=f"{type(error).__name__}: {error}", device_relpath=relative, source_snapshot=snapshot)
        result.availability_flags = ["VERIFIER_RUNTIME_ERROR"]
        result.source_notes = ["An execution error prevented this device from being fully checked. The device remains REVIEW; the error is not classified as wrong digitization."]
        result.family_comments = {family: result.fatal_error for family in FAMILIES}
        directory = _output_dir(output, intern, device, relative)
        directory.mkdir(parents=True, exist_ok=True)
        result.report_key = directory.name
        result.report_docx = str(directory / (safe_component(device, 70) + "_Consolidated_Device_Report.docx"))
        result.requirements = [api.RequirementCheck("Execution", "GENERAL", "Device verification", "Completed checks", "Interrupted", "REVIEW", result.fatal_error, "VERIFIER_RUNTIME_ERROR", "REVIEW")]
    record = _json_safe(asdict(result))
    directory = _output_dir(output, intern, device, result.device_relpath)
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / "device_result.json", record)
    return record


def restore_record(record):
    import wpd_verifier as api
    def make(cls, values):
        allowed = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in values.items() if key in allowed})
    result = make(api.DeviceResult, record)
    result.files = [make(api.FileAudit, item) for item in record.get("files", [])]
    result.requirements = [make(api.RequirementCheck, item) for item in record.get("requirements", [])]
    result.projects = []
    for item in record.get("projects", []):
        project = make(api.ProjectResult, item)
        project.calibration = [make(api.CalibrationPoint, q) for q in item.get("calibration", [])]
        result.projects.append(project)
    result.level2 = [make(api.Level2Result, item) for item in record.get("level2", [])]
    return result


def summarize(records, snapshot, version):
    return {
        "version": version, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_snapshot": snapshot, "devices": len(records),
        "interns": len({r["intern"] for r in records}),
        "projects": sum(len(r["projects"]) for r in records),
        "csv_files": sum(len(r["level2"]) for r in records),
        **{status: sum(r["overall_status"] == status for r in records) for status in ("PASS", "FAIL", "REVIEW")},
        "missing_datasheet": sum(r.get("availability", {}).get("DATASHEET") == "MISSING" for r in records),
        "missing_any_graph_image": sum(any(r.get("availability", {}).get(f + "_IMAGE") == "MISSING" for f in FAMILIES) for r in records),
        "runtime_errors": sum(bool(r.get("fatal_error")) for r in records),
        "per_intern": [
            {"intern": intern, "devices": len(rows),
             **{status: sum(r["overall_status"] == status for r in rows) for status in ("PASS", "FAIL", "REVIEW")}}
            for intern in sorted({r["intern"] for r in records})
            for rows in [[r for r in records if r["intern"] == intern]]
        ],
    }


def _write_device_report_task(arguments):
    from wpd_reports import write_device_reports
    raw, output, separate_levels, report_detail = arguments
    result = restore_record(raw)
    directory = _output_dir(output, result.intern, result.device, result.device_relpath)
    write_device_reports(result, directory, separate_levels=separate_levels, report_detail=report_detail)
    record = _json_safe(asdict(result))
    write_json(directory / "device_result.json", record)
    return record


def write_reports(records, output, separate_levels=False, only_devices=None, workers=1, report_detail="reviewer"):
    from wpd_reports import write_device_reports, write_intern_report
    selected = [raw for raw in records if not only_devices or raw["device"] in only_devices]
    jobs = [(raw, str(output), separate_levels, report_detail) for raw in selected]
    changed = {}
    if workers > 1:
        with ProcessPoolExecutor(max_workers=min(workers, os.cpu_count() or 2)) as pool:
            for count, raw in enumerate(pool.map(_write_device_report_task, jobs), 1):
                changed[raw["device_relpath"]] = raw
                print(f"Word device reports: {count}/{len(jobs)}", flush=True)
    else:
        for count, job in enumerate(jobs, 1):
            raw = _write_device_report_task(job)
            changed[raw["device_relpath"]] = raw
            print(f"Word device reports: {count}/{len(jobs)}", flush=True)
    objects = [restore_record(changed.get(raw["device_relpath"], raw)) for raw in records]
    interns = sorted({r.intern for r in objects})
    for count, intern in enumerate(interns, 1):
        rows = [r for r in objects if r.intern == intern]
        destination = Path(output) / "intern_reports" / intern_report_filename(intern)
        write_intern_report(intern, rows, destination, Path(output), report_detail=report_detail)
        print(f"Word intern reports: {count}/{len(interns)}", flush=True)
    return [_json_safe(asdict(r)) for r in objects]


def main():
    import wpd_verifier as api
    parser = argparse.ArgumentParser(description=f"WPD Verifier {api.VERSION}: read-only verification with Word reports and one Excel sheet per intern")
    parser.add_argument("intern_root", type=Path, nargs="?", help="Folder containing the intern folders")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / api.OUTPUT_FOLDER_NAME)
    parser.add_argument("--intern")
    parser.add_argument("--device")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--audit-only", action="store_true", help="Save complete result records without authoring Word or Excel")
    parser.add_argument("--reports-from", type=Path, help="Generate Word and Excel from an existing records.json without repeating OCR")
    parser.add_argument("--no-excel", action="store_true", help="Skip local Excel export")
    parser.add_argument("--separate-level-reports", action="store_true", help="Also generate separate Level 1 and Level 2 Word documents")
    parser.add_argument("--report-detail", choices=("reviewer", "compact", "full"), default="reviewer", help="Word report layout: reviewer (default, approved sample style), previous compact, or full technical detail")
    parser.add_argument("--clean-output", action="store_true", help="Remove only an existing marked output folder")
    parser.add_argument("--source-snapshot", default="", help="Optional description or date of the source snapshot")
    args = parser.parse_args()
    if not args.audit_only:
        missing = []
        try:
            import docx
        except ImportError:
            missing.append("python-docx")
        if not args.no_excel:
            try:
                import openpyxl
            except ImportError:
                missing.append("openpyxl")
        if missing:
            parser.error("Missing report package(s): " + ", ".join(missing) +
                         ". Install them with the already-configured Python interpreter; no new virtual environment is needed.")
    output = args.output.expanduser().resolve()
    root = args.intern_root.expanduser().resolve() if args.intern_root else None
    if root and (output == root or output in root.parents):
        parser.error("The report output must not be the input folder or its ancestor.")
    marker = output / ".wpd_verifier_output"
    if args.clean_output and output.exists():
        if not marker.is_file():
            parser.error("Refusing to remove an unmarked directory. Choose a new output folder.")
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    marker.write_text("WPD Verifier generated outputs", encoding="utf-8")
    audit_version = api.VERSION
    if args.reports_from:
        payload = json.loads(args.reports_from.read_text(encoding="utf-8"))
        records = payload["records"] if isinstance(payload, dict) else payload
        if isinstance(payload, dict):
            args.source_snapshot = args.source_snapshot or payload.get("source_snapshot", "")
            audit_version = payload.get("audit_version") or payload.get("version") or "Not recorded"
        else:
            audit_version = "Not recorded"
    else:
        if not root or not root.is_dir():
            parser.error("A valid intern root folder is required.")
        discovered = [d for d in discover_devices(root) if output != d[2].resolve() and output not in d[2].resolve().parents]
        if args.intern:
            discovered = [d for d in discovered if d[0].casefold() == args.intern.casefold()]
        if args.device:
            discovered = [d for d in discovered if d[1].casefold() == args.device.casefold()]
        if args.limit is not None:
            discovered = discovered[:max(0, args.limit)]
        if not discovered:
            print("No matching device folders were found. No completed verification is claimed.")
            return 2
        print(f"WPD Verifier {api.VERSION} | {len(discovered)} device folders | {len({d[0] for d in discovered})} interns", flush=True)
        print("L1 uses TAR and embedded graph; L2 uses standalone image and CSV. Axis values never come from CSV.", flush=True)
        records = []
        jobs = [(intern, device, str(path), str(root), str(output), args.source_snapshot) for intern, device, path in discovered]
        if args.workers <= 1:
            completed = map(_run_job, jobs)
            for i, record in enumerate(completed, 1):
                records.append(record)
                print(f"[{i}/{len(jobs)}] {record['intern']} / {record['device']}: {record['overall_status']}", flush=True)
        else:
            with ProcessPoolExecutor(max_workers=max(1, min(args.workers, os.cpu_count() or 2))) as pool:
                futures = [pool.submit(_run_job, job) for job in jobs]
                for i, future in enumerate(as_completed(futures), 1):
                    record = future.result()
                    records.append(record)
                    print(f"[{i}/{len(jobs)}] {record['intern']} / {record['device']}: {record['overall_status']}", flush=True)
        records.sort(key=lambda r: (r["intern"].casefold(), r["device"].casefold(), r["device_relpath"]))
    # Preserve complete audit records before report authoring, so a report-only
    # dependency or rendering error does not require repeating the analysis.
    payload = {"version": api.VERSION, "audit_version": audit_version, "source_snapshot": args.source_snapshot, "records": records}
    write_json(output / "records.json", payload)
    if not args.audit_only:
        print(f"Audit records saved. Generating {args.report_detail} Word reports.", flush=True)
        records = write_reports(records, output, args.separate_level_reports, workers=args.workers, report_detail=args.report_detail)
        if not args.no_excel:
            from wpd_excel_runtime import write_workbook
            print("Generating Excel master workbook.", flush=True)
            write_workbook(records, output / "WPD_Verification_Master.xlsx")
    payload = {"version": api.VERSION, "audit_version": audit_version, "source_snapshot": args.source_snapshot, "records": records}
    if not args.audit_only:
        payload.update({"report_version": api.VERSION, "report_detail": args.report_detail,
                        "report_generated_from_saved_results": bool(args.reports_from)})
    write_json(output / "records.json", payload)
    summary = summarize(records, args.source_snapshot, api.VERSION)
    summary["audit_version"] = audit_version
    if not args.audit_only:
        summary["report_version"] = api.VERSION
        summary["report_detail"] = args.report_detail
    write_json(output / "summary.json", summary)
    print(json.dumps(summary, indent=2), flush=True)
    print(f"Output: {output}", flush=True)
    return 1 if summary["runtime_errors"] else 0
if __name__ == "__main__":
    sys.exit(main())