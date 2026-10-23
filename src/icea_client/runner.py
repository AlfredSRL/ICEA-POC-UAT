"""Explicit, budgeted API runs. Dry-run unless the caller opts in to network calls."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import canonical  # noqa: F401
from .api_client import ApiClient
from .config import METHODS, Paths
from .preflight import find_pdf, profile_block, run_preflight
from metrics import sha256_of_file
from schemas import ExtractionResult


API_VERSION = "2.0"


class PublicExtraction(ExtractionResult):
    """API v2 extraction: the canonical result without the server-private strategy."""
    strategy: None = None


class RunRefused(RuntimeError):
    pass


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def load_records(runs_dir: Path, sample_id: str | None = None) -> list[dict]:
    pattern = f"{sample_id}/*/*/record.json" if sample_id else "*/*/*/record.json"
    records = []
    for path in sorted(runs_dir.glob(pattern)):
        record = json.loads(path.read_text(encoding="utf-8"))
        record["_dir"] = str(path.parent)
        records.append(record)
    return records


def same_configuration(record: dict, *, pdf_sha256: str, profile: str, method: str,
                       base_url: str) -> bool:
    return (record.get("pdf_sha256") == pdf_sha256 and record.get("profile") == profile
            and record.get("method") == method and record.get("extraction_option") == method
            and record.get("api_version") == API_VERSION and record.get("api_base_url") == base_url)


def plan_calls(gt, base: Paths, samples: list[str], methods: list[str], base_url: str,
               force: bool) -> dict:
    calls, skipped = [], []
    for sample_id in samples:
        document = gt.documents[sample_id]
        pdf = find_pdf(base.samples, document.filename)
        sha256 = sha256_of_file(pdf)
        done = [record for record in load_records(base.runs, sample_id)
                if record.get("status") == "succeeded"]
        for method in methods:
            completed = [record for record in done if same_configuration(
                record, pdf_sha256=sha256, profile=document.profile, method=method,
                base_url=base_url)]
            entry = {"sample_id": sample_id, "method": method, "pdf": pdf, "pdf_sha256": sha256,
                     "profile": document.profile, "filename": document.filename}
            if completed and not force:
                skipped.append({**entry, "previous_record": completed[-1]["_dir"]})
            else:
                calls.append(entry)
    return {"calls": calls, "skipped": skipped, "api_calls": len(calls)}


def format_budget(plan: dict) -> str:
    lines = [f"Budget: {plan['api_calls']} chiamate API di estrazione. "
             f"Già completate e saltate: {len(plan['skipped'])}."]
    for call in plan["calls"]:
        lines.append(f"  {call['sample_id']} {call['profile']} extraction_option={call['method']}")
    return "\n".join(lines)


def check_response(body: object, *, pdf_sha256: str, filename: str, profile: str,
                   page_count: int) -> str | None:
    if not isinstance(body, dict):
        return "body_not_object"
    if body.get("api_version") != API_VERSION:
        return "api_version_mismatch"
    try:
        extraction = PublicExtraction.model_validate(body.get("extraction"))
    except ValueError:
        return "extraction_not_canonical"
    if (extraction.source_sha256 != pdf_sha256 or extraction.document_profile_code != profile
            or extraction.source_file != filename):
        return "extraction_identity_mismatch"
    if any(field.page is not None and field.page > page_count for field in extraction.fields):
        return "page_outside_document"
    if not isinstance(body.get("field_issues"), list) or not isinstance(body.get("metrics"), dict):
        return "missing_field_issues_or_metrics"
    return None


def execute_call(client: ApiClient, call: dict, base: Paths, page_count: int) -> dict:
    if profile_block(call["profile"]):
        raise RunRefused(f"profile not accepted by the API: {call['profile']}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = base.runs / call["sample_id"] / call["method"] / stamp
    run_dir.mkdir(parents=True, exist_ok=False)
    result = client.extract(call["pdf"], call["filename"], call["profile"], call["method"])
    body = result["body"]
    # The full JSON response is kept verbatim, whatever its status.
    save_json(run_dir / "response.json", {"http_status": result["http_status"], "body": body})
    problem = None
    if result["http_status"] == 200 and result["error"] is None:
        problem = check_response(body, pdf_sha256=call["pdf_sha256"], filename=call["filename"],
                                 profile=call["profile"], page_count=page_count)
    succeeded = result["http_status"] == 200 and result["error"] is None and problem is None
    metrics = body.get("metrics") if succeeded else None
    record = {
        "sample_id": call["sample_id"], "method": call["method"],
        "extraction_option": call["method"], "api_version": API_VERSION,
        "profile": call["profile"], "filename": call["filename"],
        "pdf_sha256": call["pdf_sha256"], "api_base_url": client.base_url,
        "started_at": result["started_at"], "finished_at": result["finished_at"],
        "http_status": result["http_status"], "http_seconds": result["http_seconds"],
        "status": "succeeded" if succeeded else "failed",
        "error": result["error"] or (f"invalid_response:{problem}" if problem else None),
        "api_run_id": body.get("run_id") if isinstance(body, dict) else None,
        "pipeline_seconds": (metrics or {}).get("pipeline_seconds"),
    }
    save_json(run_dir / "record.json", record)
    return record


def run(gt, base: Paths, samples: list[str], *, methods: list[str] | None = None,
        execute: bool = False, approved_calls: int | None = None, force: bool = False,
        client_factory=None, base_url: str | None = None, log=print) -> dict:
    if not samples:
        raise RunRefused("an explicit list of sample IDs is required; no mass run")
    methods = list(methods or METHODS)
    if any(method not in METHODS for method in methods):
        raise RunRefused(f"unknown method; choose from {METHODS}")
    report = run_preflight(gt, base, samples)
    if report["blocking"]:
        raise RunRefused("preflight blocking issues; run `preflight` for details")
    pages = {item["sample_id"]: item["page_count"] for item in report["samples"]}
    settings = None
    if base_url is None:
        from .config import api_settings
        # The dry-run reads the configured URL too (no network), so its plan skips the
        # same completed combinations as the real run would.
        try:
            settings = api_settings()
        except ValueError:
            if execute:
                raise
            settings = None
        base_url = settings.base_url if settings else "(dry-run)"
    plan = plan_calls(gt, base, samples, methods, base_url, force)
    log(format_budget(plan))
    if not execute:
        log("DRY RUN: nessuna chiamata di rete. Usa --execute --approve-calls N.")
        return {"plan": plan, "records": [], "executed": False}
    if approved_calls != plan["api_calls"]:
        raise RunRefused(f"--approve-calls must equal the planned {plan['api_calls']} API calls")
    records = []
    if not plan["calls"]:
        return {"plan": plan, "records": records, "executed": True}
    if client_factory is None:
        if settings is None:
            from .config import api_settings
            settings = api_settings()
        client_factory = lambda: ApiClient(settings)  # noqa: E731
    with client_factory() as client:
        for call in plan["calls"]:
            record = execute_call(client, call, base, pages[call["sample_id"]])
            records.append(record)
            log(f"{record['sample_id']} {record['method']}: {record['status']}"
                + (f" ({record['error']}, HTTP {record['http_status']})" if record["error"] else "")
                + f" {record['http_seconds']:.1f}s")
    return {"plan": plan, "records": records, "executed": True}
