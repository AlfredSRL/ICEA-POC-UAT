"""Client-side scoring adaptations, applied to in-memory copies of the GT document and
of the extraction before the canonical scorer runs. The vendored server modules, the GT
workbook and the archived responses stay unchanged.

UAT decisions (20 October 2026):
1. A boolean judgment field of the profile that the model does not emit at all counts
   as `false`. An emitted `ambiguous` or `illegible` occurrence is left as it is.
2. On any other field, the value `false` means "empty": a GT row `Presente` with value
   `false` is scored as an expected absence, so a model that emits nothing (or
   `not_present`) is a match; a model occurrence whose value is `false` is read as
   `not_present`. Example: `tc.buyer.buying_on_behalf_of` of the TC sheets.
"""
from __future__ import annotations

from . import canonical  # noqa: F401
from dataclasses import replace

from contracts import CONTRACTS
from schemas import ExtractedField, ExtractionResult, OccurrenceStatus

IMPLICIT_FALSE_EVIDENCE = "false implicito: campo booleano non emesso dal modello (regola client UAT)"


def boolean_paths(profile: str) -> list[str]:
    return sorted(path for path, spec in CONTRACTS[profile].items() if spec.rule == "boolean_judgment")


def with_implicit_false(extraction: ExtractionResult, document) -> tuple[ExtractionResult, list[str]]:
    """Return a copy with `false` added for every boolean path absent from the output.

    Only paths that the GT scores are completed: elsewhere an implicit value would only
    appear as an unpaired occurrence. The GT value itself is never read here.
    """
    emitted = {field.field_path for field in extraction.fields}
    scored = {row.field_path for row in document.rows}
    missing = [path for path in boolean_paths(extraction.document_profile_code)
               if path not in emitted and path in scored]
    if not missing:
        return extraction, []
    added = [ExtractedField(field_path=path, value_raw=None, value_normalized=False, page=None,
                            group_id=None, occurrence_status=OccurrenceStatus.PRESENT,
                            text_evidence=IMPLICIT_FALSE_EVIDENCE) for path in missing]
    return extraction.model_copy(update={"fields": [*extraction.fields, *added]}), missing


def _is_false(value: object) -> bool:
    return value is False or (isinstance(value, str) and value.strip().casefold() == "false")


def false_as_empty_truth(document) -> tuple[object, list[str]]:
    """Copy of the GT document where non-boolean `false` rows become expected absences."""
    converted, rows = [], []
    for row in document.rows:
        if (row.status == "Presente" and row.rule != "boolean_judgment"
                and (_is_false(row.value_raw) or _is_false(row.value_normalized))
                and all(value is None or _is_false(value) for value in (row.value_raw, row.value_normalized))):
            rows.append(replace(row, status="Assente", value_raw=None, value_normalized=None))
            converted.append(row.row_id)
        else:
            rows.append(row)
    return (replace(document, rows=tuple(rows)) if converted else document), converted


def false_as_empty_extraction(extraction: ExtractionResult) -> tuple[ExtractionResult, list[str]]:
    """Copy of the extraction where non-boolean occurrences valued `false` are `not_present`."""
    catalog = CONTRACTS[extraction.document_profile_code]
    fields, changed = [], []
    for field in extraction.fields:
        spec = catalog.get(field.field_path)
        if (spec and spec.rule != "boolean_judgment" and field.occurrence_status == OccurrenceStatus.PRESENT
                and all(value is None or _is_false(value) for value in (field.value_raw, field.value_normalized))
                and (_is_false(field.value_raw) or _is_false(field.value_normalized))):
            fields.append(field.model_copy(update={"occurrence_status": OccurrenceStatus.NOT_PRESENT,
                                                   "value_raw": None, "value_normalized": None}))
            changed.append(field.field_path)
        else:
            fields.append(field)
    return (extraction.model_copy(update={"fields": fields}) if changed else extraction), changed