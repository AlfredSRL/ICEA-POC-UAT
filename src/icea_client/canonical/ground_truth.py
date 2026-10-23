"""Read the local DEV workbook without exporting its expected values."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from contracts import CONTRACTS

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"m": MAIN}


@dataclass(frozen=True)
class TruthRow:
    row_id: str
    field_path: str
    role: str | None
    page: str | None
    group_id: str | None
    value_raw: str | None
    value_normalized: str | None
    status: str | None
    rule: str | None
    validation: str | None


@dataclass(frozen=True)
class DevDocument:
    sample_id: str
    profile: str
    filename: str
    sha256: str
    page_count: int
    rows: tuple[TruthRow, ...]


def date_styles(archive: zipfile.ZipFile) -> set[int]:
    if "xl/styles.xml" not in archive.namelist():
        return set()
    styles = ET.fromstring(archive.read("xl/styles.xml"))
    formats = {int(item.get("numFmtId")): item.get("formatCode", "")
               for item in styles.findall("m:numFmts/m:numFmt", NS)}
    result = set()
    for index, item in enumerate(styles.findall("m:cellXfs/m:xf", NS)):
        format_id = int(item.get("numFmtId", "0"))
        code = re.sub(r'"[^"]*"|\\.', "", formats.get(format_id, ""))
        if format_id in set(range(14, 23)) or (re.search(r"[yd]", code, re.I) and re.search(r"m", code, re.I)):
            result.add(index)
    return result


def _excel_number(text: str) -> str:
    """Read a stored decimal as Excel shows it: 15 significant digits.

    A stored value such as 1234.5600000000002 is the binary form of 1234.56; the
    reviewer saw and approved the 15-digit value. Integers and identifiers are untouched.
    """
    if not re.fullmatch(r"-?\d+\.\d+", text) or len(re.sub(r"\D", "", text).lstrip("0")) <= 15:
        return text
    shown = format(float(text), ".15g")
    return shown if "e" not in shown else text


def _text(cell: ET.Element, shared: list[str], date_style_ids: set[int] = frozenset(),
          date1904: bool = False) -> str | None:
    value = cell.find(f"{{{MAIN}}}v")
    inline = cell.find(f"{{{MAIN}}}is")
    if cell.get("t") == "b" and value is not None:
        result = "true" if value.text == "1" else "false"
    elif cell.get("t") == "s" and value is not None:
        result = shared[int(value.text or "0")]
    elif inline is not None:
        result = "".join(inline.itertext())
    elif value is not None:
        result = value.text or ""
        if cell.get("t") in {None, "n"}:
            result = _excel_number(result)
        if int(cell.get("s", "0")) in date_style_ids and cell.get("t") in {None, "n"}:
            try:
                epoch = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30)
                result = (epoch + timedelta(days=float(result))).date().isoformat()
            except (ValueError, OverflowError):
                pass
    else:
        return None
    return result if result != "" else None


def _rows(xml: bytes, shared: list[str], date_style_ids: set[int] = frozenset(),
          date1904: bool = False) -> dict[int, dict[str, str]]:
    root = ET.fromstring(xml)
    result = {}
    for row in root.findall("m:sheetData/m:row", NS):
        cells = {}
        for cell in row.findall("m:c", NS):
            value = _text(cell, shared, date_style_ids, date1904)
            if value is not None:
                column = re.match(r"[A-Z]+", cell.get("r", ""))
                if column:
                    cells[column.group()] = value
        result[int(row.get("r", "0"))] = cells
    return result


def load_registry(workbook_path: Path) -> dict[str, DevDocument]:
    """Use only DEV sheets. Expected values stay in memory and local runtime."""
    registry = {}
    with zipfile.ZipFile(workbook_path) as archive:
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(item.itertext()) for item in shared_root.findall("m:si", NS)]
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        style_ids = date_styles(archive)
        properties = workbook.find("m:workbookPr", NS)
        date1904 = properties is not None and properties.get("date1904") in {"1", "true"}
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {item.get("Id"): item.get("Target") for item in rels.findall(f"{{{PACKAGE_REL}}}Relationship")}
        for sheet in workbook.findall("m:sheets/m:sheet", NS):
            sample_id = sheet.get("name", "")
            if not re.fullmatch(r"DEV-\d{2}", sample_id):
                continue
            target = targets[sheet.get(f"{{{REL}}}id")]
            sheet_path = target.lstrip("/") if target.startswith("/") else "xl/" + target
            data = _rows(archive.read(sheet_path), shared, style_ids, date1904)
            meta = data[4]
            sha256 = data.get(6, {}).get("B")
            if meta.get("C") == "Profilo" and meta.get("K") == "File":
                # Single-row header (SELEZIONE-TERZISTI sheets): file and SHA-256 in row 4.
                profile, pages = meta.get("D"), meta.get("J")
                filename = meta.get("L")
                sha256 = meta.get("N") if meta.get("M") == "SHA-256" else None
            elif meta.get("C") == "Profilo":
                profile, pages = meta.get("D"), meta.get("J")
                filename = data[5].get("B")
            else:
                profile, pages = meta.get("E"), meta.get("N")
                filename = data[5].get("G")
            if profile not in CONTRACTS or not filename or not pages:
                raise ValueError(f"incomplete DEV metadata: {sample_id}")
            sha256 = (sha256 or "").strip().lower()
            truth_rows = []
            for number, cells in data.items():
                if number < 10 or not cells.get("C"):
                    continue
                truth_rows.append(TruthRow(
                    row_id=cells.get("A") or f"{sample_id}:{number}",
                    field_path=cells["C"].strip(), role=cells.get("D"),
                    page=cells.get("F"),
                    group_id=cells.get("G") or None,
                    value_raw=cells.get("H"), value_normalized=cells.get("I"),
                    status=cells.get("J"), rule=cells.get("K"),
                    validation=cells.get("M"),
                ))
            registry[sample_id] = DevDocument(
                sample_id, profile, Path(filename.replace("\\", "/")).name,
                sha256, int(float(pages)), tuple(truth_rows),
            )
    if not registry:
        raise ValueError("no DEV samples in workbook")
    return registry
