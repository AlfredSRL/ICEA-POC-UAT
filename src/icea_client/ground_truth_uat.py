"""Read the ICEA-approved UAT ground-truth workbook through an explicit layout.

Expected values are never modified. The layout maps workbook cells to the
canonical TruthRow fields; a workbook that does not fit the layout raises an
error instead of being guessed.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import canonical  # noqa: F401
from ground_truth import NS, PACKAGE_REL, REL, DevDocument, TruthRow, _rows, date_styles

DEFAULT_LAYOUT = Path(__file__).resolve().parent / "layouts" / "default.json"
ROW_FIELDS = ("row_id", "field_path", "role", "page", "group_id", "value_raw",
              "value_normalized", "status", "rule", "validation")
METADATA_FIELDS = ("profile", "page_count", "filename", "sha256")
CELL = re.compile(r"^([A-Z]{1,3})(\d+)$")


@dataclass(frozen=True)
class Layout:
    sample_sheet_pattern: str
    metadata: dict[str, list[dict]]
    first_data_row: int
    columns: dict[str, str]
    source: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> "Layout":
        path = path or DEFAULT_LAYOUT
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        layout = cls(sample_sheet_pattern=data["sample_sheet_pattern"],
                     metadata={key: list(value) for key, value in data["metadata"].items()},
                     first_data_row=int(data["first_data_row"]),
                     columns=dict(data["columns"]), source=str(path))
        missing = [key for key in METADATA_FIELDS if not layout.metadata.get(key)]
        missing += [key for key in ("field_path", "value_raw", "value_normalized", "status", "rule")
                    if key not in layout.columns]
        unknown = set(layout.columns) - set(ROW_FIELDS)
        if missing or unknown:
            raise ValueError(f"invalid GT layout: missing={missing} unknown={sorted(unknown)}")
        re.compile(layout.sample_sheet_pattern)
        return layout


@dataclass
class GroundTruth:
    path: Path
    documents: dict[str, DevDocument]
    skipped_sheets: list[str] = field(default_factory=list)
    layout_source: str = ""


def _metadata(data: dict[int, dict[str, str]], specs: list[dict]) -> str | None:
    """Each spec is either {"cell": "D4"} or {"label": regex, "rows": [first, last]}."""
    for spec in specs:
        if "cell" in spec:
            match = CELL.match(spec["cell"])
            if not match:
                raise ValueError(f"invalid layout cell: {spec['cell']}")
            value = data.get(int(match.group(2)), {}).get(match.group(1))
        else:
            first, last = spec.get("rows", [1, 9])
            pattern = re.compile(spec["label"], re.IGNORECASE)
            value = None
            for number in range(first, last + 1):
                cells = data.get(number, {})
                ordered = sorted(cells, key=lambda column: (len(column), column))
                for index, column in enumerate(ordered):
                    if pattern.fullmatch(cells[column].strip()):
                        value = next((cells[other] for other in ordered[index + 1:]
                                      if cells[other].strip()), None)
                        break
                if value is not None:
                    break
        if value is not None and value.strip():
            return value.strip()
    return None


def load_ground_truth(path: Path, layout: Layout | None = None) -> GroundTruth:
    layout = layout or Layout.load()
    pattern = re.compile(layout.sample_sheet_pattern)
    documents, skipped = {}, []
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(item.itertext()) for item in root.findall("m:si", NS)]
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        styles = date_styles(archive)
        properties = workbook.find("m:workbookPr", NS)
        date1904 = properties is not None and properties.get("date1904") in {"1", "true"}
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {item.get("Id"): item.get("Target")
                   for item in rels.findall(f"{{{PACKAGE_REL}}}Relationship")}
        for sheet in workbook.findall("m:sheets/m:sheet", NS):
            name = sheet.get("name", "")
            if not pattern.fullmatch(name):
                skipped.append(name)
                continue
            target = targets[sheet.get(f"{{{REL}}}id")]
            sheet_path = target.lstrip("/") if target.startswith("/") else "xl/" + target
            data = _rows(archive.read(sheet_path), shared, styles, date1904)
            meta = {key: _metadata(data, layout.metadata[key]) for key in METADATA_FIELDS}
            missing = [key for key, value in meta.items() if not value]
            if missing:
                raise ValueError(f"GT sheet {name}: metadata not found by layout: {missing}")
            try:
                pages = int(float(meta["page_count"]))
            except ValueError as error:
                raise ValueError(f"GT sheet {name}: page count is not a number") from error
            rows = []
            for number in sorted(data):
                cells = data[number]
                path_cell = cells.get(layout.columns["field_path"])
                if number < layout.first_data_row or not path_cell or not path_cell.strip():
                    continue
                values = {key: cells.get(column) for key, column in layout.columns.items()}
                rows.append(TruthRow(
                    row_id=values.get("row_id") or f"{name}:{number}",
                    field_path=path_cell.strip(), role=values.get("role"),
                    page=values.get("page"), group_id=values.get("group_id") or None,
                    value_raw=values.get("value_raw"), value_normalized=values.get("value_normalized"),
                    status=values.get("status"), rule=values.get("rule"),
                    validation=values.get("validation")))
            documents[name] = DevDocument(
                name, meta["profile"], Path(meta["filename"].replace("\\", "/")).name,
                meta["sha256"].lower(), pages, tuple(rows))
    if not documents:
        raise ValueError("no sample sheets matched the GT layout pattern")
    return GroundTruth(path=path, documents=documents, skipped_sheets=skipped,
                       layout_source=layout.source)
