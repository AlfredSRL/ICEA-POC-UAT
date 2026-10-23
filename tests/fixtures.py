"""Synthetic fixtures only. No UAT or DEV values appear in this repository."""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import httpx

from icea_client.config import METHODS, Paths
from contracts import CANONICAL_PROFILES

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
HEADERS = {"A": "ID", "C": "field_path", "D": "Classe", "F": "Pagina", "G": "Gruppo",
           "H": "Valore raw", "I": "Valore normalizzato", "J": "Stato", "K": "Regola",
           "M": "Validazione"}

# (row_id, field_path, role, page, group, raw, normalized, status, rule)
INVOICE_ROWS = [
    ("R1", "invoice.number", "CORE", "1", None, "SYN-001", "SYN-001", "Presente", "identifier_exact"),
    ("R2", "invoice.date", "CORE", "1", None, "22/01/2024", "2024-01-22", "Presente", "date_equivalent"),
    ("R3", "invoice.verification", "CORE", None, None, "true", "true", "Presente", "boolean_judgment"),
    ("R4", "invoice.lines[].net_weight", "CORE", "1", "1", "100,5", 100.5, "Presente", "numeric_equivalent"),
    ("R5", "invoice.lines[].net_weight", "CORE", "1", "2", "200", 200, "Presente", "numeric_equivalent"),
    ("R6", "invoice.lines[].lot_numbers[]", "CORE", "1", "1", "LOT-A", "LOT-A", "Presente", "identifier_exact"),
    ("R7", "invoice.lines[].lot_numbers[]", "CORE", "1", "2", "LOT-B", "LOT-B", "Presente", "identifier_exact"),
    ("R8", "invoice.incoterm", "CORE", None, None, None, None, "Assente", "text_normalized"),
    ("R9", "invoice.lines[].purchase_order_number", "COND", "1", "1", "PO-77", "PO-77", "Presente", "identifier_exact"),
]


def _cell(reference: str, value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return f'<c r="{reference}" t="b"><v>{int(value)}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{reference}"><v>{value}</v></c>'
    return f'<c r="{reference}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'


def _sheet(meta: dict, rows: list[tuple]) -> str:
    if meta["profile"] == "SELEZIONE-TERZISTI":
        # Single-row header as in the UAT v1.8 selection sheet: file and SHA-256 in row 4.
        grid = {4: {"C": "Profilo", "D": meta["profile"], "I": "Pagine", "J": meta["pages"],
                    "K": "File", "L": meta["filename"], "M": "SHA-256", "N": meta["sha256"].upper()},
                7: {"A": "Profilo unico SELEZIONE-TERZISTI, istruzioni sintetiche."}, 9: HEADERS}
    else:
        grid = {4: {"C": "Profilo", "D": meta["profile"], "I": "Pagine", "J": meta["pages"]},
                5: {"A": "Percorso relativo e file", "B": meta["filename"]},
                6: {"A": "SHA-256", "B": meta["sha256"]}, 9: HEADERS}
    for offset, (row_id, path, role, page, group, raw, normalized, status, rule) in enumerate(rows):
        grid[10 + offset] = {"A": row_id, "C": path, "D": role, "F": page, "G": group, "H": raw,
                             "I": normalized, "J": status, "K": rule, "M": "Approvato"}
    xml = []
    for number in sorted(grid):
        cells = "".join(_cell(f"{column}{number}", value) for column, value in sorted(grid[number].items()))
        xml.append(f'<row r="{number}">{cells}</row>')
    return f'<worksheet xmlns="{NS}"><sheetData>{"".join(xml)}</sheetData></worksheet>'


def write_workbook(path: Path, sheets: dict[str, tuple[dict, list[tuple]]]) -> Path:
    names = ["Istruzioni", *sheets]
    sheet_xml = "".join(f'<sheet name="{name}" sheetId="{index}" r:id="rId{index}"/>'
                        for index, name in enumerate(names, 1))
    rels = "".join(f'<Relationship Id="rId{index}" Target="worksheets/sheet{index}.xml"/>'
                   for index in range(1, len(names) + 1))
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", f'<workbook xmlns="{NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>{sheet_xml}</sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>')
        archive.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{NS}"><sheetData/></worksheet>')
        for index, (meta, rows) in enumerate(sheets.values(), 2):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", _sheet(meta, rows))
    return path


def write_pdf(path: Path, text: str, pages: int = 1) -> str:
    import pymupdf as fitz

    with fitz.open() as pdf:
        for number in range(pages):
            page = pdf.new_page()
            page.insert_text((72, 72), f"{text} {number + 1}")
        pdf.save(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def environment(root: Path, samples: dict[str, list[tuple]] | None = None,
                profile: str | dict[str, str] = "FATTURA-VENDITA") -> tuple[Paths, Path, dict[str, str]]:
    """Create synthetic PDFs and an approved-looking GT workbook in temp dirs."""
    samples = samples or {"UAT-01": INVOICE_ROWS}
    base = Paths(samples=(root / "samples").resolve(), ground_truth=(root / "gt").resolve(),
                 runtime=(root / "runtime").resolve())
    for directory in (base.samples, base.ground_truth, base.runtime):
        directory.mkdir(parents=True, exist_ok=True)
    sheets, hashes = {}, {}
    for sample_id, rows in samples.items():
        filename = f"synthetic_{sample_id}.pdf"
        hashes[sample_id] = write_pdf(base.samples / filename, sample_id)
        sheets[sample_id] = ({"profile": profile if isinstance(profile, str) else profile[sample_id], "pages": "1", "filename": filename,
                              "sha256": hashes[sample_id]}, rows)
    workbook = write_workbook(base.ground_truth / "gt_icea_compilata.xlsx", sheets)
    return base, workbook, hashes


def field(path, raw, page=1, group=None, normalized=None, status="present"):
    return {"field_path": path, "value_raw": raw, "value_normalized": normalized, "page": page,
            "group_id": group, "occurrence_status": status,
            "text_evidence": f"evidence {raw}" if status == "present" else None}


GOOD_FIELDS = [
    field("invoice.number", "SYN-001"),
    field("invoice.date", "22.01.2024"),
    field("invoice.verification", None, page=None, normalized=True),
    field("invoice.lines[].net_weight", "100.5", group="L1"),
    field("invoice.lines[].net_weight", "200", group="L2"),
    # An extra occurrence before the expected ones must not shift the vector.
    field("invoice.lines[].lot_numbers[]", "EXTRA-9", group="L1"),
    field("invoice.lines[].lot_numbers[]", "LOT-A", group="L1"),
    field("invoice.lines[].lot_numbers[]", "LOT-B", group="L2"),
]


def response_body(request_fields: dict, *, filename: str, sha256: str, fields=None,
                  field_issues=None, pipeline=12.0) -> dict:
    option = request_fields["extraction_option"]
    return {"api_version": "2.0", "run_id": f"api-{option}",
            "extraction": {"source_file": filename, "source_sha256": sha256,
                           "document_profile_code": request_fields["profile"],
                           "processing_status": "partial", "warnings": [],
                           "fields": GOOD_FIELDS if fields is None else fields},
            "field_issues": field_issues or [],
            "metrics": {"pipeline_seconds": pipeline}}


def multipart_fields(request: httpx.Request) -> dict:
    content = request.read().decode("latin-1")
    found = dict(re.findall(r'name="(\w+)"\r\n\r\n([^\r]*)\r\n', content))
    filename = re.search(r'name="file"; filename="([^"]+)"', content)
    found["filename"] = filename.group(1) if filename else None
    return found


class FakeApi:
    """httpx.MockTransport handler that records each request."""

    def __init__(self, hashes: dict[str, str], responder=None):
        self.requests = []
        self.hashes = hashes
        self.responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        fields = multipart_fields(request)
        if fields.get("profile") not in CANONICAL_PROFILES:
            # Same contract as the server: historical or unknown codes are rejected.
            return httpx.Response(422, json={"detail": "invalid profile"})
        self.requests.append({"path": request.url.path, "auth": request.headers.get("authorization"),
                              **fields})
        if self.responder:
            result = self.responder(request, fields)
            if result is not None:
                return result
        sample_id = re.search(r"synthetic_(UAT-\d+)\.pdf", fields["filename"]).group(1)
        return httpx.Response(200, json=response_body(fields, filename=fields["filename"],
                                                      sha256=self.hashes[sample_id]))


def all_methods() -> list[str]:
    return list(METHODS)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
