"""DEV preflight and provisional, row-level scoring."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path

from contracts import CONTRACTS
from ground_truth import DevDocument, TruthRow
from normalization import ORIGIN_PATHS, canonical, canonical_truth, is_list_value, number, origin_variants, partial_match, reference_variants
from schemas import ExtractionResult, ExtractedField, OccurrenceStatus

RULES = {"identifier_exact", "text_normalized", "date_equivalent", "numeric_equivalent",
         "grouped_value", "presence", "boolean_judgment", "reference_equivalent"}
STATUSES = {"Presente": "present", "Assente": "not_present"}
# An approved GT decision that the reading is ambiguous: kept explicit, never scored
# as if the value were a confirmed field of the profile.
EXPLICIT_AMBIGUITY = "Ambiguo"
GROUP_FAMILIES = (
    "tc.buyer", "tc.raw_materials[]", "tc.shipments[].products[]",
    "invoice.lines[]", "transport_document.lines[]",
    "production_order.materials[]", "subcontractor_process.entries[]",
    "subcontractor_selection.entries[]",
)
ADDRESS_PART_SUFFIXES = (".address.country", ".address.region_or_province")
# Sale invoices and DDTs: one printed quantity may legitimately land in any of these
# line fields. A correctly read value is not an error because of the field chosen;
# ICEA's platform decides how to treat it.
QUANTITY_FIELDS = ("net_weight", "gross_weight", "measurement_value")
QUANTITY_PREFIXES = ("invoice.lines[].", "transport_document.lines[].")


def quantity_siblings(path: str) -> tuple[str, ...]:
    for prefix in QUANTITY_PREFIXES:
        if path.startswith(prefix) and path[len(prefix):] in QUANTITY_FIELDS:
            return tuple(prefix + name for name in QUANTITY_FIELDS if prefix + name != path)
    return ()


def _is_unit_path(path: str) -> bool:
    return any(path == prefix + "measurement_unit" for prefix in QUANTITY_PREFIXES)
SUBDIVISION_CODE = re.compile(r"\b[A-Z]{2}-\d{1,3}\b", re.IGNORECASE)


def _address_without_optional_code(raw: object) -> str:
    return canonical("address", "text_normalized",
                     SUBDIVISION_CODE.sub(lambda match: match.group(0).split("-")[0], str(raw)))


def preflight(document: DevDocument, path: Path, profile: str, sha256: str, pages: int) -> dict:
    blocking = []
    if document.profile != profile:
        blocking.append("profile_mismatch")
    if document.filename != path.name:
        blocking.append("filename_mismatch")
    if document.sha256 != sha256:
        blocking.append("sha256_mismatch")
    if document.page_count != pages:
        blocking.append("page_count_mismatch")
    issues = defaultdict(list)
    ambiguities = []
    catalog = CONTRACTS[document.profile]
    for row in document.rows:
        spec = catalog.get(row.field_path)
        if spec is None:
            issues["unknown_field_path"].append(row.row_id)
        elif row.role != spec.role:
            issues["class_mismatch"].append(row.row_id)
        if row.status == EXPLICIT_AMBIGUITY:
            ambiguities.append(row.row_id)
        elif row.status not in STATUSES:
            issues["missing_or_unknown_status"].append(row.row_id)
        if row.rule not in RULES:
            issues["missing_or_unknown_rule"].append(row.row_id)
        elif spec and spec.rule is None:
            issues["rule_not_in_contract"].append(row.row_id)
        elif spec and spec.rule is not None and row.rule != spec.rule:
            issues["rule_mismatch"].append(row.row_id)
        if row.validation != "Approvato":
            issues["not_approved"].append(row.row_id)
        if row.status == "Presente" and row.value_raw is None and row.value_normalized is None:
            issues["missing_expected_value"].append(row.row_id)
        if row.status == "Assente" and (row.value_raw is not None or row.value_normalized is not None):
            issues["absent_with_value"].append(row.row_id)
        if spec and spec.grouped and row.status in {"Presente", EXPLICIT_AMBIGUITY} and not row.group_id:
            issues["missing_group"].append(row.row_id)
        if row.page:
            numbers = [int(item) for item in re.findall(r"\d+", row.page)]
            if not numbers or any(number < 1 or number > pages for number in numbers):
                issues["invalid_page"].append(row.row_id)
    return {
        "sample_id": document.sample_id, "profile": document.profile,
        "filename": path.name, "sha256": sha256, "page_count": pages,
        "ground_truth_rows": len(document.rows), "blocking": blocking,
        "issues": {key: {"count": len(ids), "row_ids": ids} for key, ids in issues.items()},
        "explicit_ambiguities": {"count": len(ambiguities), "row_ids": ambiguities},
    }


def equivalent(row: TruthRow, field: ExtractedField, *, compare_group: bool = True) -> bool:
    if row.rule == "presence":
        return field.occurrence_status == OccurrenceStatus.PRESENT
    if row.rule == "reference_equivalent":
        return bool(reference_variants(row.value_raw, row.value_normalized)
                    & reference_variants(field.value_raw, field.value_normalized,
                                         field_path=field.field_path,
                                         text_evidence=field.text_evidence))
    if row.field_path in ORIGIN_PATHS and ";" not in (row.value_raw or ""):
        return bool(origin_variants(row.field_path, row.value_raw)
                    & origin_variants(field.field_path, field.value_raw)) and (
                        not compare_group or row.group_id == field.group_id)
    expected = canonical_truth(row.field_path, row.rule, row.value_raw, row.value_normalized)
    actual = canonical(field.field_path, row.rule, field.value_raw, field.value_normalized,
                       text_evidence=field.text_evidence)
    if expected is None or actual is None:
        return False
    if row.field_path.endswith(ADDRESS_PART_SUFFIXES) and row.rule == "text_normalized":
        # The printed GT value and its approved normalized form are both explicit
        # variants (e.g. a printed country code); no external code table is used.
        return actual in {expected, canonical(row.field_path, row.rule, row.value_raw)}
    if (row.field_path.endswith(".address_raw") and row.rule == "text_normalized"
            and expected != actual):
        gt_codes = SUBDIVISION_CODE.findall(str(row.value_raw or ""))
        model_codes = SUBDIVISION_CODE.findall(str(field.value_raw or ""))
        if bool(gt_codes) != bool(model_codes):
            if _address_without_optional_code(row.value_raw or row.value_normalized) == _address_without_optional_code(
                    field.value_raw or field.value_normalized):
                return True
    if row.rule == "numeric_equivalent":
        gt_number, model_number = number(row.value_raw or ""), number(field.value_raw or "")
        if (gt_number and model_number and gt_number[1] and model_number[1]
                and gt_number[1] != model_number[1]):
            return False
    return expected == actual and (row.rule != "grouped_value" or not compare_group
                                   or row.group_id == field.group_id)


def partly_equivalent(row: TruthRow, field: ExtractedField) -> bool:
    expected = canonical_truth(row.field_path, row.rule, row.value_raw, row.value_normalized)
    actual = canonical(field.field_path, row.rule, field.value_raw, field.value_normalized,
                       text_evidence=field.text_evidence)
    return bool(expected is not None and actual is not None
                and partial_match(expected, actual, row.rule))


def collision_candidates(issue: dict) -> list[str]:
    """Values of a cardinality collision, from its quoted display ('a'; 'b')."""
    return [value.replace("''", "'") for value in re.findall(r"'((?:[^']|'')*)'", issue["value_display"])]


def quoted_values(value: object) -> list[str]:
    """Values of a GT cell written as a quoted list ('a'; 'b').

    Excel hides a leading apostrophe, so a cell typed as 'a'; 'b' reads as a'; 'b'.
    """
    if value is None or "'; '" not in str(value):
        return []
    text = str(value).strip()
    text = ("" if text.startswith("'") else "'") + text + ("" if text.endswith("'") else "'")
    return [item.replace("''", "'") for item in re.findall(r"'((?:[^']|'')*)'", text)]


def _single(row: TruthRow, value: str) -> TruthRow:
    return replace(row, value_raw=value, value_normalized=None)


def _candidate(path: str, group: str | None, value: str, page: int | None = None) -> ExtractedField:
    return ExtractedField(field_path=path, value_raw=value, value_normalized=None, page=page,
                          group_id=group, text_evidence=value,
                          occurrence_status=OccurrenceStatus.PRESENT)


def _value_overlap(row: TruthRow, expected: list[str], fields: list[ExtractedField]) -> tuple[int, bool]:
    """Pairs of equal values (each used once) and whether any value partly matches."""
    remaining = list(fields)
    hits = 0
    for value in expected:
        for field in remaining:
            if equivalent(_single(row, value), field, compare_group=False):
                remaining.remove(field)
                hits += 1
                break
    partial = any(partly_equivalent(_single(row, value), field)
                  for value in expected for field in fields)
    return hits, partial


def _multi_value_outcome(row: TruthRow, fields: list[ExtractedField]) -> str:
    """Several GT and/or model values for a one-per-row field.

    All values equal (same count): matched; at least one equal or partly equal: partial;
    none: incorrect.
    """
    if row.status != "Presente":
        return "incorrect"
    expected = quoted_values(row.value_raw) or [row.value_raw]
    hits, partial = _value_overlap(row, expected, fields)
    if hits == len(expected) == len(fields) and len(expected) > 1:
        return "matched"
    return "partial" if hits or partial else "incorrect"


def _collision_outcome(row: TruthRow, issue: dict) -> str:
    """Several model values for a one-per-row field (cardinality collision)."""
    fields = [_candidate(issue["field_path"], issue["group_id"], value, issue.get("page"))
              for value in collision_candidates(issue)]
    if not quoted_values(row.value_raw) and row.status == "Presente":
        # Single GT value: one candidate equal or partly equal is partial, never matched.
        hits, partial = _value_overlap(row, [row.value_raw], fields)
        return "partial" if hits or partial else "incorrect"
    return _multi_value_outcome(row, fields)


def _group_family(path: str) -> str | None:
    for family in GROUP_FAMILIES:
        if path.startswith(family + "."):
            return family
    return None


def _align_groups(document: DevDocument, extraction: ExtractionResult) -> dict[tuple[str, str], str]:
    """Align local group labels only through unique matching field values."""
    mapping = {}
    for family in GROUP_FAMILIES:
        rows = [row for row in document.rows if _group_family(row.field_path) == family
                and row.group_id and row.status == "Presente" and row.rule in RULES
                and (row.rule == "presence" or row.value_raw or row.value_normalized)]
        fields = [field for field in extraction.fields if _group_family(field.field_path) == family
                  and field.group_id and field.occurrence_status == OccurrenceStatus.PRESENT]
        expected_groups = {row.group_id for row in rows}
        actual_groups = {field.group_id for field in fields}
        if len(expected_groups) == len(actual_groups) == 1:
            mapping[(family, next(iter(actual_groups)))] = next(iter(expected_groups))
            continue
        weights = Counter()
        for row in rows:
            if row.rule == "presence":
                continue
            spec = CONTRACTS[document.profile].get(row.field_path)
            if spec is None or spec.rule is None or spec.rule != row.rule:
                continue
            for field in fields:
                if row.field_path == field.field_path and equivalent(row, field, compare_group=False):
                    weights[(row.group_id, field.group_id)] += 3 if row.rule == "identifier_exact" else 1
        remaining_expected = set(expected_groups)
        remaining_actual = set(actual_groups)
        while remaining_expected and remaining_actual:
            candidates = []
            for expected in remaining_expected:
                options = [(weights[(expected, actual)], actual) for actual in remaining_actual]
                best = max((weight for weight, _ in options), default=0)
                if best <= 0:
                    continue
                matches = [actual for weight, actual in options if weight == best]
                if len(matches) != 1:
                    continue
                actual = matches[0]
                reverse = [(weights[(other, actual)], other) for other in remaining_expected]
                reverse_best = max(weight for weight, _ in reverse)
                if len([other for weight, other in reverse if weight == reverse_best]) == 1:
                    candidates.append((best, expected, actual))
            if not candidates:
                break
            for _, expected, actual in sorted(candidates, reverse=True):
                if expected in remaining_expected and actual in remaining_actual:
                    mapping[(family, actual)] = expected
                    remaining_expected.remove(expected)
                    remaining_actual.remove(actual)
        # Model groups with identical content are interchangeable: pairing the tied ones in
        # document order cannot change any outcome, so it is not a guessed association.
        content = defaultdict(set)
        for field in fields:
            content[field.group_id].add((field.field_path,
                                         canonical(field.field_path, "text_normalized", field.value_raw)))
        for expected in sorted(remaining_expected, key=_natural_key):
            options = [(weights[(expected, actual)], actual) for actual in remaining_actual]
            best = max((weight for weight, _ in options), default=0)
            tied = sorted((actual for weight, actual in options if weight == best), key=_natural_key)
            if best <= 0 or len({frozenset(content[actual]) for actual in tied}) != 1:
                continue
            mapping[(family, tied[0])] = expected
            remaining_actual.remove(tied[0])
    return mapping


def _natural_key(value: str) -> list:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value or "")]


def score(document: DevDocument, extraction: ExtractionResult,
          field_issues: list[dict] | None = None) -> dict:
    if extraction.document_profile_code != document.profile or extraction.source_sha256 != document.sha256:
        raise ValueError("extraction does not match DEV registry")
    evaluable = []
    not_ready = []
    for truth_index, row in enumerate(document.rows):
        reasons = []
        spec = CONTRACTS[document.profile].get(row.field_path)
        if spec is None:
            reasons.append("unknown_field_path")
        else:
            if row.role != spec.role:
                reasons.append("class_mismatch")
            if spec.rule is None:
                reasons.append("rule_not_in_contract")
            elif row.rule != spec.rule:
                reasons.append("rule_mismatch")
        if row.status == EXPLICIT_AMBIGUITY:
            reasons.append("ambiguous_in_ground_truth")
        elif row.status not in STATUSES:
            reasons.append("missing_or_unknown_status")
        if row.rule not in RULES:
            reasons.append("missing_or_unknown_rule")
        if row.status == "Presente" and row.rule != "presence" and row.value_normalized is None and row.value_raw is None:
            reasons.append("missing_expected_value")
        if row.status == "Assente" and (row.value_raw is not None or row.value_normalized is not None):
            reasons.append("absent_with_value")
        if spec and spec.grouped and row.status == "Presente" and not row.group_id:
            reasons.append("missing_group")
        if reasons:
            not_ready.append({"row_id": row.row_id, "truth_index": truth_index,
                              "reasons": reasons})
        else:
            evaluable.append((truth_index, row))
    fields = extraction.fields
    group_mapping = _align_groups(document, extraction)
    invalid_truth = {}
    for issue_index, issue in enumerate(field_issues or []):
        family = _group_family(issue["field_path"])
        mapped_group = group_mapping.get((family, issue["group_id"]))
        if mapped_group is None:
            continue
        for truth_index, row in evaluable:
            if row.field_path == issue["field_path"] and row.group_id == mapped_group:
                invalid_truth[truth_index] = issue_index
    truth_group_counts = Counter((row.field_path, row.group_id) for _, row in evaluable
                                 if row.status == "Presente" and row.group_id)

    def compatible_fields(row):
        family = _group_family(row.field_path)
        needs_group = bool(family and row.group_id)
        return [(index, field) for index, field in enumerate(fields)
                if field.field_path == row.field_path
                and (not needs_group or group_mapping.get((family, field.group_id)) == row.group_id)]

    # Reserve exact value matches across the whole vector before a wrong/partial
    # occurrence can consume a value belonging to a later GT row.
    exact_options = {
        truth_index: [index for index, field in compatible_fields(row)
                      if field.occurrence_status == OccurrenceStatus.PRESENT
                      and equivalent(row, field, compare_group=False)]
        for truth_index, row in evaluable if row.status == "Presente"
    }
    field_owner = {}

    def assign_exact(truth_index, seen):
        for index in exact_options[truth_index]:
            if index in seen:
                continue
            seen.add(index)
            owner = field_owner.get(index)
            if owner is None or assign_exact(owner, seen):
                field_owner[index] = truth_index
                return True
        return False

    for truth_index in sorted(exact_options, key=lambda item: (len(exact_options[item]), item)):
        assign_exact(truth_index, set())

    def mapped_group(field):
        return group_mapping.get((_group_family(field.field_path), field.group_id))

    # GT quantities per line group, and model quantities that reproduce one of them.
    gt_quantities = defaultdict(list)
    for _, row in evaluable:
        if row.status == "Presente" and quantity_siblings(row.field_path) and row.group_id:
            gt_quantities[row.group_id].append(row)

    def supported_quantity(field):
        return (field.occurrence_status == OccurrenceStatus.PRESENT
                and bool(quantity_siblings(field.field_path))
                and any(equivalent(row, field, compare_group=False)
                        for row in gt_quantities.get(mapped_group(field), [])))

    weight_units = {"kg", "kgs", "kilogram", "kilograms", "g", "gr", "gram", "grams", "grammi",
                    "lb", "lbs", "t", "ton", "tonne"}

    def supported_unit(field):
        # A weight unit emitted beside a quantity that reproduces a GT weight of the line.
        return (field.occurrence_status == OccurrenceStatus.PRESENT and _is_unit_path(field.field_path)
                and canonical(field.field_path, "text_normalized", field.value_raw) in weight_units
                and any(other.group_id == field.group_id and supported_quantity(other)
                        for other in fields))

    # A GT quantity without a same-field match may be satisfied by a sibling field.
    cross_owner = {}
    for truth_index, row in evaluable:
        if (row.status != "Presente" or not quantity_siblings(row.field_path)
                or truth_index in field_owner.values()):
            continue
        for index, field in enumerate(fields):
            if (index not in field_owner and field.field_path in quantity_siblings(row.field_path)
                    and field.occurrence_status == OccurrenceStatus.PRESENT
                    and mapped_group(field) == row.group_id
                    and equivalent(row, field, compare_group=False)):
                field_owner[index] = truth_index
                cross_owner[truth_index] = index
                break

    def unit_in_alternative(row):
        unit = canonical(row.field_path, "text_normalized", row.value_raw or row.value_normalized)
        for other, index in cross_owner.items():
            if document.rows[other].group_id != row.group_id:
                continue
            printed = f"{fields[index].value_raw} {fields[index].text_evidence or ''}"
            if unit and unit in canonical("unit", "text_normalized", printed).split():
                return True
        return False

    accepted_alternative = set()
    used = set()
    results = []
    by_class = defaultdict(Counter)
    by_rule = defaultdict(Counter)
    for truth_index, row in evaluable:
        fragment_indices = []
        family = _group_family(row.field_path)
        needs_group = bool(family and row.group_id)
        candidates = [(index, field) for index, field in compatible_fields(row)
                      if index not in used and field_owner.get(index, truth_index) == truth_index]
        if truth_index in invalid_truth:
            index = None
            outcome = _collision_outcome(row, (field_issues or [])[invalid_truth[truth_index]])
        elif row.status == "Assente":
            present = [(index, field) for index, field in candidates if field.occurrence_status == OccurrenceStatus.PRESENT]
            absence = [(index, field) for index, field in candidates if field.occurrence_status == OccurrenceStatus.NOT_PRESENT]
            alternative = [index for index, field in present
                           if supported_quantity(field) or supported_unit(field)]
            if present and len(alternative) == len(present):
                # Every value here repeats a GT quantity of its line in another field.
                accepted_alternative.update(alternative)
                index, outcome = None, "matched"
            elif present:
                index, _ = present[0]
                outcome = "incorrect"
            elif absence:
                index, _ = absence[0]
                outcome = "matched"
            else:
                index, outcome = None, "matched"
        else:
            matches = [(index, field) for index, field in candidates
                       if field.occurrence_status == OccurrenceStatus.PRESENT
                       and equivalent(row, field, compare_group=False)]
            present = [(index, field) for index, field in candidates if field.occurrence_status == OccurrenceStatus.PRESENT]
            partials = [(index, field) for index, field in present if partly_equivalent(row, field)]
            if quoted_values(row.value_raw):
                # A GT list of several values: one model value covers only part of it.
                matches = []
                partials = [(index, field) for index, field in present
                            if _multi_value_outcome(row, [field]) == "partial"]
            fragments = ([index for index, _ in present]
                         if row.rule == "grouped_value" and row.field_path not in ORIGIN_PATHS
                         and needs_group and truth_group_counts[(row.field_path, row.group_id)] == 1
                         and len(present) > 1
                         and len({field.group_id for _, field in present}) == 1
                         and len({field.page for _, field in present}) == 1
                         and canonical_truth(row.field_path, row.rule, row.value_raw, row.value_normalized)
                         == canonical(row.field_path, row.rule,
                                      " ".join(field.value_raw for _, field in present))
                         else [])
            if matches:
                index, _ = matches[0]
                outcome = "matched"
            elif truth_index in cross_owner:
                index, outcome = cross_owner[truth_index], "matched"
            elif fragments:
                index, outcome = fragments[0], "matched"
                fragment_indices = fragments
            elif partials:
                index, _ = partials[0]
                outcome = "partial"
            elif is_list_value(row.field_path) and ";" in (row.value_raw or "") and sum(
                field.field_path == row.field_path and field.occurrence_status == OccurrenceStatus.PRESENT
                for field in fields) > 1:
                index, outcome = None, "unresolved_group"
            elif present:
                index, _ = present[0]
                outcome = ("unassessed" if row.rule == "boolean_judgment"
                           and canonical(row.field_path, row.rule,
                                         fields[index].value_raw,
                                         fields[index].value_normalized) is None
                           else "incorrect")
            elif needs_group and any(
                field.field_path == row.field_path
                and field.occurrence_status == OccurrenceStatus.PRESENT
                and (family, field.group_id) not in group_mapping
                and equivalent(row, field, compare_group=False)
                for field in fields
            ):
                index, outcome = None, "unresolved_group"
            elif _is_unit_path(row.field_path) and unit_in_alternative(row):
                # The unit is printed with a quantity the model reported in a sibling field.
                index, outcome = None, "matched"
            else:
                index, outcome = None, "omitted"
        if index is not None:
            used.update(fragment_indices or [index])
        result = {"row_id": row.row_id, "truth_index": truth_index,
                  "field_path": row.field_path,
                  "class": row.role, "rule": row.rule, "outcome": outcome,
                  "extraction_index": index}
        if truth_index in invalid_truth:
            result["field_issue_index"] = invalid_truth[truth_index]
        if fragment_indices:
            result["extraction_indices"] = fragment_indices
        if index is not None and fields[index].field_path != row.field_path:
            result["alternative_field_path"] = fields[index].field_path
        results.append(result)
        by_class[row.role or "unknown"][outcome] += 1
        by_rule[row.rule or "unknown"][outcome] += 1
    for index, field in enumerate(fields):
        if index not in used and (supported_quantity(field) or supported_unit(field)):
            accepted_alternative.add(index)
            used.add(index)
    unmatched = [index for index, field in enumerate(fields)
                 if index not in used and field.occurrence_status == OccurrenceStatus.PRESENT]
    not_ready_indices = {item["truth_index"] for item in not_ready}
    not_ready_paths = {row.field_path for index, row in enumerate(document.rows)
                       if index in not_ready_indices}
    unresolved_paths = {item["field_path"] for item in results if item["outcome"] == "unresolved_group"}
    truth_paths = {row.field_path for row in document.rows}
    unscored = [index for index in unmatched if fields[index].field_path in truth_paths
                or fields[index].field_path in not_ready_paths
                or (fields[index].field_path in unresolved_paths
                    and (_group_family(fields[index].field_path), fields[index].group_id) not in group_mapping)]
    counts = Counter(item["outcome"] for item in results)
    return {
        "sample_id": document.sample_id, "profile": document.profile,
        "provisional": True, "ground_truth_rows": len(document.rows),
        "evaluable_rows": len(evaluable), "not_ready_rows": len(not_ready),
        "ambiguous_truth_rows": sum(row.status == EXPLICIT_AMBIGUITY for row in document.rows),
        "approved_evaluable_rows": sum(row.validation == "Approvato" for _, row in evaluable),
        "extracted_present": sum(field.occurrence_status == OccurrenceStatus.PRESENT for field in fields),
        "matched": counts["matched"], "partial": counts["partial"],
        "incorrect": counts["incorrect"], "unassessed": counts["unassessed"],
        "omitted": counts["omitted"], "unresolved_group": counts["unresolved_group"],
        "invalid_field": counts["invalid_field"], "field_issue_count": len(field_issues or []),
        "unmatched_extractions": len(unmatched),
        "unscored_extractions": len(unscored),
        "unpaired_occurrences": sum(fields[index].field_path in truth_paths for index in unmatched),
        "potentially_invented": len(unmatched) - len(unscored),
        "results_by_class": {key: dict(value) for key, value in by_class.items()},
        "results_by_rule": {key: dict(value) for key, value in by_rule.items()},
        "accepted_alternative_indices": sorted(accepted_alternative),
        "rows": results, "not_ready": not_ready,
    }
