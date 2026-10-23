"""Canonical DEV-1 field catalog from docs/dev-poc/02_contratto_estrazione.md."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldSpec:
    path: str
    role: str
    cardinality: str
    rule: str | None

    @property
    def minimum(self) -> int:
        return 1 if self.cardinality.startswith("1") else 0

    @property
    def maximum(self) -> int | None:
        return 1 if self.cardinality in {"1", "0..1"} | PER_GROUP_SINGLETON else None

    @property
    def per_group(self) -> bool:
        """At most one present occurrence for each group_id, not for the document."""
        return self.cardinality in PER_GROUP_SINGLETON

    @property
    def grouped(self) -> bool:
        return ("[]" in self.path or self.cardinality in {"0..N", "1..N"}
                or self.path.startswith("tc.buyer.") or self.per_group)


PER_GROUP_SINGLETON = {"0..1 per riga", "0..1 per lotto/riga", "0..1 per voce",
                       "1 per materiale", "1 per voce"}


def _specs(rows: str) -> dict[str, FieldSpec]:
    result = {}
    for line in rows.strip().splitlines():
        path, role, cardinality, rule = line.split(" | ")
        result[path] = FieldSpec(path, role, cardinality, None if rule == "-" else rule)
    return result


CONTRACTS: dict[str, dict[str, FieldSpec]] = {
    "TC": _specs("""
tc.seller.name | CORE | 1 | text_normalized
tc.seller.te_id | CORE | 0..1 | identifier_exact
tc.seller.address_raw | COND | 0..1 | text_normalized
tc.seller.license_number | COND | 0..1 | identifier_exact
tc.buyer.name | CORE | 0..N | text_normalized
tc.buyer.te_id | CORE | 0..N | identifier_exact
tc.buyer.address_raw | CORE | 0..N | text_normalized
tc.buyer.license_number | CORE | 0..N | identifier_exact
tc.buyer.buying_on_behalf_of | CORE | 0..N | text_normalized
tc.raw_materials[].origin_country | CORE | 0..N | grouped_value
tc.raw_materials[].origin_subdivision | COND | 0..N | grouped_value
tc.shipments[].products[].article_number | CORE | 0..N | identifier_exact
tc.shipments[].products[].product_category | CORE | 0..N | grouped_value
tc.shipments[].products[].product_details | CORE | 0..N | grouped_value
tc.shipments[].products[].material_composition | CORE | 0..N | grouped_value
tc.shipments[].products[].net_weight | CORE | 0..N | numeric_equivalent
tc.shipments[].products[].supplementary_weight | COND | 0..N | numeric_equivalent
tc.number | CORE | 1 | identifier_exact
tc.issue_date | CORE | 1 | date_equivalent
tc.is_draft | CORE | 1 | boolean_judgment
tc.usda_nop | COND | 1 | boolean_judgment
"""),
    "FATTURA-VENDITA": _specs("""
invoice.number | CORE | 1 | identifier_exact
invoice.date | CORE | 1 | date_equivalent
invoice.customer.name | CORE | 1 | text_normalized
invoice.customer.address_raw | CORE | 0..1 | text_normalized
invoice.seller.name | CORE | 1 | text_normalized
invoice.seller.address_raw | CORE | 0..1 | text_normalized
invoice.verification | CORE | 1 | boolean_judgment
invoice.incoterm | CORE | 0..1 | text_normalized
invoice.lines[].shipment_reference | CORE | 0..1 per riga | reference_equivalent
invoice.lines[].lot_numbers[] | CORE | 0..N | identifier_exact
invoice.lines[].product_description | CORE | 0..N | grouped_value
invoice.lines[].lot_composition | CORE | 0..N | grouped_value
invoice.lines[].technical_specifications | COND | 0..1 per riga | grouped_value
invoice.lines[].net_weight | CORE | 0..1 per riga | numeric_equivalent
invoice.lines[].gross_weight | COND | 0..1 per riga | numeric_equivalent
invoice.lines[].purchase_order_number | COND | 0..1 per riga | identifier_exact
invoice.lines[].measurement_value | COND | 0..1 per riga | numeric_equivalent
invoice.lines[].measurement_unit | COND | 0..1 per riga | text_normalized
"""),
    "DDT-VENDITA": _specs("""
transport_document.number | CORE | 1 | identifier_exact
transport_document.date | CORE | 1 | date_equivalent
transport_document.customer.name | CORE | 0..1 | text_normalized
transport_document.customer.address_raw | CORE | 0..1 | text_normalized
transport_document.sender.name | CORE | 0..1 | text_normalized
transport_document.sender.address_raw | CORE | 0..1 | text_normalized
transport_document.consignee.name | CORE | 1 | text_normalized
transport_document.consignee.address_raw | CORE | 0..1 | text_normalized
transport_document.verification | CORE | 1 | boolean_judgment
transport_document.incoterm | CORE | 0..1 | text_normalized
transport_document.invoice_reference | CORE | 0..1 | identifier_exact
transport_document.lines[].lot_numbers[] | CORE | 0..N | identifier_exact
transport_document.lines[].product_description | CORE | 0..N | grouped_value
transport_document.lines[].lot_composition | CORE | 0..N | grouped_value
transport_document.lines[].technical_specifications | COND | 0..1 per riga | grouped_value
transport_document.lines[].net_weight | CORE | 0..1 per riga | numeric_equivalent
transport_document.lines[].gross_weight | COND | 0..1 per riga | numeric_equivalent
transport_document.lines[].purchase_order_number | COND | 0..1 per riga | identifier_exact
transport_document.lines[].measurement_value | COND | 0..1 per riga | numeric_equivalent
transport_document.lines[].measurement_unit | COND | 0..1 per riga | grouped_value
"""),
    "FATTURA-ACQUISTO-RMDF": _specs("""
invoice.seller.name | CORE | 1 | text_normalized
invoice.seller.address_raw | CORE | 0..1 | text_normalized
invoice.seller.address.country | COND | 0..1 | text_normalized
invoice.seller.address.region_or_province | COND | 0..1 | text_normalized
invoice.number | CORE | 1 | identifier_exact
invoice.date | CORE | 1 | date_equivalent
invoice.customer.name | CORE | 0..1 | text_normalized
invoice.customer.address_raw | CORE | 0..1 | text_normalized
invoice.consignee.name | CORE | 0..1 | text_normalized
invoice.consignee.address_raw | CORE | 0..1 | text_normalized
invoice.lines[].lot_numbers[] | CORE | 0..N | identifier_exact
invoice.lines[].product_description | CORE | 0..N | grouped_value
invoice.lines[].lot_composition | CORE | 0..N | grouped_value
invoice.lines[].quantity | CORE | 0..1 per lotto/riga | numeric_equivalent
"""),
    "DDT-ACQUISTO-RMDF": _specs("""
transport_document.sender.name | CORE | 1 | text_normalized
transport_document.sender.address_raw | CORE | 0..1 | text_normalized
transport_document.sender.address.country | COND | 0..1 | text_normalized
transport_document.sender.address.region_or_province | COND | 0..1 | text_normalized
transport_document.arrival_date_stamp | COND | 0..1 | date_equivalent
transport_document.number | CORE | 1 | identifier_exact
transport_document.date | CORE | 1 | date_equivalent
transport_document.customer.name | CORE | 1 | text_normalized
transport_document.customer.address_raw | CORE | 0..1 | text_normalized
transport_document.consignee.name | CORE | 0..1 | text_normalized
transport_document.consignee.address_raw | CORE | 0..1 | text_normalized
transport_document.lines[].lot_numbers[] | CORE | 0..N | identifier_exact
transport_document.lines[].product_description | CORE | 0..N | grouped_value
transport_document.lines[].lot_composition | CORE | 0..N | grouped_value
transport_document.lines[].quantity | CORE | 0..1 per lotto/riga | numeric_equivalent
"""),
    "SELEZIONE-TERZISTI": _specs("""
subcontractor_selection.document_date | COND | 0..1 | date_equivalent
subcontractor_selection.document_reference | COND | 0..1 | identifier_exact
subcontractor_selection.entries[].article_code | COND | 0..1 per voce | identifier_exact
subcontractor_selection.entries[].article_description | CORE | 0..1 per voce | grouped_value
subcontractor_selection.entries[].lot_number | COND | 0..1 per voce | identifier_exact
subcontractor_selection.entries[].material_composition | COND | 0..1 per voce | grouped_value
subcontractor_selection.entries[].certifications | COND | 0..1 per voce | grouped_value
subcontractor_selection.entries[].operation | CORE | 1 per voce | grouped_value
subcontractor_selection.entries[].subcontractor | CORE | 0..1 per voce | grouped_value
subcontractor_selection.entries[].process_reference | COND | 0..1 per voce | identifier_exact
subcontractor_selection.entries[].entry_date | COND | 0..1 per voce | date_equivalent
subcontractor_selection.entries[].quantity_input_kg | COND | 0..1 per voce | numeric_equivalent
subcontractor_selection.entries[].quantity_output_kg | COND | 0..1 per voce | numeric_equivalent
subcontractor_selection.entries[].delivery_date | COND | 0..1 per voce | date_equivalent
subcontractor_selection.entries[].operation_note | COND | 0..1 per voce | grouped_value
"""),
    # Historical codes: kept only to read archived DEV runs; not accepted for new runs.
    "DISPOSIZIONE-CONTO-LAVORO": _specs("""
production_order.document_date | CORE | 1 | date_equivalent
production_order.subcontractor | CORE | 1 | text_normalized
production_order.materials[].description | CORE | 1..N | grouped_value
production_order.materials[].batch_number | CORE | 1 per materiale | identifier_exact
production_order.materials[].quantity | CORE | 1 per materiale | numeric_equivalent
production_order.kg_obtained | CORE | 0..1 | numeric_equivalent
production_order.requested_operation | CORE | 0..N | text_normalized
production_order.work_note | COND | 0..1 | text_normalized
production_order.delivery_date | CORE | 0..1 | date_equivalent
"""),
    "ELENCO-TERZISTI": _specs("""
subcontractor_process.production_lot | CORE | 1 | identifier_exact
subcontractor_process.article | CORE | 1 | text_normalized
subcontractor_process.kg_launched | CORE | 1 | numeric_equivalent
subcontractor_process.kg_obtained | CORE | 1 | numeric_equivalent
subcontractor_process.entries[].operation | CORE | 1..N | grouped_value
subcontractor_process.entries[].subcontractor | CORE | 1..N | grouped_value
subcontractor_process.entries[].internal_document_number | CORE | 1 per voce | identifier_exact
subcontractor_process.entries[].document_date | CORE | 1 per voce | date_equivalent
"""),
}

CANONICAL_PROFILES = ("TC", "FATTURA-VENDITA", "DDT-VENDITA", "FATTURA-ACQUISTO-RMDF",
                      "DDT-ACQUISTO-RMDF", "SELEZIONE-TERZISTI")
LEGACY_PROFILES = ("DISPOSIZIONE-CONTO-LAVORO", "ELENCO-TERZISTI")

# Paths removed from the contract. Archived responses keep them; offline re-evaluation
# drops their occurrences in memory and records the removal.
REMOVED_FIELD_PATHS = {"transport_document.handwritten_order_or_lot"}

# Offline migration only: archived Azure responses remain byte-for-byte unchanged.
LEGACY_FIELD_PATHS = {
    "invoice.shipment_reference": "invoice.lines[].shipment_reference",
    "invoice.lines[].net_quantity": "invoice.lines[].net_weight",
    "transport_document.lines[].net_quantity": "transport_document.lines[].net_weight",
}
