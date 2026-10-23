"""Comparison workbook (openpyxl) and its structural verification."""
from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.chart import BarChart, Reference, ScatterChart, Series
from openpyxl.chart.data_source import NumFmt
from openpyxl.chart.label import DataLabelList
from openpyxl.chart.marker import Marker
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from .config import METHOD_NAMES, METHODS

NAMES = [METHOD_NAMES[method] for method in METHODS]
PALETTE = ["315A8A", "A35B28", "24806D", "7450A0"]
OUTCOME_COLORS = {
    "matched": "DDF2E3", "partial": "FFF0BF", "incorrect": "FCE0E0", "omitted": "E8EAF0",
    "unresolved_group": "FFE2C4", "invalid_field": "FBC8B5", "unassessed": "F5E4FE",
    "non appaiata": "FFE2C4", "GT non valutabile": "F5E4FE", "non eseguita": "E8EAF0",
    "valore alternativo accettato": "DDF2E3",
}
FAILURE_COLOR = "FCE0E0"
SHORT_PROFILES = {"TC": "TC", "FATTURA-VENDITA": "Fattura", "DDT-VENDITA": "DDT",
                  "FATTURA-ACQUISTO-RMDF": "Fattura RMDF", "DDT-ACQUISTO-RMDF": "DDT RMDF",
                  "SELEZIONE-TERZISTI": "Selezione terzisti",
                  "DISPOSIZIONE-CONTO-LAVORO": "Conto lavoro", "ELENCO-TERZISTI": "Terzisti"}
HEADER_FILL = PatternFill("solid", fgColor="17324D")
HEADER_FONT = Font(name="Aptos", size=10, bold=True, color="FFFFFF")
BODY_FONT = Font(name="Aptos", size=10, color="172B40")
TITLE_FONT = Font(name="Aptos", size=15, bold=True, color="17324D")
SECTION_FONT = Font(name="Aptos", size=12, bold=True, color="17324D")
NOTE_FONT = Font(name="Aptos", size=10, italic=True, color="35546F")
THIN = Side(style="thin", color="DDE7EE")
FIRST_DATA_ROW = 16
SAMPLE_HEADERS = [
    "Tipo riga", "Indice interno GT", "ID riga GT", "Campo canonico", "Classe", "Pagina GT",
    "Stato GT", "Valore GT raw", "Valore GT normalizzato", "Regola di confronto",
    *[f"{name} · valore normalizzato" for name in NAMES],
    *[f"{name} · esito" for name in NAMES],
    *[f"{name} · raw / pagina / gruppo / indice" for name in NAMES],
    "Anomalia GT",
]
# Summary anchors, shared with verify_workbook.
OVERALL_FIRST, PROFILE_TABLE_FIRST, PROFILE_CHART_FIRST = 8, 17, 43
TIMING_FIRST, TIMING_PROFILE_FIRST = 53, 61


def _cell_value(value):
    if isinstance(value, bool):
        # Boolean judgments are shown with the contract labels, never as Excel TRUE/FALSE.
        return "true" if value else "false"
    if value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return str(value)


def put(sheet, address: str, value, *, text: bool = False):
    """Write a value; strings are always stored as text, never as formulas."""
    cell = sheet[address]
    cell.value = _cell_value(value)
    if isinstance(cell.value, str):
        cell.data_type = "s"
        if text:
            cell.number_format = "@"
    return cell


def put_row(sheet, row: int, values: list, first_column: int = 1):
    for offset, value in enumerate(values):
        cell = sheet.cell(row=row, column=first_column + offset)
        cell.value = _cell_value(value)
        if isinstance(cell.value, str):
            cell.data_type = "s"


def style_header(sheet, row: int, first: int, last: int, height: float = 33):
    for column in range(first, last + 1):
        cell = sheet.cell(row=row, column=column)
        cell.fill, cell.font = HEADER_FILL, HEADER_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
    sheet.row_dimensions[row].height = height


def style_body(sheet, first_row: int, last_row: int, first: int, last: int, number_format=None,
               number_columns=()):
    for row in range(first_row, last_row + 1):
        for column in range(first, last + 1):
            cell = sheet.cell(row=row, column=column)
            cell.font = BODY_FONT
            cell.alignment = Alignment(vertical="center")
            cell.border = Border(bottom=Side(style="thin", color="E3EAF0"))
            if number_format and column in number_columns:
                cell.number_format = number_format


def _percent(part, whole):
    return 100 * part / whole if whole else None


def _detail(item: dict) -> str | None:
    if not item or (item.get("raw") is None and item.get("page") is None
                    and item.get("group") is None and not item.get("diagnostic_outcome")):
        return None
    parts = []
    if item.get("raw") is not None:
        parts.append(str(item["raw"]))
    parts.append(f"pagina {item.get('page') if item.get('page') is not None else '—'}")
    parts.append(f"gruppo {item.get('group') if item.get('group') is not None else '—'}")
    parts.append(f"indice {item.get('item_index') if item.get('item_index') is not None else '—'}")
    if item.get("alignment"):
        parts.append(item["alignment"])
    if item.get("field_issue_label"):
        parts.append(f"anomalia: {item['field_issue_label']}")
    if item.get("value_outcome") and item["value_outcome"] != item.get("outcome"):
        parts.append(f"score conservativo: {item.get('outcome')}")
    return "\n".join(parts)


def _value_axis_percent(chart):
    chart.y_axis.scaling.min = 0
    chart.y_axis.scaling.max = 100
    chart.y_axis.numFmt = NumFmt(formatCode='0"%"', sourceLinked=False)
    chart.x_axis.delete = False
    chart.y_axis.delete = False


def _summary_sheet(workbook: Workbook, data: dict, summary: dict) -> None:
    sheet = workbook.active
    sheet.title = "Sintesi"
    sheet.sheet_view.showGridLines = False
    sheet.sheet_properties.tabColor = "17324D"
    put(sheet, "A2", "Sintesi estrazioni UAT").font = TITLE_FONT
    put(sheet, "A4", f"GT {data['ground_truth_file']} · {len(data['samples'])} campioni · "
                     f"SHA-256 {data['ground_truth_sha256']}").font = NOTE_FONT
    put(sheet, "A5", "Score provvisorio. Match % e Partial % usano tutte le righe GT valutabili; "
                     "partial non è match.").font = NOTE_FONT
    if data.get("excluded_samples"):
        put(sheet, "A6", "Esclusi: " + ", ".join(
            f"{item['sample_id']} ({item['profile']}, {item['reason']})" for item in data["excluded_samples"])).font = NOTE_FONT
    headers = ["Soluzione", "Campioni valutabili", "Righe valutabili", "Match", "Partial",
               "Incorrect", "Omitted", "Gruppi irrisolti", "Match %", "Partial %", "Non appaiate"]
    put_row(sheet, OVERALL_FIRST - 1, headers)
    for index, method in enumerate(METHODS):
        item = summary["totals"][method]
        put_row(sheet, OVERALL_FIRST + index, [
            NAMES[index], f"{item['valid_samples']}/{item['samples']}", item["evaluable_rows"],
            item["matched"], item["partial"], item["incorrect"], item["omitted"],
            item["unresolved_group"], _percent(item["matched"], item["evaluable_rows"]),
            _percent(item["partial"], item["evaluable_rows"]), item["unpaired_occurrences"]])
    style_header(sheet, OVERALL_FIRST - 1, 1, 11)
    style_body(sheet, OVERALL_FIRST, OVERALL_FIRST + 3, 1, 11, "0.0", (9, 10))

    put(sheet, "A14", "Risultati per profilo").font = SECTION_FONT
    put_row(sheet, PROFILE_TABLE_FIRST - 1, ["Profilo", "Soluzione", "Campioni valutabili",
                                             "Righe valutabili", "Match", "Partial", "Incorrect",
                                             "Omitted", "Gruppi irrisolti", "Match %"])
    profiles = sorted(summary["totals_by_profile"])
    row = PROFILE_TABLE_FIRST
    for profile in profiles:
        for index, method in enumerate(METHODS):
            item = summary["totals_by_profile"][profile][method]
            put_row(sheet, row, [profile, NAMES[index], f"{item['valid_samples']}/{item['samples']}",
                                 item["evaluable_rows"], item["matched"], item["partial"],
                                 item["incorrect"], item["omitted"], item["unresolved_group"],
                                 _percent(item["matched"], item["evaluable_rows"])])
            row += 1
    style_header(sheet, PROFILE_TABLE_FIRST - 1, 1, 10)
    style_body(sheet, PROFILE_TABLE_FIRST, max(row - 1, PROFILE_TABLE_FIRST), 1, 10, "0.0", (10,))

    put(sheet, "A40", "Match per profilo (%)").font = SECTION_FONT
    put_row(sheet, PROFILE_CHART_FIRST - 1, ["Profilo", *NAMES])
    for offset, profile in enumerate(profiles):
        put_row(sheet, PROFILE_CHART_FIRST + offset, [
            SHORT_PROFILES.get(profile, profile),
            *[_percent(summary["totals_by_profile"][profile][method]["matched"],
                       summary["totals_by_profile"][profile][method]["evaluable_rows"])
              for method in METHODS]])
    profile_last = PROFILE_CHART_FIRST + max(len(profiles), 1) - 1
    style_header(sheet, PROFILE_CHART_FIRST - 1, 1, 5)
    style_body(sheet, PROFILE_CHART_FIRST, profile_last, 1, 5, "0.0", (2, 3, 4, 5))

    put(sheet, "A49", "Tempi delle chiamate").font = SECTION_FONT
    put(sheet, "A50", "Pipeline = elaborazione server (misurata dalla API). HTTP = richiesta completa "
                      "misurata dal client, incluso upload.").font = NOTE_FONT
    put_row(sheet, TIMING_FIRST - 1, ["Soluzione", "Chiamate valide", "Pipeline media (s)",
                                      "Pipeline mediana (s)", "HTTP medio (s)", "HTTP mediano (s)"])
    for index, method in enumerate(METHODS):
        item = summary["timing_totals"][method]
        put_row(sheet, TIMING_FIRST + index, [
            NAMES[index], f"{item['valid']}/{item['samples']}",
            item["pipeline"]["mean_seconds"], item["pipeline"]["median_seconds"],
            item["http"]["mean_seconds"], item["http"]["median_seconds"]])
    style_header(sheet, TIMING_FIRST - 1, 1, 6)
    style_body(sheet, TIMING_FIRST, TIMING_FIRST + 3, 1, 6, "0.00", range(3, 7))

    put(sheet, "A58", "Tempi per profilo").font = SECTION_FONT
    put_row(sheet, TIMING_PROFILE_FIRST - 1, ["Profilo", "Soluzione", "Chiamate valide",
                                              "Pipeline media (s)", "HTTP medio (s)"])
    row = TIMING_PROFILE_FIRST
    for profile in profiles:
        for index, method in enumerate(METHODS):
            item = summary["timing_by_profile"][profile][method]
            put_row(sheet, row, [profile, NAMES[index], f"{item['valid']}/{item['samples']}",
                                 item["pipeline"]["mean_seconds"], item["http"]["mean_seconds"]])
            row += 1
    style_header(sheet, TIMING_PROFILE_FIRST - 1, 1, 5)
    style_body(sheet, TIMING_PROFILE_FIRST, max(row - 1, TIMING_PROFILE_FIRST), 1, 5, "0.00", (4, 5))
    for column, width in zip("ABCDEFGHIJKL", (31, 24, 19, 19, 16, 16, 16, 21, 17, 17, 19, 3)):
        sheet.column_dimensions[column].width = width

    overall = BarChart()
    overall.type, overall.grouping, overall.overlap, overall.gapWidth = "col", "stacked", 100, 110
    overall.title = "Match e partial"
    overall.add_data(Reference(sheet, min_col=9, max_col=10, min_row=OVERALL_FIRST - 1,
                               max_row=OVERALL_FIRST + 3), titles_from_data=True)
    overall.set_categories(Reference(sheet, min_col=1, min_row=OVERALL_FIRST, max_row=OVERALL_FIRST + 3))
    overall.series[0].graphicalProperties.solidFill = PALETTE[0]
    overall.series[1].graphicalProperties.solidFill = "E87536"
    overall.dataLabels = DataLabelList()
    overall.dataLabels.showVal = True
    overall.dataLabels.numFmt = "0.0"
    overall.legend.position = "t"
    _value_axis_percent(overall)
    overall.width, overall.height = 18, 9
    sheet.add_chart(overall, "M6")

    by_profile = BarChart()
    by_profile.type = "col"
    by_profile.title = "Match per profilo"
    by_profile.add_data(Reference(sheet, min_col=2, max_col=5, min_row=PROFILE_CHART_FIRST - 1,
                                  max_row=profile_last), titles_from_data=True)
    by_profile.set_categories(Reference(sheet, min_col=1, min_row=PROFILE_CHART_FIRST, max_row=profile_last))
    for index, series in enumerate(by_profile.series):
        series.graphicalProperties.solidFill = PALETTE[index]
    by_profile.dataLabels = DataLabelList()
    by_profile.dataLabels.showVal = True
    by_profile.dataLabels.numFmt = "0.0"
    by_profile.legend.position = "t"
    _value_axis_percent(by_profile)
    by_profile.width, by_profile.height = 18, 11
    sheet.add_chart(by_profile, "M22")

    scatter = ScatterChart()
    scatter.title = "Tempo pipeline e match"
    scatter.style = 13
    scatter.x_axis.title = "Tempo pipeline medio (s)"
    scatter.y_axis.title = "Match (righe GT)"
    for index in range(len(METHODS)):
        x_values = Reference(sheet, min_col=3, min_row=TIMING_FIRST + index)
        y_values = Reference(sheet, min_col=4, min_row=OVERALL_FIRST + index)
        series = Series(y_values, x_values, title=NAMES[index])
        series.marker = Marker(symbol="circle", size=10)
        series.marker.graphicalProperties.solidFill = PALETTE[index]
        series.marker.graphicalProperties.line.solidFill = PALETTE[index]
        series.graphicalProperties.line.noFill = True
        scatter.series.append(series)
    scatter.y_axis.scaling.min = 0
    scatter.y_axis.scaling.max = max(1, max(summary["totals"][m]["evaluable_rows"] for m in METHODS))
    scatter.x_axis.scaling.min = 0
    scatter.x_axis.axPos = "b"
    scatter.x_axis.delete = False
    scatter.y_axis.delete = False
    scatter.legend.position = "t"
    scatter.width, scatter.height = 18, 11
    sheet.add_chart(scatter, "M48")


def _sample_sheet(workbook: Workbook, sample: dict) -> None:
    sheet = workbook.create_sheet(sample["sample_id"])
    sheet.sheet_view.showGridLines = False
    sheet.sheet_properties.tabColor = "277DA1"
    put(sheet, "A2", f"CONFRONTO ESTRAZIONI · {sample['sample_id']} · {sample['profile']}").font = TITLE_FONT
    labels = [("Filename", sample["filename"]),
              (f"Run API ({' / '.join(METHODS)})",
               " / ".join(str(sample["methods"][m]["api_run_id"] or "—") for m in METHODS)),
              ("SHA256 GT / PDF", f"{sample['ground_truth_sha256']} / {sample['source_sha256']}")]
    for offset, (label, value) in enumerate(labels):
        put(sheet, f"A{3 + offset}", label).font = Font(name="Aptos", size=10, bold=True, color="35546F")
        put(sheet, f"B{3 + offset}", value, text=True)
    put(sheet, "A6", "Esito colorato: confronto del valore. Se il gruppo è incerto, lo score "
                     "conservativo è nel dettaglio e nella sintesi.").font = NOTE_FONT
    put_row(sheet, 8, ["Soluzione", "Pipeline (s)", "HTTP (s)", "Esito"])
    for index, method in enumerate(METHODS):
        info = sample["methods"][method]
        timing = info["timing"]
        status = ("valido" if info["status"] == "succeeded" and not info["field_issues"]
                  else "valido parziale" if info["status"] == "succeeded" else info["error"])
        put_row(sheet, 9 + index, [NAMES[index], timing["pipeline_seconds"],
                                   timing["http_seconds"], status])
    style_header(sheet, 8, 1, 4)
    style_body(sheet, 9, 12, 1, 4, "0.00", (2, 3))

    header_row = FIRST_DATA_ROW - 1
    put_row(sheet, header_row, SAMPLE_HEADERS)
    style_header(sheet, header_row, 1, len(SAMPLE_HEADERS), 44)
    ordinals = {}
    last_row = header_row + len(sample["rows"])
    for offset, row in enumerate(sample["rows"]):
        excel_row = FIRST_DATA_ROW + offset
        ordinals[row["field_path"]] = ordinals.get(row["field_path"], 0) + 1
        values = [row["methods"].get(method) or {} for method in METHODS]
        put_row(sheet, excel_row, [
            row["kind"], ordinals[row["field_path"]], row.get("row_id"), row["field_path"],
            row.get("role"), row.get("gt_page"), row.get("gt_status"), row.get("gt_raw"),
            row.get("gt_normalized"), row.get("rule"),
            *[value.get("normalized") for value in values],
            *[value.get("value_outcome") or value.get("outcome") for value in values],
            *[_detail(value) for value in values], row.get("gt_issue")])
        fill = PatternFill("solid", fgColor="F2F8FC" if row["kind"] == "GT" else "FFF8E8")
        for column in range(1, len(SAMPLE_HEADERS) + 1):
            cell = sheet.cell(row=excel_row, column=column)
            cell.fill = fill
            cell.font = BODY_FONT
            cell.alignment = Alignment(wrap_text=True, vertical="center")
            cell.border = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
            # Identifiers and raw evidence stay text (leading zeros); column I keeps types.
            if column == 8 or 11 <= column <= 14:
                cell.number_format = "@"
        sheet.row_dimensions[excel_row].height = 36
    if sample["rows"]:
        target = f"O{FIRST_DATA_ROW}:R{last_row}"
        for status, color in OUTCOME_COLORS.items():
            sheet.conditional_formatting.add(target, FormulaRule(
                formula=[f'O{FIRST_DATA_ROW}="{status}"'],
                fill=PatternFill("solid", bgColor=color, fgColor=color)))
        sheet.conditional_formatting.add(target, FormulaRule(
            formula=[f'LEFT(O{FIRST_DATA_ROW},10)="errore API"'],
            fill=PatternFill("solid", bgColor=FAILURE_COLOR, fgColor=FAILURE_COLOR)))
    widths = (19, 16, 16, 51, 18, 10, 13, 37, 37, 21, 34, 34, 34, 34, 16, 16, 16, 16, 43, 43, 43, 43, 30)
    for column, width in enumerate(widths, 1):
        sheet.column_dimensions[sheet.cell(row=1, column=column).column_letter].width = width
    sheet.freeze_panes = f"E{FIRST_DATA_ROW}"


# Error analysis, aligned with the server's build_comparison_workbook.mjs. Runs that
# failed (schema or API) and GT rows that are Ambiguo/not evaluable carry outcomes
# outside this set, so they never count as field errors.
ERROR_OUTCOMES = {"partial", "incorrect", "omitted", "unresolved_group", "invalid_field",
                  "unassessed", "non appaiata"}
ERROR_SHEETS = ("Errori per campo", "Errori")


def _outcome(item: dict) -> str | None:
    return item.get("value_outcome") or item.get("outcome")


def error_analysis(data: dict) -> tuple[list[dict], list[list]]:
    stats, rows = {}, []
    for sample in data["samples"]:
        for row in sample["rows"]:
            items = [row["methods"].get(method) or {} for method in METHODS]
            outcomes = [_outcome(item) for item in items]
            if row["kind"] == "GT":
                key = (sample["profile"], row["field_path"])
                stat = stats.setdefault(key, {"profile": key[0], "path": key[1], "rows": 0,
                                              **{name: [0] * len(METHODS) for name in
                                                 ("errors", "incorrect", "omitted", "partial")}})
                if not all(outcome == "GT non valutabile" for outcome in outcomes):
                    stat["rows"] += 1
                for index, outcome in enumerate(outcomes):
                    stat["errors"][index] += outcome in ERROR_OUTCOMES
                    for name in ("incorrect", "omitted", "partial"):
                        stat[name][index] += outcome == name
            failing = sum(outcome in ERROR_OUTCOMES for outcome in outcomes)
            if not failing:
                continue
            rows.append([sample["sample_id"], sample["profile"], row["kind"], row.get("row_id"),
                         row["field_path"], row.get("rule"), row.get("gt_status"), row.get("gt_group"),
                         row.get("gt_raw"), row.get("gt_normalized"), failing,
                         *[value for item in items for value in
                           (_outcome(item), item.get("raw"), item.get("normalized"), item.get("evidence"))]])
    ordered = sorted((stat for stat in stats.values() if stat["rows"] > 0),
                     key=lambda stat: (-sum(stat["errors"]), stat["profile"], stat["path"]))
    return ordered, rows


def _color_outcomes(sheet, column_letter: str, first: int, last: int) -> None:
    target = f"{column_letter}{first}:{column_letter}{last}"
    for status, color in OUTCOME_COLORS.items():
        sheet.conditional_formatting.add(target, FormulaRule(
            formula=[f'{column_letter}{first}="{status}"'],
            fill=PatternFill("solid", bgColor=color, fgColor=color)))


def _error_sheets(workbook: Workbook, data: dict) -> None:
    stats, rows = error_analysis(data)
    by_field = workbook.create_sheet("Errori per campo")
    by_field.sheet_view.showGridLines = False
    by_field.sheet_properties.tabColor = "A35B28"
    put(by_field, "A1", "ERRORI PER CAMPO · righe GT valutabili non in match pieno, per soluzione").font = TITLE_FONT
    put(by_field, "A2", "Ordinato per errori totali. Errori = partial + incorrect + omitted + gruppi irrisolti "
                        "+ campi invalidi. Escluse le righe Ambiguo o non valutabili e le run fallite "
                        "(schema non valido o errore API).").font = NOTE_FONT
    header = ["Profilo", "Campo canonico", "Righe GT valutabili",
              *[f"{name} · {kind}" for kind in ("errori", "incorrect", "omitted", "partial") for name in NAMES],
              "Errori totali"]
    put_row(by_field, 4, header)
    style_header(by_field, 4, 1, len(header), 44)
    for offset, stat in enumerate(stats):
        put_row(by_field, 5 + offset, [stat["profile"], stat["path"], stat["rows"], *stat["errors"],
                                       *stat["incorrect"], *stat["omitted"], *stat["partial"],
                                       sum(stat["errors"])])
    if stats:
        style_body(by_field, 5, 4 + len(stats), 1, len(header))
    by_field.column_dimensions["A"].width = 24
    by_field.column_dimensions["B"].width = 56
    by_field.freeze_panes = "C5"

    errors = workbook.create_sheet("Errori")
    errors.sheet_view.showGridLines = False
    errors.sheet_properties.tabColor = "8B3A3A"
    put(errors, "A1", "ERRORI · righe con almeno una soluzione non in match pieno").font = TITLE_FONT
    put(errors, "A2", "Una riga per occorrenza GT o extra del modello. Per ogni soluzione: esito, valore raw, "
                      "valore normalizzato usato nel confronto ed evidenza testuale del modello.").font = NOTE_FONT
    header = ["Campione", "Profilo", "Tipo riga", "ID riga GT", "Campo canonico", "Regola", "Stato GT",
              "Gruppo GT", "Valore GT raw", "Valore GT normalizzato", "Soluzioni in errore",
              *[f"{name} · {kind}" for name in NAMES for kind in ("esito", "raw", "normalizzato", "evidenza")]]
    put_row(errors, 4, header)
    style_header(errors, 4, 1, len(header), 44)
    for offset, values in enumerate(rows):
        put_row(errors, 5 + offset, values)
        for column in range(1, len(header) + 1):
            cell = errors.cell(row=5 + offset, column=column)
            cell.font = BODY_FONT
            cell.alignment = Alignment(wrap_text=True, vertical="center")
            cell.border = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
            if column != 10 and column != 11:
                cell.number_format = "@"
    if rows:
        for index in range(len(METHODS)):
            letter = errors.cell(row=4, column=12 + 4 * index).column_letter
            _color_outcomes(errors, letter, 5, 4 + len(rows))
    widths = {1: 10, 2: 22, 3: 9, 4: 13, 5: 46, 6: 18, 7: 10, 8: 9, 9: 32, 10: 32, 11: 10}
    for column in range(1, len(header) + 1):
        kind = (column - 12) % 4
        width = widths.get(column, 14 if kind == 0 else 40 if kind == 3 else 26)
        errors.column_dimensions[errors.cell(row=4, column=column).column_letter].width = width
    errors.freeze_panes = "F5"

def build_workbook(data: dict, summary: dict, path: Path) -> Path:
    data = json.loads(json.dumps(data, default=str))
    summary = json.loads(json.dumps(summary, default=str))
    workbook = Workbook()
    _summary_sheet(workbook, data, summary)
    _error_sheets(workbook, data)
    for sample in data["samples"]:
        _sample_sheet(workbook, sample)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    return path


C_NS = {"c": "http://schemas.openxmlformats.org/drawingml/2006/chart"}


def _close(actual, expected) -> bool:
    if expected is None:
        return actual is None
    return isinstance(actual, (int, float)) and abs(actual - expected) <= 1e-6


def verify_workbook(path: Path, data: dict, summary: dict) -> None:
    """Raise AssertionError when the workbook does not match the comparison data."""
    data = json.loads(json.dumps(data, default=str))
    with zipfile.ZipFile(path) as archive:
        charts = sorted(name for name in archive.namelist()
                        if re.fullmatch(r"xl/charts/chart\d+\.xml", name))
        if len(charts) != 3:
            raise AssertionError(f"expected 3 charts, found {len(charts)}")
        bars, scatters = [], []
        for name in charts:
            root = ET.fromstring(archive.read(name))
            bar, scatter = root.find(".//c:barChart", C_NS), root.find(".//c:scatterChart", C_NS)
            if bar is not None:
                bars.append((root, bar))
            elif scatter is not None:
                scatters.append((root, scatter))
            else:
                raise AssertionError(f"unexpected chart type in {name}")
        if len(bars) != 2 or len(scatters) != 1:
            raise AssertionError("expected two bar charts and one scatter chart")
        for root, bar in bars:
            axis = root.find(".//c:valAx", C_NS)
            if (axis is None or axis.find("c:scaling/c:min[@val='0']", C_NS) is None
                    or axis.find("c:scaling/c:max[@val='100']", C_NS) is None
                    or axis.find("c:numFmt[@formatCode='0\"%\"']", C_NS) is None):
                raise AssertionError("bar chart value axis is not 0-100%")
            if bar.find(".//c:dLbls/c:showVal[@val='1']", C_NS) is None:
                raise AssertionError("bar chart lacks value labels")
        stacked = [bar for _, bar in bars if bar.find("c:grouping[@val='stacked']", C_NS) is not None]
        if len(stacked) != 1 or stacked[0].find("c:barDir[@val='col']", C_NS) is None:
            raise AssertionError("missing stacked column chart")
        sources = [node.text for node in stacked[0].findall("c:ser/c:val/c:numRef/c:f", C_NS)]
        if (len(sources) != 2 or not sources[0].endswith(f"$I${OVERALL_FIRST}:$I${OVERALL_FIRST + 3}")
                or not sources[1].endswith(f"$J${OVERALL_FIRST}:$J${OVERALL_FIRST + 3}")):
            raise AssertionError("stacked chart is not bound to Match % and Partial %")
        root, scatter = scatters[0]
        if len(scatter.findall("c:ser", C_NS)) != 4:
            raise AssertionError("scatter chart must have four solution points")
        rows = max(1, max(summary["totals"][method]["evaluable_rows"] for method in METHODS))
        y_axes = [axis for axis in root.findall(".//c:valAx", C_NS)
                  if axis.find("c:axPos[@val='l']", C_NS) is not None]
        if not y_axes or y_axes[0].find(f"c:scaling/c:max[@val='{rows}']", C_NS) is None:
            raise AssertionError("scatter match axis does not cover all evaluable GT rows")
    workbook = load_workbook(path)
    expected = ["Sintesi", *ERROR_SHEETS, *[sample["sample_id"] for sample in data["samples"]]]
    if workbook.sheetnames != expected:
        raise AssertionError("unexpected worksheet names or order")
    stats, error_rows = error_analysis(data)
    for name, expected_rows in (("Errori per campo", len(stats)), ("Errori", len(error_rows))):
        sheet = workbook[name]
        written = sum(1 for row in range(5, sheet.max_row + 1) if sheet.cell(row=row, column=1).value is not None)
        if written != expected_rows:
            raise AssertionError(f"{name}: {written} rows instead of {expected_rows}")
    summary_sheet = workbook["Sintesi"]
    for index, method in enumerate(METHODS):
        item = summary["totals"][method]
        for column, key in (("I", "matched"), ("J", "partial")):
            cell = summary_sheet[f"{column}{OVERALL_FIRST + index}"].value
            if not _close(cell, _percent(item[key], item["evaluable_rows"])):
                raise AssertionError(f"summary cell {column}{OVERALL_FIRST + index} differs")
    for sample in data["samples"]:
        sheet = workbook[sample["sample_id"]]
        for index, method in enumerate(METHODS):
            timing = sample["methods"][method]["timing"]
            for column, key in (("B", "pipeline_seconds"), ("C", "http_seconds")):
                if not _close(sheet[f"{column}{9 + index}"].value, timing[key]):
                    raise AssertionError(f"{sample['sample_id']}!{column}{9 + index} timing differs")
        for offset, row in enumerate(sample["rows"]):
            if row["kind"] != "GT" or row.get("rule") != "numeric_equivalent" or row.get("gt_normalized") is None:
                continue
            value = sheet[f"I{FIRST_DATA_ROW + offset}"].value
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value != row["gt_normalized"]:
                raise AssertionError(f"{sample['sample_id']}!I{FIRST_DATA_ROW + offset} is not the typed GT number")
        if sheet[f"D{FIRST_DATA_ROW - 1}"].value != "Campo canonico":
            raise AssertionError(f"{sample['sample_id']} header row moved")
