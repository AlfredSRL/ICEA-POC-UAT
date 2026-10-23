"""DEV extraction validation and backend outcome types."""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from contracts import CONTRACTS


class OccurrenceStatus(str, Enum):
    PRESENT = "present"
    NOT_PRESENT = "not_present"
    AMBIGUOUS = "ambiguous"
    ILLEGIBLE = "illegible"


BOOLEAN_FIELDS = {"tc.is_draft", "tc.usda_nop", "invoice.verification",
                  "transport_document.verification"}


class ExtractedField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field_path: str
    value_raw: str | None
    value_normalized: str | int | float | bool | None
    page: int | None
    group_id: str | None
    occurrence_status: OccurrenceStatus
    text_evidence: str | None

    @field_validator("page", mode="before")
    @classmethod
    def valid_page(cls, value):
        if isinstance(value, bool) or (value is not None and not isinstance(value, int)):
            raise ValueError("page must be integer or null")
        if value is not None and value < 1:
            raise ValueError("page must start at 1")
        return value

    @model_validator(mode="after")
    def coherent_state(self, info: ValidationInfo):
        if self.field_path in BOOLEAN_FIELDS and not (info.context or {}).get("legacy_boolean"):
            if self.occurrence_status == OccurrenceStatus.NOT_PRESENT:
                raise ValueError("boolean judgment must be false, not not_present")
            if self.occurrence_status == OccurrenceStatus.PRESENT:
                if not isinstance(self.value_normalized, bool):
                    raise ValueError("boolean judgment requires true or false")
                if self.page is not None and self.page < 1:
                    raise ValueError("invalid boolean page")
                return self
            if self.value_raw is not None or self.value_normalized is not None:
                raise ValueError("unresolved boolean judgment cannot contain a value")
            return self
        if self.occurrence_status == OccurrenceStatus.PRESENT:
            if not self.value_raw or not self.value_raw.strip():
                raise ValueError("present occurrence requires value_raw")
            if not self.text_evidence or not self.text_evidence.strip():
                raise ValueError("present occurrence requires text_evidence")
            if self.page is None and not self.field_path.endswith(".verification"):
                raise ValueError("present occurrence requires page")
        elif self.value_raw is not None or self.value_normalized is not None:
            raise ValueError("non-present occurrence cannot contain a value")
        return self


class ModelPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    processing_status: Literal["complete", "partial", "unreadable", "failed"]
    warnings: list[str] = Field(default_factory=list)
    fields: list[ExtractedField] = Field(default_factory=list)


class ExtractionResult(ModelPayload):
    source_file: str
    source_sha256: str
    document_profile_code: str
    strategy: Literal["vision_direct", "layout_assisted"]

    @model_validator(mode="after")
    def contract_valid(self, info: ValidationInfo):
        catalog = CONTRACTS.get(self.document_profile_code)
        if catalog is None:
            raise ValueError("unknown document profile")
        if len(self.source_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.source_sha256):
            raise ValueError("invalid source_sha256")
        present_by_path: dict[str, list[ExtractedField]] = {}
        for field in self.fields:
            spec = catalog.get(field.field_path)
            if spec is None:
                raise ValueError(f"unknown field_path: {field.field_path}")
            if field.occurrence_status == OccurrenceStatus.PRESENT:
                if spec.grouped and not field.group_id and not (info.context or {}).get("legacy_group"):
                    raise ValueError(f"group_id required: {field.field_path}")
                present_by_path.setdefault(field.field_path, []).append(field)
        if self.processing_status == "complete":
            missing = [spec.path for spec in catalog.values() if spec.minimum and not present_by_path.get(spec.path)
                       and not ((info.context or {}).get("legacy_boolean") and spec.path in BOOLEAN_FIELDS)]
            if missing:
                raise ValueError(f"missing required fields: {', '.join(missing)}")
        for path, entries in present_by_path.items():
            spec = catalog[path]
            if spec.maximum == 1:
                if spec.per_group:
                    if (path == "invoice.lines[].shipment_reference"
                            and (info.context or {}).get("legacy_shipment_reference")):
                        continue
                    groups = [entry.group_id for entry in entries]
                    if len(groups) != len(set(groups)):
                        raise ValueError(f"too many occurrences per group: {path}")
                elif len(entries) > 1:
                    raise ValueError(f"too many occurrences: {path}")
        if self.processing_status == "complete":
            for anchor, dependents in (
                ("production_order.materials[].description", (
                    "production_order.materials[].batch_number", "production_order.materials[].quantity")),
                ("subcontractor_process.entries[].operation", (
                    "subcontractor_process.entries[].subcontractor",
                    "subcontractor_process.entries[].internal_document_number",
                    "subcontractor_process.entries[].document_date")),
            ):
                if anchor not in catalog:
                    continue
                groups = {field.group_id for field in present_by_path.get(anchor, [])}
                for dependent in dependents:
                    other = {field.group_id for field in present_by_path.get(dependent, [])}
                    if groups != other:
                        raise ValueError(f"incomplete groups: {dependent}")
            # SELEZIONE-TERZISTI: every entry needs its operation; other fields are optional.
            anchor = "subcontractor_selection.entries[].operation"
            if anchor in catalog:
                operations = {field.group_id for field in present_by_path.get(anchor, [])}
                entries = {field.group_id for path, fields in present_by_path.items()
                           if path.startswith("subcontractor_selection.entries[].") for field in fields}
                if entries - operations:
                    raise ValueError(f"incomplete groups: {anchor}")
        return self


def validate_payload(payload: object, *, source_file: str, source_sha256: str,
                     profile: str, strategy: str, page_count: int) -> ExtractionResult:
    model = ModelPayload.model_validate(payload)
    result = ExtractionResult.model_validate({
        **model.model_dump(), "source_file": source_file, "source_sha256": source_sha256,
        "document_profile_code": profile, "strategy": strategy,
    })
    for field in result.fields:
        if field.page is not None and field.page > page_count:
            raise ValueError(f"page outside document: {field.field_path}")
    return result


def validate_payload_with_field_issues(payload: object, *, source_file: str,
                                       source_sha256: str, profile: str,
                                       strategy: str, page_count: int) -> tuple[ExtractionResult, list[dict]]:
    """Keep valid fields when only per-row singleton groups collide.

    Every colliding occurrence is excluded; no candidate is selected or reassigned.
    The original model response must be retained by the caller.
    """
    try:
        return validate_payload(payload, source_file=source_file, source_sha256=source_sha256,
                                profile=profile, strategy=strategy, page_count=page_count), []
    except ValueError as original_error:
        model = ModelPayload.model_validate(payload)
        if model.processing_status not in {"complete", "partial"}:
            raise original_error
        catalog = CONTRACTS[profile]
        buckets: dict[tuple[str, str], list[int]] = {}
        for index, field in enumerate(model.fields):
            if field.page is not None and field.page > page_count:
                raise original_error
            spec = catalog.get(field.field_path)
            if (spec is not None and spec.per_group and not spec.minimum
                    and field.occurrence_status == OccurrenceStatus.PRESENT and field.group_id):
                buckets.setdefault((field.field_path, field.group_id), []).append(index)
        collisions = [(key, indices) for key, indices in buckets.items() if len(indices) > 1]
        if not collisions:
            raise original_error
        excluded = {index for _, indices in collisions for index in indices}
        candidate = model.model_dump(mode="json")
        candidate["fields"] = [field for index, field in enumerate(candidate["fields"])
                               if index not in excluded]
        # Validate the remainder under the original status first. A second, unrelated
        # missing required field or contract error must not be masked by `partial`.
        validate_payload(candidate, source_file=source_file, source_sha256=source_sha256,
                         profile=profile, strategy=strategy, page_count=page_count)
        issues = []
        for (path, group), indices in collisions:
            fields = [model.fields[index] for index in indices]
            display = "; ".join("'" + field.value_raw.replace("'", "''") + "'"
                                for field in fields)
            pages = {field.page for field in fields}
            issues.append({"label": "collisione_cardinalita", "field_path": path,
                           "group_id": group, "page": next(iter(pages)) if len(pages) == 1 else None,
                           "value_display": display, "source_field_indices": indices})
        candidate["processing_status"] = "partial"
        candidate["warnings"] = [*candidate["warnings"],
                                 f"collisione_cardinalita: {len(issues)} campi isolati"]
        result = validate_payload(candidate, source_file=source_file, source_sha256=source_sha256,
                                  profile=profile, strategy=strategy, page_count=page_count)
        return result, issues


class ErrorCategory(str, Enum):
    AUTH = "auth"
    NOT_FOUND = "not_found"
    QUOTA = "quota"
    UNSUPPORTED_CONTENT = "unsupported_content"
    NETWORK = "network"
    SCHEMA = "schema"
    UNKNOWN = "unknown"


class CallOutcome(BaseModel):
    backend: str
    deployment: str | None = None
    model_version: str | None = None
    document_id: str
    document_sha256: str
    started_at: str
    duration_seconds: float
    http_status: int | None = None
    succeeded: bool
    error_category: ErrorCategory | None = None
    error_message: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    result: dict | None = None
