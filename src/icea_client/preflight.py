"""Offline checks of UAT PDFs and the approved ground truth. No network access."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from . import canonical
from .config import Paths
from .ground_truth_uat import GroundTruth
from contracts import CANONICAL_PROFILES, CONTRACTS, LEGACY_PROFILES
from evaluation import preflight as canonical_preflight
from metrics import sha256_of_file

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
# ICEA approval of the GT content is already acquired: the validation column is
# reported for information only and never blocks.
INFORMATIONAL_ISSUES = {"not_approved"}


def find_pdfs(samples_dir: Path, filename: str) -> list[Path]:
    direct = (samples_dir / filename).resolve()
    if direct.is_relative_to(samples_dir) and direct.is_file():
        return [direct]
    matches = [path.resolve() for path in samples_dir.rglob("*")
               if path.name == filename and path.is_file()]
    return [path for path in matches if path.is_relative_to(samples_dir)]


def find_pdf(samples_dir: Path, filename: str) -> Path | None:
    matches = find_pdfs(samples_dir, filename)
    return matches[0] if len(matches) == 1 else None


def page_count(path: Path) -> int:
    import pymupdf as fitz

    with fitz.open(path) as pdf:
        if not pdf.is_pdf or pdf.needs_pass:
            raise ValueError("unsupported or encrypted PDF")
        return len(pdf)


def profile_block(profile: str) -> str | None:
    """Only the server's canonical codes reach the API; historical codes get 422 there."""
    if profile in LEGACY_PROFILES:
        return "legacy_profile_rejected_by_api"
    if profile not in CANONICAL_PROFILES or profile not in CONTRACTS:
        return "unknown_profile"
    return None


def check_sample(document, base: Paths) -> dict:
    blocking, warnings = [], []
    blocked_profile = profile_block(document.profile)
    if blocked_profile:
        return {"sample_id": document.sample_id, "profile": document.profile,
                "filename": document.filename, "blocking": [blocked_profile],
                "warnings": [], "issues": {}, "ground_truth_rows": len(document.rows),
                "explicit_ambiguities": {"count": 0, "row_ids": []}, "informational": {}}
    candidates = find_pdfs(base.samples, document.filename)
    pdf = candidates[0] if len(candidates) == 1 else None
    if pdf is None:
        # As on the server: the GT rows are still checked against the registered
        # hash and page count, never against a substitute file.
        sha256, pages = document.sha256, document.page_count
        report = canonical_preflight(document, base.samples / document.filename,
                                     document.profile, sha256, pages)
        blocking = ["pdf_missing" if not candidates else "pdf_not_unique"]
        size = None
    else:
        sha256 = sha256_of_file(pdf)
        try:
            pages = page_count(pdf)
        except Exception:
            pages = -1
            blocking.append("pdf_unreadable")
        size = pdf.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            blocking.append("pdf_over_api_limit_20MiB")
        report = canonical_preflight(document, pdf, document.profile, sha256, pages)
        blocking = [*report["blocking"], *blocking]
    issues = {key: value for key, value in report["issues"].items()
              if key not in INFORMATIONAL_ISSUES}
    informational = {key: value["count"] for key, value in report["issues"].items()
                     if key in INFORMATIONAL_ISSUES}
    duplicates = [row_id for row_id, count in Counter(row.row_id for row in document.rows).items()
                  if count > 1]
    if duplicates:
        issues["duplicate_row_id"] = {"count": len(duplicates), "row_ids": duplicates}
    if not document.rows:
        blocking.append("no_ground_truth_rows")
    if issues:
        warnings.append("rows_not_evaluable")
    return {"sample_id": document.sample_id, "profile": document.profile,
            "filename": document.filename, "pdf_sha256": sha256 if pdf else None,
            "page_count": pages if pdf else None, "pdf_bytes": size,
            "ground_truth_rows": len(document.rows),
            "blocking": blocking, "warnings": warnings, "issues": issues,
            "explicit_ambiguities": report.get("explicit_ambiguities", {"count": 0, "row_ids": []}),
            "informational": informational}


def default_selection(gt: GroundTruth, samples: list[str] | None) -> tuple[list[str], list[dict]]:
    """Explicit samples are kept as given (an unsupported profile then blocks).

    Without a selection, samples whose profile is outside the server contract
    are excluded and listed, so the remaining samples can still be processed.
    """
    if samples:
        return list(samples), []
    selected, excluded = [], []
    for sample_id, document in sorted(gt.documents.items()):
        if profile_block(document.profile) is None:
            selected.append(sample_id)
        else:
            excluded.append({"sample_id": sample_id, "profile": document.profile,
                             "reason": profile_block(document.profile)})
    return selected, excluded


def run_preflight(gt: GroundTruth, base: Paths, samples: list[str] | None = None) -> dict:
    drift = canonical.verify_manifest()
    selected, excluded = default_selection(gt, samples)
    unknown = [sample for sample in selected if sample not in gt.documents]
    filenames = Counter(document.filename for document in gt.documents.values())
    results = [check_sample(gt.documents[sample], base) for sample in selected
               if sample in gt.documents]
    for result in results:
        if filenames[result["filename"]] > 1:
            result["blocking"].append("filename_shared_by_samples")
    blocking = bool(drift or unknown or any(result["blocking"] for result in results))
    return {"ground_truth_file": gt.path.name, "ground_truth_sha256": sha256_of_file(gt.path),
            "layout": Path(gt.layout_source).name, "sheets_ignored": gt.skipped_sheets,
            "canonical_drift": drift, "unknown_samples": unknown,
            "excluded_samples": excluded, "samples": results, "blocking": blocking}


def format_report(report: dict) -> str:
    """Counts and codes only: no expected or extracted values reach the console."""
    lines = [f"GT {report['ground_truth_file']} sha256={report['ground_truth_sha256'][:12]}… "
             f"layout={report['layout']}"]
    if report["canonical_drift"]:
        lines.append(f"BLOCKING canonical modules differ from MANIFEST: {report['canonical_drift']}")
    if report["unknown_samples"]:
        lines.append(f"BLOCKING unknown samples: {report['unknown_samples']}")
    for item in report.get("excluded_samples", []):
        lines.append(f"{item['sample_id']} {item['profile']} escluso: {item['reason']}")
    for item in report["samples"]:
        state = "BLOCKING " + ",".join(item["blocking"]) if item["blocking"] else "ok"
        issues = ", ".join(f"{key}={value['count']}" for key, value in item["issues"].items())
        lines.append(f"{item['sample_id']} {item['profile']} rows={item['ground_truth_rows']} "
                     f"pages={item.get('page_count', '?')} {state}"
                     + (f" | non valutabili: {issues}" if issues else "")
                     + (f" | Ambiguo: {item['explicit_ambiguities']['count']}"
                        if item["explicit_ambiguities"]["count"] else ""))
    return "\n".join(lines)
