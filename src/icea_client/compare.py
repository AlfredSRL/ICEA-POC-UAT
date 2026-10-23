"""Offline comparison of saved API responses with the approved UAT ground truth.

No network access. Saved responses are read, never rewritten. Scoring and
row alignment come from the vendored server modules, unchanged.
"""
from __future__ import annotations

import hashlib
import json
import statistics
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import canonical
from .client_rules import false_as_empty_extraction, false_as_empty_truth, with_implicit_false
from .config import METHOD_NAMES, METHODS, Paths
from .preflight import default_selection, profile_block
from .runner import API_VERSION, PublicExtraction, load_records, save_json
from evaluation import score
from contracts import REMOVED_FIELD_PATHS
import export_field_comparison
from export_field_comparison import GT_ISSUES, comparison_rows
from metrics import sha256_of_file

REPORT_FILES = ("field_comparison_data.json", "comparison_summary.json",
                "comparison_summary.md", "Confronto_estrazioni_UAT.xlsx")
COUNT_KEYS = ("evaluable_rows", "not_ready_rows", "ambiguous_truth_rows", "matched", "partial", "incorrect", "omitted",
              "unassessed", "unresolved_group", "unpaired_occurrences", "invalid_field",
              "field_issue_count")
TIMING_KEYS = ("pipeline_seconds", "http_seconds")
# The vendored row builder requires its own four method keys. They serve here only as
# positional slots, renamed back to the API options: no option-to-model meaning.
SLOTS = dict(zip(METHODS, export_field_comparison.METHODS))


def transient(record: dict) -> bool:
    """Failures that say nothing about the extraction (busy server, transport, auth)."""
    error = record.get("error") or ""
    return (record.get("status") != "succeeded"
            and (error in {"busy", "timeout", "unauthorized", "api_not_configured"}
                 or error.startswith("network:") or record.get("http_status") in {401, 429, 503}))


def select_record(records: list[dict], document, method: str) -> dict | None:
    """Latest definitive call (success or schema/API failure) for this PDF and profile.

    A schema failure stays the outcome of the whole run: an older success is not
    preferred over it. Transient failures are used only when nothing else exists.
    """
    relevant = [record for record in records
                if record.get("method") == method and record.get("extraction_option") == method
                and record.get("api_version") == API_VERSION and record.get("profile") == document.profile
                and record.get("pdf_sha256") == document.sha256]
    definitive = [record for record in relevant if not transient(record)]
    chosen = definitive or relevant
    return max(chosen, key=lambda record: record["started_at"]) if chosen else None


def _load_method(record: dict | None, document) -> tuple[dict, str | None]:
    failed = {"status": "failed", "parsed": {}, "diagnostic": None, "field_issues": []}
    if not record:
        return failed, "non eseguita"
    if record["status"] != "succeeded":
        return failed, f"errore API: {record['error']}"
    response = json.loads((Path(record["_dir"]) / "response.json").read_text(encoding="utf-8"))
    body = response["body"]
    # As on the server: occurrences of paths removed from the contract are dropped in
    # memory and recorded; the archived response is not rewritten.
    payload = dict(body["extraction"])
    removed = sorted({field.get("field_path") for field in payload.get("fields", [])
                      if field.get("field_path") in REMOVED_FIELD_PATHS})
    payload["fields"] = [field for field in payload.get("fields", [])
                         if field.get("field_path") not in REMOVED_FIELD_PATHS]
    issues = [issue for issue in body.get("field_issues") or []
              if issue.get("field_path") not in REMOVED_FIELD_PATHS]
    try:
        # Archived responses are re-validated with the current canonical schema, offline.
        extraction = PublicExtraction.model_validate(payload)
    except ValueError:
        return {**failed, "removed_paths": removed}, "schema non valido (rivalidazione offline)"
    extraction, implicit = with_implicit_false(extraction, document)
    extraction, false_empty = false_as_empty_extraction(extraction)
    return {"status": "succeeded", "extraction": extraction, "implicit_false": implicit,
            "false_as_empty_paths": false_empty,
            "removed_paths": removed, "field_issues": issues}, None

def code_fingerprint() -> dict:
    """Identify the scoring code: vendored server modules and the client sources."""
    manifest = json.loads((canonical.CANONICAL_DIR / "MANIFEST.json").read_text(encoding="utf-8"))
    package = canonical.CANONICAL_DIR.parent
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*")):
        if path.is_file() and path.suffix in {".py", ".json"} and "__pycache__" not in path.parts:
            digest.update(path.relative_to(package).as_posix().encode())
            digest.update(path.read_bytes())
    return {"canonical_server_commit": manifest["server_commit"],
            "canonical_files_sha256": manifest["files"],
            "client_sources_sha256": digest.hexdigest()}

def _timing(record: dict | None) -> dict:
    return {key: (record or {}).get(key) for key in TIMING_KEYS}


def build_comparison(gt, base: Paths, samples: list[str] | None = None) -> dict:
    gt_hash = sha256_of_file(gt.path)
    selected, excluded = default_selection(gt, samples)
    unsupported = [sample for sample in selected if profile_block(gt.documents[sample].profile)]
    if unsupported:
        raise ValueError(f"profile not accepted by the API: {unsupported}")
    output = []
    for sample_id in selected:
        document, false_rows = false_as_empty_truth(gt.documents[sample_id])
        records = load_records(base.runs, sample_id)
        chosen = {method: select_record(records, document, method) for method in METHODS}
        loaded, labels = {}, {}
        for method in METHODS:
            loaded[method], labels[method] = _load_method(chosen[method], document)
        rows = comparison_rows(document, {SLOTS[method]: value for method, value in loaded.items()})
        for row in rows:
            row["methods"] = {method: row["methods"][SLOTS[method]]
                              for method in METHODS if SLOTS[method] in row["methods"]}
        for row in rows:
            if row.get("row_id") in false_rows and row["kind"] == "GT":
                row["gt_raw"] = "false"
                row["gt_issue"] = "false letto come campo vuoto: assenza attesa (regola client)"
        for row in rows:
            for method, label in labels.items():
                value = row["methods"].get(method)
                if label and value and value.get("outcome") == "schema non valido":
                    value["outcome"] = label
        methods = {}
        for method in METHODS:
            record, value = chosen[method], loaded[method]
            methods[method] = {
                "status": value["status"], "error": labels[method],
                "api_run_id": (record or {}).get("api_run_id"),
                "local_run_dir": Path(record["_dir"]).name if record else None,
                "api_base_url": (record or {}).get("api_base_url"),
                "http_status": (record or {}).get("http_status"),
                "field_issues": value.get("field_issues", []),
                "implicit_false_paths": value.get("implicit_false", []),
                "removed_paths": value.get("removed_paths", []),
                "false_as_empty_paths": value.get("false_as_empty_paths", []),
                "timing": _timing(record),
                "score": (score(document, value["extraction"], value["field_issues"])
                          if value["status"] == "succeeded" else None)}
        output.append({"sample_id": sample_id, "profile": document.profile,
                       "filename": document.filename, "source_sha256": document.sha256,
                       "ground_truth_sha256": gt_hash, "gt_rows": len(document.rows),
                       "false_as_empty_rows": false_rows,
                       "methods": methods, "rows": rows})
    return {"version": 2, "methods": list(METHODS), "samples": output,
            "excluded_samples": excluded,
            "ground_truth_sha256": gt_hash, "ground_truth_file": gt.path.name,
            "code": code_fingerprint(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "note": "Score provvisorio sulle risposte API v2 salvate."}


def _stats(values: list) -> dict:
    values = [value for value in values if isinstance(value, (int, float))]
    return {"count": len(values),
            "mean_seconds": statistics.mean(values) if values else None,
            "median_seconds": statistics.median(values) if values else None,
            "total_seconds": sum(values) if values else None}


def _timing_stats(samples: list[dict], method: str) -> dict:
    entries = [sample["methods"][method] for sample in samples]
    return {"attempts": sum(entry["local_run_dir"] is not None for entry in entries),
            "samples": len(entries),
            "valid": sum(entry["status"] == "succeeded" for entry in entries),
            **{key.removesuffix("_seconds"): _stats([entry["timing"][key] for entry in entries])
               for key in TIMING_KEYS}}


def _totals(samples: list[dict], method: str) -> dict:
    totals = {key: 0 for key in COUNT_KEYS}
    for sample in samples:
        report = sample["methods"][method]["score"]
        if report:
            for key in COUNT_KEYS:
                totals[key] += report.get(key, 0)
    totals["valid_samples"] = sum(sample["methods"][method]["score"] is not None for sample in samples)
    totals["samples"] = len(samples)
    return totals


def summarize(data: dict) -> tuple[dict, str]:
    samples = data["samples"]
    profiles = sorted({sample["profile"] for sample in samples})
    totals = {method: _totals(samples, method) for method in METHODS}
    by_profile = {profile: {method: _totals([s for s in samples if s["profile"] == profile], method)
                            for method in METHODS} for profile in profiles}
    timing = {method: _timing_stats(samples, method) for method in METHODS}
    timing_profile = {profile: {method: _timing_stats([s for s in samples if s["profile"] == profile], method)
                                for method in METHODS} for profile in profiles}
    seconds = lambda value: f"{value:.2f}" if value is not None else "—"  # noqa: E731
    percent = lambda part, whole: f"{100 * part / whole:.1f}" if whole else "—"  # noqa: E731
    lines = ["# Confronto offline delle estrazioni UAT", "",
             "Questo report non effettua chiamate di rete e non modifica le risposte API salvate. "
             "Le quattro soluzioni sono le opzioni di estrazione opache A, B, C e D della API; "
             "la loro configurazione resta riservata al server.", "",
             f"GT: `{data['ground_truth_file']}`; SHA-256 `{data['ground_truth_sha256']}`. "
             "Score provvisorio: `partial` non vale come match.", "",
             f"Codice di scoring: moduli server `{data['code']['canonical_server_commit'][:7]}`, "
             f"sorgenti client SHA-256 `{data['code']['client_sources_sha256'][:12]}`.", "",
             *([("Campioni esclusi: " + ", ".join(
                 f"{item['sample_id']} ({item['profile']}, {item['reason']})" for item in data["excluded_samples"]) + "."), ""]
               if data.get("excluded_samples") else []),
             "| Campione | Soluzione | Esito API | Match | Parziali | Discordanti | Omesse | Campi invalidi | Non valutate | Gruppi irrisolti | GT non pronta |",
             "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for sample in samples:
        for method in METHODS:
            info = sample["methods"][method]
            report = info["score"]
            if report:
                lines.append(f"| {sample['sample_id']} | {METHOD_NAMES[method]} | ok | " + " | ".join(
                    str(report.get(key, 0)) for key in ("matched", "partial", "incorrect", "omitted",
                                                        "invalid_field", "unassessed",
                                                        "unresolved_group", "not_ready_rows")) + " |")
            else:
                lines.append(f"| {sample['sample_id']} | {METHOD_NAMES[method]} | {info['error']} |"
                             + " |" * 8)
    lines += ["", "## Totali per soluzione", "",
              "| Soluzione | Campioni valutati | Righe valutabili | Match | Partial | Incorrect | Omitted | Campi invalidi | Gruppi irrisolti | Match % | Partial % |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for method in METHODS:
        item = totals[method]
        lines.append(f"| {METHOD_NAMES[method]} | {item['valid_samples']}/{item['samples']} | "
                     f"{item['evaluable_rows']} | {item['matched']} | {item['partial']} | "
                     f"{item['incorrect']} | {item['omitted']} | {item['invalid_field']} | "
                     f"{item['unresolved_group']} | {percent(item['matched'], item['evaluable_rows'])} | "
                     f"{percent(item['partial'], item['evaluable_rows'])} |")
    lines += ["", "## Tempi", "",
              "Tempo pipeline = elaborazione server (dalla API). Tempo HTTP = richiesta completa misurata "
              "dal client, incluso upload.", "",
              "| Soluzione | Chiamate valide | Pipeline media | Pipeline mediana | HTTP medio | HTTP mediano |",
              "|---|---:|---:|---:|---:|---:|"]
    for method in METHODS:
        item = timing[method]
        lines.append(f"| {METHOD_NAMES[method]} | {item['valid']}/{item['samples']} | "
                     f"{seconds(item['pipeline']['mean_seconds'])} | {seconds(item['pipeline']['median_seconds'])} | "
                     f"{seconds(item['http']['mean_seconds'])} | {seconds(item['http']['median_seconds'])} |")
    reasons = {}
    for sample in samples:
        for method in METHODS:
            report = sample["methods"][method]["score"]
            if report:
                for item in report["not_ready"]:
                    for reason in item["reasons"]:
                        reasons[reason] = reasons.get(reason, 0) + 1
                break
    if reasons:
        lines += ["", "Righe GT non valutabili per causa: " + ", ".join(
            f"{GT_ISSUES.get(reason, reason)}={count}" for reason, count in sorted(reasons.items())) + "."]
    summary = {"provisional": True, "network_calls_in_compare": 0,
               "ground_truth_file": data["ground_truth_file"],
               "ground_truth_sha256": data["ground_truth_sha256"],
               "excluded_samples": data.get("excluded_samples", []),
               "code": data["code"],
               "totals": totals, "totals_by_profile": by_profile,
               "timing_totals": timing, "timing_by_profile": timing_profile,
               "samples": [{"sample_id": sample["sample_id"], "profile": sample["profile"],
                            "methods": {method: {key: sample["methods"][method][key] for key in
                                                 ("status", "error", "api_run_id", "local_run_dir", "removed_paths",
                                                  "timing")}
                                        for method in METHODS}} for sample in samples]}
    return summary, "\n".join(lines) + "\n"


def archive_previous(reports: Path) -> Path | None:
    existing = [reports / name for name in REPORT_FILES if (reports / name).is_file()]
    if not existing:
        return None
    history = reports / "history"
    history.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = history / f"before_compare_{stamp}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in existing:
            archive.write(path, path.name)
    return target


def run_compare(gt, base: Paths, samples: list[str] | None = None, *, workbook: bool = True,
                log=print) -> dict:
    from .workbook import build_workbook, verify_workbook

    data = build_comparison(gt, base, samples)
    summary, markdown = summarize(data)
    reports = base.reports
    reports.mkdir(parents=True, exist_ok=True)
    archived = archive_previous(reports)
    save_json(reports / "field_comparison_data.json", data)
    save_json(reports / "comparison_summary.json", summary)
    (reports / "comparison_summary.md").write_text(markdown, encoding="utf-8")
    with (reports / "ground_truth_history.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"generated_at": data["generated_at"],
                                 "ground_truth_file": data["ground_truth_file"],
                                 "ground_truth_sha256": data["ground_truth_sha256"],
                                 "samples": [sample["sample_id"] for sample in data["samples"]]}) + "\n")
    path = None
    if workbook:
        path = reports / "Confronto_estrazioni_UAT.xlsx"
        build_workbook(data, summary, path)
        verify_workbook(path, data, summary)
        log(f"Excel verificato: {path.name}")
    log(f"Confronto offline: {len(data['samples'])} campioni; report in {reports}"
        + (f"; report precedenti archiviati in {archived.name}" if archived else ""))
    return {"data": data, "summary": summary, "workbook": path}
