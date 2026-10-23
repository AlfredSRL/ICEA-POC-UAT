"""Command line: preflight | run | compare | all | check-api."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import METHODS, paths, resolve_ground_truth
from .ground_truth_uat import Layout, load_ground_truth
from .preflight import format_report, run_preflight
from .runner import RunRefused, save_json


def _common(parser: argparse.ArgumentParser, samples_required: bool = False) -> None:
    parser.add_argument("--ground-truth", required=True,
                        help="ICEA-approved workbook inside data/uat/ground_truth/ (any filename)")
    parser.add_argument("--layout", type=Path, help="GT layout JSON (default: layouts/default.json)")
    parser.add_argument("--samples", nargs="+", required=samples_required, metavar="SAMPLE_ID",
                        help="explicit sample IDs (sheet names)")


def _run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--methods", nargs="+", choices=METHODS, default=list(METHODS),
                        help="opaque server extraction options")
    parser.add_argument("--execute", action="store_true",
                        help="perform real API calls (default: dry-run)")
    parser.add_argument("--approve-calls", type=int,
                        help="must equal the planned number of API calls")
    parser.add_argument("--force", action="store_true",
                        help="call again combinations already completed with the same configuration")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="icea_client", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    _common(commands.add_parser("preflight", help="offline PDF and GT checks"))
    run = commands.add_parser("run", help="API calls for explicit samples (dry-run by default)")
    _common(run, samples_required=True)
    _run_args(run)
    compare = commands.add_parser("compare", help="offline scoring, reports and Excel")
    _common(compare)
    compare.add_argument("--no-workbook", action="store_true")
    everything = commands.add_parser("all", help="preflight + run + compare")
    _common(everything, samples_required=True)
    _run_args(everything)
    commands.add_parser("check-api", help="GET /openapi.json with the configured token")
    return parser


def _preflight(args, base, gt) -> dict:
    report = run_preflight(gt, base, args.samples)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    save_json(base.preflight / f"preflight_{stamp}.json", report)
    print(format_report(report))
    return report


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "check-api":
        from .api_client import ApiClient
        from .config import api_settings

        with ApiClient(api_settings()) as client:
            schema = client.openapi()
        print(json.dumps({"title": schema.get("info", {}).get("title"),
                          "version": schema.get("info", {}).get("version"),
                          "paths": sorted(schema.get("paths", {}))}, indent=2))
        return 0
    base = paths()
    try:
        gt = load_ground_truth(resolve_ground_truth(args.ground_truth, base), Layout.load(args.layout))
    except (OSError, ValueError, KeyError) as error:
        print(f"GT non leggibile: {error}", file=sys.stderr)
        return 1
    try:
        if args.command == "preflight":
            return 1 if _preflight(args, base, gt)["blocking"] else 0
        if args.command == "compare":
            from .compare import run_compare
            run_compare(gt, base, args.samples, workbook=not args.no_workbook)
            return 0
        from .runner import run
        if args.command == "all" and _preflight(args, base, gt)["blocking"]:
            return 1
        result = run(gt, base, args.samples, methods=args.methods, execute=args.execute,
                     approved_calls=args.approve_calls, force=args.force)
        if args.command == "all":
            from .compare import run_compare
            run_compare(gt, base, args.samples)
        failed = [record for record in result["records"] if record["status"] != "succeeded"]
        return 2 if failed else 0
    except RunRefused as error:
        print(f"RIFIUTATO: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
