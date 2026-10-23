from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx

from icea_client import canonical
from icea_client.api_client import ApiClient
from icea_client.compare import run_compare
from icea_client.config import ApiSettings, check_api_url, plain_http_remote, resolve_ground_truth
from icea_client.ground_truth_uat import Layout, load_ground_truth
from icea_client.preflight import run_preflight
from icea_client.runner import RunRefused, load_records, run
from tests.fixtures import (INVOICE_ROWS, FakeApi, environment, field, read_json,
                            response_body)

TOKEN = "synthetic-token-not-a-secret"


def quiet(*_args, **_kwargs):
    pass


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.base, self.workbook, self.hashes = environment(self.root)
        self.gt = load_ground_truth(self.workbook)

    def tearDown(self):
        self._tmp.cleanup()

    def factory(self, api: FakeApi):
        settings = ApiSettings("http://localhost:8000", TOKEN, 5)
        return lambda: ApiClient(settings, transport=httpx.MockTransport(api))

    def execute(self, api: FakeApi, samples=("UAT-01",), approve=4, **kwargs):
        return run(self.gt, self.base, list(samples), execute=True, approved_calls=approve,
                   client_factory=self.factory(api), base_url="http://localhost:8000", log=quiet,
                   **kwargs)


class CanonicalTests(unittest.TestCase):
    def test_vendored_modules_match_manifest(self):
        self.assertEqual(canonical.verify_manifest(), [])


class GroundTruthTests(Case):
    def test_layout_reads_metadata_rows_and_types(self):
        document = self.gt.documents["UAT-01"]
        self.assertEqual(self.gt.skipped_sheets, ["Istruzioni"])
        self.assertEqual((document.profile, document.page_count), ("FATTURA-VENDITA", 1))
        self.assertEqual(document.filename, "synthetic_UAT-01.pdf")
        self.assertEqual(document.sha256, self.hashes["UAT-01"])
        self.assertEqual(len(document.rows), len(INVOICE_ROWS))
        weight = document.rows[3]
        self.assertEqual((weight.group_id, weight.value_raw, weight.value_normalized),
                         ("1", "100,5", "100.5"))
        self.assertIsNone(document.rows[7].value_raw)

    def test_explicit_cell_layout_and_missing_metadata(self):
        data = json.loads(Path(Layout.load().source).read_text(encoding="utf-8"))
        data["metadata"]["profile"] = [{"cell": "D4"}]
        custom = self.root / "layout.json"
        custom.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(load_ground_truth(self.workbook, Layout.load(custom))
                         .documents["UAT-01"].profile, "FATTURA-VENDITA")
        data["metadata"]["profile"] = [{"cell": "Z99"}]
        custom.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "metadata not found"):
            load_ground_truth(self.workbook, Layout.load(custom))

    def test_ground_truth_must_stay_in_ignored_directory(self):
        self.assertEqual(resolve_ground_truth("gt_icea_compilata.xlsx", self.base), self.workbook)
        outside = self.root / "elsewhere.xlsx"
        outside.write_bytes(self.workbook.read_bytes())
        with self.assertRaises(ValueError):
            resolve_ground_truth(outside, self.base)


class PreflightTests(Case):
    def test_clean_sample_and_validation_column_not_blocking(self):
        report = run_preflight(self.gt, self.base)
        self.assertFalse(report["blocking"])
        self.assertEqual(report["samples"][0]["issues"], {})

    def test_hash_missing_pdf_and_unevaluable_rows_are_reported(self):
        rows = [*INVOICE_ROWS, ("R10", "invoice.unknown", "CORE", "1", None, "x", "x", "Presente",
                                "text_normalized"),
                ("R11", "invoice.lines[].net_weight", "CORE", "1", None, "5", 5, "Presente",
                 "numeric_equivalent")]
        base, workbook, _ = environment(self.root / "second", {"UAT-01": rows, "UAT-02": INVOICE_ROWS})
        (base.samples / "synthetic_UAT-02.pdf").unlink()
        report = run_preflight(load_ground_truth(workbook), base)
        first, second = report["samples"]
        self.assertTrue(report["blocking"])
        self.assertEqual(first["blocking"], [])
        self.assertEqual(set(first["issues"]), {"unknown_field_path", "missing_group"})
        self.assertEqual(second["blocking"], ["pdf_missing"])
        # The GT rows of a sample without its PDF are still checked.
        self.assertIn("issues", second)
        (base.samples / "synthetic_UAT-01.pdf").write_bytes(b"%PDF-1.4 altered")
        altered = run_preflight(load_ground_truth(workbook), base, ["UAT-01"])
        self.assertIn("sha256_mismatch", altered["samples"][0]["blocking"])


class UnsupportedProfileTests(unittest.TestCase):
    def test_default_selection_excludes_profiles_outside_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            base, workbook, _ = environment(
                Path(directory), {"UAT-01": INVOICE_ROWS, "UAT-02": INVOICE_ROWS},
                profile={"UAT-01": "FATTURA-VENDITA", "UAT-02": "FATTURA-ACQUISTO-SYN"})
            gt = load_ground_truth(workbook)
            report = run_preflight(gt, base)
            self.assertFalse(report["blocking"])
            self.assertEqual([item["sample_id"] for item in report["samples"]], ["UAT-01"])
            self.assertEqual(report["excluded_samples"][0]["sample_id"], "UAT-02")
            self.assertTrue(run_preflight(gt, base, ["UAT-02"])["blocking"])
            with redirect_stdout(io.StringIO()):
                result = run_compare(gt, base, log=quiet)
            self.assertEqual([sample["sample_id"] for sample in result["data"]["samples"]], ["UAT-01"])
            self.assertEqual(result["summary"]["excluded_samples"][0]["profile"], "FATTURA-ACQUISTO-SYN")


class SettingsTests(unittest.TestCase):
    def test_url_is_transport_agnostic_but_well_formed(self):
        self.assertEqual(check_api_url("https://api.example.test/"), "https://api.example.test")
        self.assertEqual(check_api_url("http://icea-uat-api:8000"), "http://icea-uat-api:8000")
        self.assertTrue(plain_http_remote("http://icea-uat-api:8000"))
        self.assertFalse(plain_http_remote("http://127.0.0.1:8000"))
        self.assertFalse(plain_http_remote("https://api.example.test"))
        for url in ("https://user:pw@api.example.test", "https://x.test/?t=1", "ftp://x"):
            with self.assertRaises(ValueError):
                check_api_url(url)


class RunTests(Case):
    def test_dry_run_is_default_and_shows_budget(self):
        api = FakeApi(self.hashes)
        lines = []
        result = run(self.gt, self.base, ["UAT-01"], client_factory=self.factory(api),
                     log=lines.append)
        self.assertFalse(result["executed"])
        self.assertEqual(api.requests, [])
        self.assertEqual(result["plan"]["api_calls"], 4)
        self.assertIn("4 chiamate API di estrazione", lines[0])

    def test_explicit_samples_and_exact_approval_required(self):
        api = FakeApi(self.hashes)
        with self.assertRaises(RunRefused):
            run(self.gt, self.base, [], log=quiet)
        with self.assertRaises(RunRefused):
            self.execute(api, approve=3)
        self.assertEqual(api.requests, [])

    def test_four_requests_saved_and_not_repeated(self):
        api = FakeApi(self.hashes)
        result = self.execute(api)
        self.assertEqual(len(api.requests), 4)
        self.assertEqual([item["extraction_option"] for item in api.requests], ["A", "B", "C", "D"])
        # API v2: the client never sends model or strategy.
        self.assertTrue(all("model" not in item and "strategy" not in item for item in api.requests))
        self.assertTrue(all(item["auth"] == f"Bearer {TOKEN}" and item["profile"] == "FATTURA-VENDITA"
                            and item["path"] == "/v1/extractions" for item in api.requests))
        self.assertTrue(all(record["status"] == "succeeded" for record in result["records"]))
        record = result["records"][2]
        self.assertEqual((record["extraction_option"], record["pipeline_seconds"]), ("C", 12.0))
        self.assertIsNotNone(record["http_seconds"])
        saved = read_json(Path(load_records(self.base.runs)[0]["_dir"]) / "response.json")
        self.assertEqual(saved["body"]["api_version"], "2.0")
        for path in self.base.runtime.rglob("*.json"):
            self.assertNotIn(TOKEN, path.read_text(encoding="utf-8"))
        again = self.execute(api, approve=0)
        self.assertEqual((len(api.requests), len(again["plan"]["skipped"])), (4, 4))
        self.execute(api, approve=1, methods=["D"], force=True)
        self.assertEqual(len(api.requests), 5)

    def test_http_errors_and_timeouts_are_recorded_without_retry(self):
        def responder(request, fields):
            if fields["extraction_option"] == "A":
                return httpx.Response(429, json={"error": "busy"})
            if fields["extraction_option"] == "B":
                return httpx.Response(502, json={"error": "schema", "run_id": "r"})
            if fields["extraction_option"] == "C":
                raise httpx.ReadTimeout("synthetic timeout", request=request)
            return None

        api = FakeApi(self.hashes, responder)
        records = self.execute(api)["records"]
        self.assertEqual(len(api.requests), 4)
        self.assertEqual([record["error"] for record in records],
                         ["busy", "schema", "timeout", None])
        self.assertIsNone(records[0]["pipeline_seconds"])
        # Failures are not "completed": a later explicit run may call them again.
        retry = self.execute(api, approve=3)
        self.assertEqual(len(retry["plan"]["skipped"]), 1)

    def test_response_identity_mismatch_is_a_failure(self):
        def responder(request, fields):
            return httpx.Response(200, json=response_body(fields, filename=fields["filename"],
                                                          sha256="f" * 64))

        records = self.execute(FakeApi(self.hashes, responder))["records"]
        self.assertTrue(all(record["error"] == "invalid_response:extraction_identity_mismatch"
                            for record in records))

    def test_v1_response_or_exposed_strategy_is_a_failure(self):
        def responder(request, fields):
            body = response_body(fields, filename=fields["filename"], sha256=self.hashes["UAT-01"])
            if fields["extraction_option"] == "A":
                body["api_version"] = "1.0"
            if fields["extraction_option"] == "B":
                body["extraction"]["strategy"] = "vision_direct"
            return httpx.Response(200, json=body)

        records = self.execute(FakeApi(self.hashes, responder))["records"]
        self.assertEqual([record["error"] for record in records],
                         ["invalid_response:api_version_mismatch",
                          "invalid_response:extraction_not_canonical", None, None])


class CompareTests(Case):
    def compare(self):
        with redirect_stdout(io.StringIO()):
            return run_compare(self.gt, self.base, log=quiet)

    def test_scoring_alignment_failures_and_workbook(self):
        issue = {"label": "collisione_cardinalita", "field_path": "invoice.lines[].purchase_order_number",
                 "group_id": "L1", "page": 1, "value_display": "'PO-1'; 'PO-2'",
                 "source_field_indices": [8, 9]}

        def responder(request, fields):
            if fields["extraction_option"] == "D":
                return httpx.Response(502, json={"error": "schema", "run_id": "r"})
            if fields["extraction_option"] == "C":
                return httpx.Response(200, json=response_body(
                    fields, filename=fields["filename"], sha256=self.hashes["UAT-01"],
                    field_issues=[issue]))
            return None

        self.execute(FakeApi(self.hashes, responder))
        result = self.compare()
        sample = result["data"]["samples"][0]
        direct = sample["methods"]["A"]["score"]
        # Extra lot occurrence does not shift LOT-A/LOT-B; absent incoterm is a match.
        self.assertEqual((direct["matched"], direct["omitted"], direct["incorrect"]), (8, 1, 0))
        self.assertEqual(direct["unpaired_occurrences"], 1)
        assisted = sample["methods"]["C"]["score"]
        # Collision with no candidate equal to the GT value: incorrect (no more invalid_field).
        collision = next(row for row in assisted["rows"] if row["row_id"] == "R9")
        self.assertEqual((assisted["invalid_field"], collision["outcome"]), (0, "incorrect"))
        self.assertIsNone(sample["methods"]["D"]["score"])
        outcomes = {row["row_id"]: row["methods"]["D"]["outcome"]
                    for row in sample["rows"] if row["kind"] == "GT"}
        self.assertEqual(set(outcomes.values()), {"errore API: schema"})
        extra = [row for row in sample["rows"] if row["kind"] != "GT"]
        self.assertEqual([row["field_path"] for row in extra], ["invoice.lines[].lot_numbers[]"])
        totals = result["summary"]["totals"]
        self.assertEqual((totals["A"]["evaluable_rows"], totals["D"]["valid_samples"]),
                         (9, 0))
        self.assertEqual(result["summary"]["timing_totals"]["C"]["pipeline"]["mean_seconds"], 12.0)
        path = result["workbook"]
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(sum(name.startswith("xl/charts/chart") for name in archive.namelist()), 3)
        from openpyxl import load_workbook
        sheet = load_workbook(path)["UAT-01"]
        self.assertEqual(sheet["I19"].value, 100.5)
        self.assertEqual(sheet["H19"].number_format, "@")
        self.assertEqual(sheet["R16"].value, "errore API: schema")

    def test_unrun_samples_and_report_history(self):
        first = self.compare()
        self.assertEqual(first["data"]["samples"][0]["methods"]["A"]["error"], "non eseguita")
        self.compare()
        history = list((self.base.reports / "history").glob("before_compare_*.zip"))
        self.assertEqual(len(history), 1)
        lines = (self.base.reports / "ground_truth_history.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)

    def test_formula_like_text_is_stored_as_text(self):
        rows = [("R1", "invoice.number", "CORE", "1", None, "=SUM(1)", "=SUM(1)", "Presente",
                 "identifier_exact")]
        base, workbook, hashes = environment(self.root / "formula", {"UAT-01": rows})
        with patch.object(self, "base", base), patch.object(self, "hashes", hashes):
            gt = load_ground_truth(workbook)
            with redirect_stdout(io.StringIO()):
                result = run_compare(gt, base, log=quiet)
        with zipfile.ZipFile(result["workbook"]) as archive:
            sheets = [archive.read(name).decode() for name in archive.namelist()
                      if name.startswith("xl/worksheets/")]
        self.assertFalse(any("<f>" in sheet for sheet in sheets))


if __name__ == "__main__":
    unittest.main()
