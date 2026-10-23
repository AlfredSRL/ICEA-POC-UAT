"""Prepare a local, row-level DEV comparison for the spreadsheet renderer.

Only existing, explicitly recorded DEV runs are read. No Azure calls occur.
"""
from __future__ import annotations

import json
import argparse
import math
import re
import statistics
from decimal import Decimal
from dataclasses import replace
from collections import Counter
from pathlib import Path

from contracts import CONTRACTS, LEGACY_FIELD_PATHS, REMOVED_FIELD_PATHS
from evaluation import equivalent, partly_equivalent, preflight, score
from ground_truth import DevDocument, load_registry
from metrics import sha256_of_file
from normalization import canonical, canonical_truth, is_list_value
from schemas import ExtractedField, ExtractionResult, validate_payload_with_field_issues

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "runtime"
DATA = ROOT / "data" / "dev"
WORKBOOK = DATA / "Ground_truth_DEV_ICEA_v1.8.xlsx"
METHODS = ("terra_direct", "luna_direct", "terra_di", "luna_di")
GT_ISSUES = {
    "unknown_field_path": "percorso non nel contratto",
    "class_mismatch": "classe GT diversa dal contratto",
    "rule_not_in_contract": "regola non definita nel contratto",
    "rule_mismatch": "regola GT diversa dal contratto",
    "missing_or_unknown_status": "stato GT mancante o ignoto",
    "missing_or_unknown_rule": "regola GT mancante o ignota",
    "missing_expected_value": "valore GT presente ma vuoto",
    "missing_group": "gruppo GT mancante",
}


def read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def _duration(info: dict | None) -> float | None:
    value = (info or {}).get("duration_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def _timing(model: dict, di: dict | None = None) -> dict:
    model_seconds = _duration(model)
    di_seconds = _duration(di) if di is not None else None
    return {"model_seconds": model_seconds, "di_seconds": di_seconds,
            "service_path_seconds": (model_seconds + di_seconds if di is not None
                                     and model_seconds is not None and di_seconds is not None
                                     else model_seconds if di is None else None)}


def _timing_stats(samples: list[dict], method: str) -> dict:
    entries = [sample["methods"][method] for sample in samples]
    def stats(key: str) -> dict:
        values = [item.get("timing", {}).get(key) for item in entries]
        values = [value for value in values if value is not None]
        return {"count": len(values), "mean_seconds": statistics.mean(values) if values else None,
                "median_seconds": statistics.median(values) if values else None,
                "total_seconds": sum(values) if values else None}
    return {"attempts": len(entries),
            "valid": sum(item["status"] == "succeeded" for item in entries),
            "model": stats("model_seconds"), "service_path": stats("service_path_seconds")}


def _json_value(value):
    return int(value) if isinstance(value,Decimal) and value==value.to_integral_value() else float(value) if isinstance(value,Decimal) else value


def _field_record(field, rule=None) -> dict:
    if not isinstance(field, dict):
        field = field.model_dump(mode="json")
    normalized = canonical(field.get("field_path", ""), rule,
                           field.get("value_raw"), field.get("value_normalized"),
                           text_evidence=field.get("text_evidence")) if rule else None
    return {
        "raw": field.get("value_raw"),
        "normalized": _json_value(normalized),
        "page": field.get("page"),
        "group": field.get("group_id"),
        "status": field.get("occurrence_status"),
        "evidence": field.get("text_evidence"),
    }


def comparison_rows(document: DevDocument, methods: dict) -> list[dict]:
    """Display every canonical occurrence, without presenting guessed groups as matches."""
    if set(methods) != set(METHODS):
        raise ValueError("four comparison methods required")
    rows, display_truth, source_rows = [], [], {}
    gt_occurrences = Counter()
    for source_index, original in enumerate(document.rows):
        items = (original.value_raw.split(";") if is_list_value(original.field_path)
                 and ";" in (original.value_raw or "") else [original.value_raw])
        normalized_items = (original.value_normalized or "").split(";")
        for item_index, raw in enumerate(items, 1):
            normalized = (normalized_items[item_index-1].strip() if len(items) > 1
                          and len(normalized_items) == len(items) else original.value_normalized if len(items) == 1 else None)
            row = replace(original, value_raw=raw.strip() if raw is not None else None,
                          value_normalized=normalized)
            source_rows.setdefault(source_index, []).append(len(rows))
            gt_occurrences[(row.field_path, row.group_id)] += 1
            display_truth.append(row)
            value = canonical_truth(row.field_path, row.rule, row.value_raw, row.value_normalized)
            rows.append({"kind": "GT", "source": None, "row_id": row.row_id,
                         "source_truth_index": source_index,
                         "gt_item_index": gt_occurrences[(row.field_path,row.group_id)],
                         "source_gt_raw": original.value_raw if len(items)>1 else None,
                         "field_path": row.field_path, "role": row.role, "gt_group": row.group_id,
                         "gt_page": row.page, "gt_status": row.status, "gt_raw": row.value_raw,
                         "gt_normalized": (_json_value(value) if row.rule == "numeric_equivalent"
                                           else row.value_normalized),
                         "gt_source_normalized": row.value_normalized,
                         "gt_canonical": _json_value(value),
                         "rule": row.rule, "validation": row.validation,
                         "gt_issue": None, "methods": {}})
    continuations = {}
    path_rows = {}
    for index, row in enumerate(rows):
        path_rows.setdefault(row["field_path"], []).append(index)
    for method in METHODS:
        run = methods[method]
        diagnostic = None
        if run["status"] == "succeeded":
            extraction = run["extraction"]
            field_issues = run.get("field_issues", [])
            report = score(document, extraction, field_issues)
            fields = extraction.fields
        elif run["status"] == "failed":
            parsed = run.get("parsed") or {}
            fields = parsed.get("fields") or []
            diagnostic = run.get("diagnostic")
            report = diagnostic["score"] if diagnostic else None
        else:
            raise ValueError(f"unexpected method status: {method}")
        display_fields, original_fields, counts = [], {}, Counter()
        for original_index, field in enumerate(fields):
            payload = field if isinstance(field, dict) else field.model_dump(mode="json")
            raw = payload.get("value_raw")
            items = raw.split(";") if is_list_value(payload.get("field_path", "")) and isinstance(raw,str) and ";" in raw else [raw]
            for raw_item in items:
                record = {**payload, "value_raw": raw_item.strip() if raw_item is not None else None,
                          "value_normalized": payload.get("value_normalized") if len(items)==1 else None}
                counts[(record.get("field_path"),record.get("group_id"))] += 1
                record["review_item_index"] = counts[(record.get("field_path"),record.get("group_id"))]
                original_fields.setdefault(original_index,[]).append(len(display_fields))
                display_fields.append(record)
        quarantined_display = ({index for original_index in
                                diagnostic.get("quarantined_indices", [diagnostic["quarantined_index"]])
                                for index in original_fields.get(original_index, [])}
                               if diagnostic else set())
        used = set()
        displayed_issues = set()
        accepted_display = {display for original in ((report or {}).get("accepted_alternative_indices", [])
                                                     if not diagnostic else [])
                            for display in original_fields.get(original, [])}
        for row in rows:
            row["methods"][method] = {"raw":None,"normalized":None,"page":None,"group":None,
                                      "status":None,"outcome":"schema non valido" if run["status"] == "failed" else "non valutata"}
        if report:
            for result in report["rows"]:
                targets = source_rows[result["truth_index"]]
                if "field_issue_index" in result:
                    issue_index = result["field_issue_index"]
                    issue = field_issues[issue_index]
                    displayed_issues.add(issue_index)
                    for target in targets:
                        rows[target]["methods"][method] = {
                            "raw": issue["value_display"], "normalized": issue["value_display"],
                            "page": issue["page"], "group": issue["group_id"],
                            "status": "collisione_cardinalita", "outcome": result["outcome"],
                            "field_issue_label": issue["label"],
                            "alignment": "più valori per un campo singolo: partial se uno coincide, altrimenti incorrect",
                            "display_attached": True}
                    continue
                original_index = result["extraction_index"]
                if original_index is not None and diagnostic:
                    original_index = diagnostic["kept_indices"][original_index]
                source_indices = result.get("extraction_indices") or (
                    [result["extraction_index"]] if result["extraction_index"] is not None else [])
                if diagnostic:
                    source_indices = [diagnostic["kept_indices"][index] for index in source_indices]
                for target in targets:
                    if diagnostic:
                        rows[target]["methods"][method]["diagnostic_outcome"] = result["outcome"]
                    else:
                        rows[target]["methods"][method]["outcome"] = (result["outcome"] if len(targets)==1
                                                                      else "lista GT espansa: gruppo da verificare")
                if source_indices and len(targets)==1 and all(
                        len(original_fields[source_index]) == 1 for source_index in source_indices):
                    display_indices = [original_fields[source_index][0] for source_index in source_indices]
                    records = [display_fields[index] for index in display_indices]
                    scored_field = (diagnostic["extraction"].fields[result["extraction_index"]]
                                    if diagnostic else fields[original_index])
                    if len(records) == 1 and result["outcome"] == "incorrect" and any(
                        other != targets[0] and rows[other]["field_path"] == rows[targets[0]]["field_path"]
                        and equivalent(display_truth[other], scored_field, compare_group=False)
                        for other in path_rows[rows[targets[0]]["field_path"]]
                    ):
                        continue
                    used.update(display_indices)
                    record = (records[0] if len(records) == 1 else {
                        **records[0], "value_raw": " ".join(item["value_raw"] for item in records),
                        "value_normalized": None})
                    rows[targets[0]]["methods"][method] = {
                        **_field_record(record,result["rule"]),
                        "outcome":"schema non valido" if diagnostic else result["outcome"],
                        **({"diagnostic_outcome":result["outcome"]} if diagnostic else {}),
                        "item_index":(records[0]["review_item_index"] if len(records) == 1
                                      else "+".join(str(item["review_item_index"]) for item in records)),
                        "display_attached":True,
                        **({"source_field_indices": source_indices} if len(records) > 1 else {}),
                        "alignment":(f"valore riportato in {result['alternative_field_path']}"
                                     if result.get("alternative_field_path")
                                     else "allineata dallo scorer" if len(records) == 1
                                     else f"frammenti ricomposti ({len(records)} occorrenze)")}
            for item in report["not_ready"]:
                for target in source_rows[item["truth_index"]]:
                    if not diagnostic:
                        rows[target]["methods"][method]["outcome"] = "GT non valutabile"
                    rows[target]["gt_issue"] = "; ".join(
                        GT_ISSUES.get(reason, reason) for reason in item["reasons"])
        overflow = {}
        validated = {}
        priorities = {}
        for index, payload in enumerate(display_fields):
            candidate = {key:value for key,value in payload.items() if key != "review_item_index"}
            try:
                model_field = ExtractedField.model_validate(candidate,context={"legacy_boolean":True})
            except ValueError:
                model_field = None
            validated[index] = model_field
            targets = path_rows.get(payload.get("field_path"), [])
            if index in quarantined_display:
                priorities[index] = 4
            elif payload.get("occurrence_status") != "present":
                priorities[index] = 3
            elif any(model_field and equivalent(display_truth[target],model_field,compare_group=False)
                     for target in targets):
                priorities[index] = 0
            elif any(model_field and partly_equivalent(display_truth[target],model_field)
                     for target in targets):
                priorities[index] = 1
            else:
                priorities[index] = 2
        for index in sorted(range(len(display_fields)),key=lambda item:(priorities[item],item)):
            payload = display_fields[index]
            if index in used:
                continue
            quarantined = index in quarantined_display
            path = payload.get("field_path")
            targets = path_rows.get(path, [])
            vacant = [target for target in targets
                      if not rows[target]["methods"][method].get("display_attached")]
            if quarantined:
                vacant = []
            if payload.get("occurrence_status") != "present":
                vacant = [target for target in vacant if display_truth[target].status == "Assente"]
            if vacant:
                model_field = validated[index]
                matching = [target for target in vacant if model_field and equivalent(display_truth[target],model_field,compare_group=False)]
                partials = [target for target in vacant if model_field and partly_equivalent(display_truth[target],model_field)]
                target = (matching or partials or vacant)[0]
                previous = rows[target]["methods"].get(method, {})
                value_outcome = ("matched" if index in accepted_display
                                 else "matched" if display_truth[target].status == "Assente"
                                 and payload.get("occurrence_status") == "not_present"
                                 else "omitted" if payload.get("occurrence_status") != "present"
                                 else "matched" if target in matching else "partial" if target in partials
                                 else "incorrect" if model_field else "non valutato")
                rows[target]["methods"][method] = {
                    **_field_record(payload, rows[target]["rule"]),
                    "outcome": previous.get("outcome", "non valutata"),
                    **({"diagnostic_outcome":value_outcome} if diagnostic else {"value_outcome":value_outcome}),
                    "item_index":payload["review_item_index"],
                    "alignment": ("quantità di riga riportata in un altro campo: accettata"
                                  if index in accepted_display else "sequenziale non verificato"),
                    "display_attached": True,
                }
                used.add(index)
                continue
            if payload.get("occurrence_status") != "present" and not quarantined:
                continue
            ordinal = overflow.get(path, 0)
            overflow[path] = ordinal + 1
            key = (path, ordinal)
            if key not in continuations:
                kind = "QUARANTENA" if quarantined else "CONTINUAZIONE" if targets else "MODEL_ONLY"
                continuations[key] = _extra_row(kind, None, index, payload, "non appaiata")
                continuations[key]["gt_item_index"] = len(targets) + ordinal + 1
                continuations[key]["methods"] = {}
            rule = rows[targets[0]]["rule"] if targets else None
            continuations[key]["methods"][method] = {
                **_field_record(payload, rule),
                "outcome": ("valore alternativo accettato" if index in accepted_display
                            else "non appaiata" if run["status"] == "succeeded" else "schema non valido"),
                "item_index":payload["review_item_index"],
                "alignment": (f"esclusa dalla diagnostica: {diagnostic.get('quarantine_reason', 'schema incoerente')}"
                              if quarantined else "sequenziale non verificato"),
            }
        for issue_index, issue in enumerate(run.get("field_issues", [])):
            if issue_index in displayed_issues:
                continue
            path = issue["field_path"]
            ordinal = overflow.get(path, 0)
            overflow[path] = ordinal + 1
            key = (path, ordinal)
            if key not in continuations:
                continuations[key] = {
                    "kind": "QUARANTENA", "source": None, "row_id": None,
                    "field_path": path, "role": None, "gt_group": None, "gt_page": None,
                    "gt_status": None, "gt_raw": None, "gt_normalized": None,
                    "rule": None, "validation": None,
                    "gt_item_index": len(path_rows.get(path, [])) + ordinal + 1,
                    "methods": {}}
            continuations[key]["methods"][method] = {
                "raw": issue["value_display"], "normalized": issue["value_display"],
                "page": issue["page"], "group": issue["group_id"],
                "status": "invalid_field", "outcome": "invalid_field",
                "field_issue_label": issue["label"],
                "alignment": "gruppo non appaiato; candidati non confrontati"}
        if report and not diagnostic:
            for row in rows:
                value=row["methods"][method]
                if row["gt_status"] == "Assente" and not value.get("display_attached"):
                    value["value_outcome"]="matched"
                elif row["gt_status"] == "Presente" and not value.get("display_attached"):
                    value["value_outcome"]="omitted"
    output = []
    for index, row in enumerate(rows):
        output.append(row)
        if path_rows[row["field_path"]][-1] == index:
            output.extend(value for (path, _), value in continuations.items() if path == row["field_path"])
    output.extend(value for (path, _), value in continuations.items() if path not in path_rows)
    return output


def _extra_row(kind: str, method: str, index: int, field, outcome: str) -> dict:
    if not isinstance(field, dict):
        field = field.model_dump(mode="json")
    return {"kind": kind, "source": method, "row_id": None,
            "field_path": field.get("field_path"), "role": None,
            "gt_group": None, "gt_page": None, "gt_status": None,
            "gt_raw": None, "gt_normalized": None, "rule": None,
            "validation": None, "ordinal": index + 1,
            "methods": {method: {**_field_record(field), "outcome": outcome}}}


def _check_config(config: dict, document: DevDocument, gt_hash: str,
                  strategy: str, dpi: int | None = None, *, updated_gt=False) -> int:
    expected = {"sample_id": document.sample_id, "profile": document.profile,
                "source_sha256": document.sha256,
                "strategy": strategy}
    if any(config.get(key) != value for key, value in expected.items()):
        raise ValueError(f"run identity mismatch: {document.sample_id}")
    if not updated_gt and config.get("ground_truth_sha256") != gt_hash:
        raise ValueError(f"run ground truth mismatch: {document.sample_id}")
    if updated_gt and (not isinstance(config.get("ground_truth_sha256"), str)
                       or len(config["ground_truth_sha256"]) != 64):
        raise ValueError(f"archived ground truth hash missing: {document.sample_id}")
    if dpi is not None and config.get("dpi") != dpi:
        raise ValueError(f"run DPI mismatch: {document.sample_id}")
    return config["dpi"]


def _terra_artifacts(direct_dir: Path, terra_info: dict) -> tuple[Path, str]:
    source = terra_info.get("source")
    if source == "new":
        return direct_dir / "terra", direct_dir.name
    if source == "reused":
        run_id = terra_info.get("run_id")
        if not isinstance(run_id, str) or not re.fullmatch(r"\d{8}T\d{6}Z", run_id):
            raise ValueError("invalid reused Terra run ID")
        return RUNTIME / "runs" / run_id, run_id
    raise ValueError("unknown Terra artifact source")


def _diagnose_isolated_invalid_fields(payload: dict, document: DevDocument,
                                      strategy: str) -> dict | None:
    """Quarantine an isolated status error or one cardinality collision, never official scoring."""
    invalid = []
    for index, field in enumerate(payload.get("fields", [])):
        try:
            ExtractedField.model_validate(field)
        except ValueError as error:
            invalid.append((index, str(error)))
    if invalid:
        if len(invalid) != 1:
            return None
        bad_index, reason = invalid[0]
        bad = payload["fields"][bad_index]
        if (bad.get("occurrence_status") not in {"ambiguous", "not_present", "illegible"}
                or (bad.get("value_raw") is None and bad.get("value_normalized") is None)):
            return None
        quarantined_indices = [bad_index]
        quarantine_reason = "stato e valori incoerenti"
    else:
        buckets = {}
        catalog = CONTRACTS[document.profile]
        for index, field in enumerate(payload["fields"]):
            path = field.get("field_path")
            spec = catalog.get(path)
            if (spec and spec.cardinality in {"0..1 per riga", "1 per materiale", "1 per voce"}
                    and field.get("occurrence_status") == "present"):
                buckets.setdefault((path, field.get("group_id")), []).append(index)
        collisions = [indices for indices in buckets.values() if len(indices) > 1]
        if len(collisions) != 1:
            return None
        quarantined_indices = collisions[0]
        reason = "too many occurrences per group"
        quarantine_reason = "più valori nello stesso gruppo"
    bad_index = quarantined_indices[0]
    bad = payload["fields"][bad_index]
    excluded = set(quarantined_indices)
    kept_indices = [index for index in range(len(payload["fields"])) if index not in excluded]
    candidate = {**payload, "fields": [payload["fields"][index] for index in kept_indices]}
    try:
        extraction = ExtractionResult.model_validate(candidate)
    except ValueError:
        return None
    if (extraction.source_file != document.filename or extraction.source_sha256 != document.sha256
            or extraction.document_profile_code != document.profile
            or extraction.strategy != strategy
            or any(field.page is not None and field.page > document.page_count
                   for field in extraction.fields)):
        return None
    return {"extraction": extraction, "kept_indices": kept_indices,
            "quarantined_index": bad_index, "quarantined_indices": quarantined_indices,
            "quarantined_path": bad.get("field_path"), "quarantine_reason": quarantine_reason,
            "reason": reason}


def _load_method(base: Path, status: str, document: DevDocument,
                 strategy: str, error_category: str | None = None) -> dict:
    if status == "succeeded":
        payload = read_json(base / "extraction.json")
    elif status == "failed" and error_category == "schema":
        response = read_json(base / "model_response.json")
        parsed = response.get("parsed")
        if not isinstance(parsed, dict) or not isinstance(parsed.get("fields"), list):
            raise ValueError(f"invalid response unavailable: {base}")
        payload = {**parsed, "source_file": document.filename,
                   "source_sha256": document.sha256, "document_profile_code": document.profile,
                   "strategy": strategy}
    else:
        raise ValueError(f"missing or non-schema extraction: {base}")
    migrated = []
    removed = sorted({field.get("field_path") for field in payload.get("fields", [])
                      if field.get("field_path") in REMOVED_FIELD_PATHS})
    payload["fields"] = [field for field in payload.get("fields", [])
                         if field.get("field_path") not in REMOVED_FIELD_PATHS]
    for field in payload.get("fields", []):
        old_path = field.get("field_path")
        if old_path in LEGACY_FIELD_PATHS:
            field["field_path"] = LEGACY_FIELD_PATHS[old_path]
            migrated.append(old_path)
    legacy_shipment_reference = "invoice.shipment_reference" in migrated
    try:
        for context in ({}, {"legacy_boolean": True}, {"legacy_group": True},
                        {"legacy_boolean": True, "legacy_group": True}):
            try:
                extraction = ExtractionResult.model_validate(
                    payload, context={**context, "legacy_shipment_reference": legacy_shipment_reference})
                break
            except ValueError:
                if context.get("legacy_boolean") and context.get("legacy_group"):
                    raise
        legacy_boolean = bool(context.get("legacy_boolean"))
        legacy_group = bool(context.get("legacy_group"))
        if (extraction.source_file != document.filename
                or extraction.strategy != strategy
                or extraction.document_profile_code != document.profile
                or extraction.source_sha256 != document.sha256):
            raise ValueError(f"extraction identity mismatch: {base}")
        if any(field.page is not None and field.page > document.page_count for field in extraction.fields):
            raise ValueError(f"extraction page outside DEV document: {base}")
        return {"status": "succeeded", "extraction": extraction,
                "archived_status": status, "offline_revalidated": status != "succeeded",
                 "field_issues": (read_json(base / "field_issues.json")
                                  if (base / "field_issues.json").is_file() else []),
                "migrated_paths": sorted(set(migrated)),
                "removed_paths": removed,
                "legacy_shipment_reference_compatible": legacy_shipment_reference,
                "legacy_boolean_compatible": legacy_boolean,
                "legacy_group_compatible": legacy_group}
    except ValueError as error:
        if status == "succeeded":
            raise ValueError(f"archived DEV extraction invalid: {base}") from error
        try:
            extraction, field_issues = validate_payload_with_field_issues(
                {key: payload[key] for key in ("processing_status", "warnings", "fields")
                 if key in payload},
                source_file=document.filename, source_sha256=document.sha256,
                profile=document.profile, strategy=strategy, page_count=document.page_count)
        except ValueError:
            pass
        else:
            if field_issues:
                return {"status": "succeeded", "extraction": extraction,
                        "field_issues": field_issues, "archived_status": status,
                        "offline_revalidated": True,
                        "migrated_paths": sorted(set(migrated)),
                        "legacy_shipment_reference_compatible": legacy_shipment_reference,
                        "legacy_boolean_compatible": False, "legacy_group_compatible": False}
        diagnostic = _diagnose_isolated_invalid_fields(payload, document, strategy)
        if diagnostic:
            diagnostic["score"] = score(document, diagnostic["extraction"])
        return {"status": "failed", "parsed": payload, "validation_error": str(error),
                "diagnostic": diagnostic, "archived_status": status,
                 "migrated_paths": sorted(set(migrated)), "field_issues": []}


def build_export(*, updated_gt=False, workbook_path: Path = WORKBOOK) -> dict:
    import pymupdf as fitz

    workbook_path = workbook_path.resolve()
    if not (workbook_path.is_relative_to(DATA.resolve()) or workbook_path.is_relative_to(RUNTIME.resolve())):
        raise ValueError("DEV ground truth must remain in data/dev or runtime")
    registry = load_registry(workbook_path)
    direct = read_json(RUNTIME / "comparison_summary.json")
    assisted = read_json(RUNTIME / "comparison_summary_di_assisted.json")
    direct_by_id = {item["sample_id"]: item for item in direct["samples"]}
    assisted_by_id = {item["sample_id"]: item for item in assisted["samples"]}
    if (len(direct_by_id) != len(direct["samples"])
            or len(assisted_by_id) != len(assisted["samples"])
            or set(registry) != set(direct_by_id) or set(registry) != set(assisted_by_id)):
        raise ValueError("DEV sample coverage mismatch")
    gt_hash = sha256_of_file(workbook_path)
    samples = []
    for sample_id, document in sorted(registry.items()):
        pdf_path = (DATA / document.filename).resolve()
        if not pdf_path.is_relative_to(DATA.resolve()):
            raise ValueError("registered DEV PDF outside data/dev")
        pdf_hash = sha256_of_file(pdf_path)
        if pdf_hash != document.sha256:
            raise ValueError(f"registered PDF hash mismatch: {sample_id}")
        with fitz.open(pdf_path) as pdf:
            check = preflight(document,pdf_path,document.profile,pdf_hash,len(pdf))
        if check["blocking"]:
            raise ValueError(f"DEV preflight blocked: {sample_id}: {check['blocking']}")
        old, new = direct_by_id[sample_id], assisted_by_id[sample_id]
        if old["profile"] != document.profile or new["profile"] != document.profile:
            raise ValueError(f"profile mismatch: {sample_id}")
        if new["direct_run_id"] != old["run_id"]:
            raise ValueError(f"direct run mismatch: {sample_id}")
        direct_dir = RUNTIME / "comparisons" / "runs" / old["run_id"]
        di_dir = RUNTIME / "di_workflow" / "runs" / new["di_workflow_run_id"]
        direct_config = read_json(direct_dir / "config.json")
        assisted_config = read_json(di_dir / "config.json")
        dpi = _check_config(direct_config, document,
                            gt_hash, "vision_direct", updated_gt=updated_gt)
        _check_config(assisted_config, document,
                      gt_hash, "layout_assisted", dpi, updated_gt=updated_gt)
        terra_info = old["backends"]["terra"]
        terra_dir, terra_run_id = _terra_artifacts(direct_dir, terra_info)
        terra_config = (direct_config if terra_info.get("source") == "new"
                        else read_json(terra_dir / "config.json"))
        _check_config(terra_config, document,
                      gt_hash, "vision_direct", dpi, updated_gt=updated_gt)
        methods = {
            "terra_direct": _load_method(terra_dir, terra_info["status"],
                                         document, "vision_direct",
                                         terra_info.get("error_category")),
            "luna_direct": _load_method(direct_dir / "luna", old["backends"]["luna"]["status"],
                                        document, "vision_direct",
                                        old["backends"]["luna"].get("error_category")),
            "terra_di": _load_method(di_dir / "terra", new["models"]["terra"]["di_assisted"]["status"],
                                     document, "layout_assisted",
                                     new["models"]["terra"]["di_assisted"].get("error_category")),
            "luna_di": _load_method(di_dir / "luna", new["models"]["luna"]["di_assisted"]["status"],
                                    document, "layout_assisted",
                                    new["models"]["luna"]["di_assisted"].get("error_category")),
        }
        timings = {
            "terra_direct": _timing(old["backends"]["terra"]),
            "luna_direct": _timing(old["backends"]["luna"]),
            "terra_di": _timing(new["models"]["terra"]["di_assisted"],
                                new["document_intelligence"]),
            "luna_di": _timing(new["models"]["luna"]["di_assisted"],
                               new["document_intelligence"]),
        }
        rows = comparison_rows(document, methods)
        samples.append({"sample_id": sample_id, "profile": document.profile,
                        "filename": document.filename,
                        "source_sha256": document.sha256, "ground_truth_sha256": gt_hash,
                        "direct_run_id": old["run_id"],
                        "terra_run_id": terra_run_id,
                        "di_run_id": new["di_workflow_run_id"],
                        "preflight":check,
                        "document_intelligence_timing": {
                            "direct_seconds": _duration(old["backends"].get("document_intelligence")),
                            "assisted_seconds": _duration(new.get("document_intelligence"))},
                        "archived_gt_hashes": {"direct": direct_config["ground_truth_sha256"],
                            "assisted": assisted_config["ground_truth_sha256"],
                            "terra": terra_config["ground_truth_sha256"]},
                        "methods": {key: {"status": value["status"],
                            "archived_status": value.get("archived_status"),
                            "offline_revalidated": value.get("offline_revalidated", False),
                            "migrated_paths": value.get("migrated_paths", []),
                            "legacy_shipment_reference_compatible": value.get(
                                "legacy_shipment_reference_compatible", False),
                             "field_issues": value.get("field_issues", []),
                            "timing": timings[key],
                            "diagnostic": ({"quarantined_index": value["diagnostic"]["quarantined_index"],
                                            "quarantined_indices": value["diagnostic"]["quarantined_indices"],
                                            "quarantined_path": value["diagnostic"]["quarantined_path"],
                                            "quarantine_reason": value["diagnostic"]["quarantine_reason"],
                                            "counts": {name: value["diagnostic"]["score"].get(name, 0)
                                                       for name in ("matched", "partial", "incorrect", "omitted", "unresolved_group")}}
                                           if value.get("diagnostic") else None),
                             "score": score(document, value["extraction"], value.get("field_issues"))
                                      if value["status"] == "succeeded" else None}
                            for key, value in methods.items()},
                        "gt_rows": len(document.rows), "rows": rows})
    return {"version": 2, "methods": list(METHODS), "samples": samples,
            "ground_truth_sha256": gt_hash,
            "ground_truth_file": str(workbook_path.relative_to(ROOT)),
            "reevaluated_updated_gt": updated_gt,
            "note": "Score provvisorio sulle run selezionate. DI non è un estrattore canonico autonomo."}


def reevaluation_summary(data: dict) -> tuple[dict, str]:
    keys=("evaluable_rows","not_ready_rows","matched","partial","incorrect","omitted",
          "unassessed","unresolved_group","unpaired_occurrences","invalid_field","field_issue_count")
    totals={method:{key:0 for key in keys} for method in METHODS}
    samples=[]
    gt_status = (f"La GT DEV {WORKBOOK.stem.rsplit('_', 1)[-1]} è approvata; lo score delle run selezionate resta provvisorio."
                 if data["ground_truth_file"] == str(WORKBOOK.relative_to(ROOT)).replace("\\", "/")
                 else "Lo stato di approvazione della GT selezionata va verificato; lo score resta provvisorio.")
    lines=["# Confronto offline delle run DEV", "", "Questo export non effettua chiamate Azure e non modifica le risposte delle run selezionate.",
           "Le quattro soluzioni sono Terra diretta, Luna diretta, DI → Terra e DI → Luna. DI da sola è OCR/layout, non un estrattore canonico.",
           "", gt_status + " `partial` non vale come corrispondenza piena. Nell'Excel l'esito colorato confronta il valore; il motivo GT e l'allineamento del gruppo sono separati. Lo score in questo riepilogo resta conservativo.",
           "", f"GT usata: `{data['ground_truth_file']}`; SHA-256 `{data['ground_truth_sha256']}`.", "",
            "| Campione | Soluzione | Match | Parziali | Discordanti | Omesse | Campi invalidi | Non valutate | Gruppi irrisolti | GT non pronta |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for sample in data["samples"]:
        methods={}
        for method,info in sample["methods"].items():
            report=info["score"]
            counts={key:report.get(key,0) for key in keys} if report else None
            methods[method]={**{key:info[key] for key in ("status","archived_status","offline_revalidated","migrated_paths")},
                             "legacy_shipment_reference_compatible": info.get("legacy_shipment_reference_compatible",False),
                             "diagnostic": info.get("diagnostic"),
                             "field_issue_count": len(info.get("field_issues", [])),
                             "timing": info.get("timing", {}),
                             "counts":counts}
            if counts:
                for key in keys:
                    totals[method][key]+=counts[key]
                lines.append(f"| {sample['sample_id']} | {method} | " + " | ".join(str(counts[key]) for key in
                             ("matched","partial","incorrect","omitted","invalid_field","unassessed","unresolved_group","not_ready_rows")) + " |")
            else:
                lines.append(f"| {sample['sample_id']} | {method} | schema non valido | | | | | | | |")
        samples.append({"sample_id":sample["sample_id"],"profile":sample["profile"],
                        "direct_run_id":sample["direct_run_id"],"di_run_id":sample["di_run_id"],
                        "document_intelligence_timing": sample.get("document_intelligence_timing", {}),
                        "gt_rows":sample["gt_rows"],"display_gt_rows":sum(row["kind"]=="GT" for row in sample["rows"]),
                        "archived_gt_hashes":sample["archived_gt_hashes"],"methods":methods})
    lines += ["", "## Totali per soluzione", "",
               "| Soluzione | Valutabili | Match | Parziali | Discordanti | Omesse | Campi invalidi | Gruppi irrisolti | Occorrenze non appaiate |",
               "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for method in METHODS:
        counts = totals[method]
        lines.append(f"| {method} | " + " | ".join(str(counts[key]) for key in
                      ("evaluable_rows", "matched", "partial", "incorrect", "omitted", "invalid_field",
                      "unresolved_group", "unpaired_occurrences")) + " |")
    timing_totals = {method: _timing_stats(samples, method) for method in METHODS}
    timing_profiles = {profile: {method: _timing_stats(
        [sample for sample in samples if sample["profile"] == profile], method)
        for method in METHODS} for profile in sorted({sample["profile"] for sample in samples})}
    def seconds(value):
        return f"{value:.2f}" if value is not None else "—"
    lines += ["", "## Tempi delle chiamate", "",
              "Il tempo modello include la chiamata remota, anche per risposte poi respinte dallo schema. "
              "La pipeline assistita somma modello e DI: non include rendering, validazione o I/O locale. "
              "La chiamata DI è condivisa dai due modelli e non va sommata due volte come consumo totale.", "",
              "| Campione | Soluzione | Schema | Modello (s) | DI (s) | Pipeline servizi (s) |",
              "|---|---|---|---:|---:|---:|"]
    for sample in samples:
        for method in METHODS:
            item = sample["methods"][method]
            timing = item["timing"]
            lines.append(f"| {sample['sample_id']} | {method} | {item['status']} | "
                         f"{seconds(timing.get('model_seconds'))} | "
                         f"{seconds(timing.get('di_seconds'))} | "
                         f"{seconds(timing.get('service_path_seconds'))} |")
    lines += ["", "| Soluzione | Chiamate misurate | Valide | Modello medio (s) | Modello mediano (s) | Pipeline media (s) | Pipeline mediana (s) |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for method in METHODS:
        item = timing_totals[method]
        lines.append(f"| {method} | {item['model']['count']}/{item['attempts']} | {item['valid']} | "
                     f"{seconds(item['model']['mean_seconds'])} | {seconds(item['model']['median_seconds'])} | "
                     f"{seconds(item['service_path']['mean_seconds'])} | "
                     f"{seconds(item['service_path']['median_seconds'])} |")
    lines += ["", "### Tempi per profilo", "",
              "| Profilo | Soluzione | Chiamate misurate | Valide | Modello medio (s) | Pipeline media (s) |",
              "|---|---|---:|---:|---:|---:|"]
    for profile, methods_by_profile in timing_profiles.items():
        for method, item in methods_by_profile.items():
            lines.append(f"| {profile} | {method} | {item['model']['count']}/{item['attempts']} | "
                         f"{item['valid']} | {seconds(item['model']['mean_seconds'])} | "
                         f"{seconds(item['service_path']['mean_seconds'])} |")
    reasons=Counter(reason for sample in data["samples"]
                    for item in (sample["methods"]["terra_direct"]["score"] or {}).get("not_ready",[])
                    for reason in item["reasons"])
    if reasons:
        lines += ["", "Righe GT non valutabili per causa (una riga può avere più cause): "
                  + ", ".join(f"{GT_ISSUES.get(reason,reason)}={count}" for reason,count in sorted(reasons.items())) + "."]
    lines += ["", "Le liste GT esplicite separate da `;` sono espanse nella vista con indice interno e riga sorgente conservata. "
              "Gli indici ricavati dall'ordine delle estrazioni non provano l'appaiamento tra campi fratelli. "
              "Valori condivisi da una testata e ripetuti per riga restano visibili come continuazioni, non come invenzioni.",
              "", "Le risposte non conformi al contratto restano segnalate come schema non valido; "
              "i loro singoli valori non entrano nei totali dello score. La propagazione dei valori "
              "condivisi tra gruppi richiede ancora verifica documentale."]
    return {"provisional":True,"azure_calls_in_export":0,"ground_truth_sha256":data["ground_truth_sha256"],
            "ground_truth_file":data["ground_truth_file"],"totals":totals,
            "timing_totals": timing_totals, "timing_by_profile": timing_profiles,
            "samples":samples}, "\n".join(lines)+"\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reevaluate-updated-gt", action="store_true",
                        help="Explicit offline re-scoring; preserve original run configurations and hashes")
    parser.add_argument("--ground-truth", type=Path, default=WORKBOOK,
                        help="Current DEV workbook or a reviewed local migration candidate")
    args = parser.parse_args()
    output = RUNTIME / "field_comparison_data.json"
    data = build_export(updated_gt=args.reevaluate_updated_gt, workbook_path=args.ground_truth)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    summary, markdown = reevaluation_summary(data)
    (RUNTIME / "reevaluation_summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    (RUNTIME / "reevaluation_summary.md").write_text(markdown,encoding="utf-8")
    print(f"Prepared {len(data['samples'])} DEV samples in {output}")
