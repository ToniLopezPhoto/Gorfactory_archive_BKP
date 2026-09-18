"""Command-line interface for gorbackup."""

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from gorbackup import __version__
from gorbackup.baseline import BaselineError, adopt_baseline, load_baseline_summary
from gorbackup.config import ConfigError, load_config
from gorbackup.dependencies import DependencyError, check_rclone
from gorbackup.ledger import LedgerError, scan_catalogue
from gorbackup.planner import PlanError, create_plan
from gorbackup.preflight import PreflightError, run_preflight

COMMANDS = (
    "backup",
    "baseline",
    "scan",
    "plan",
    "status",
    "verify",
    "restore",
    "audit",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gorbackup",
        description="Safe archive backup command line interface.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("config/config.yaml"),
        help="configuration file (default: config/config.yaml)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in COMMANDS:
        subparser = subparsers.add_parser(
            command,
            help=(
                "verify and adopt an existing first dump"
                if command == "baseline"
                else "inventory catalogue metadata"
                if command == "scan"
                else "generate a non-destructive backup plan"
                if command == "plan"
                else f"{command} operation (placeholder)"
            ),
            description=(
                "Verify an existing archive copy and record a trusted baseline."
                if command == "baseline"
                else "Scan source metadata and update the SQLite inventory."
                if command == "scan"
                else "Run rclone in dry-run mode and persist a classified plan."
                if command == "plan"
                else f"Validate prerequisites for the future {command} operation."
            ),
        )
        if command == "baseline":
            subparser.add_argument(
                "--reconcile",
                action="store_true",
                help="copy missing or mismatched files, without deleting destination extras",
            )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        rclone = check_rclone()
        preflight = None
        if args.command in {"backup", "baseline", "scan", "plan"}:
            baseline = (
                load_baseline_summary(config)
                if args.command in {"backup", "scan", "plan"}
                else None
            )
            preflight = run_preflight(config, baseline=baseline)
        if args.command == "baseline":
            result = adopt_baseline(
                config,
                rclone,
                preflight,
                reconcile=args.reconcile,
            )
        if args.command == "scan":
            scan_result = scan_catalogue(config)
        if args.command == "plan":
            plan_result = create_plan(config, rclone)
    except (
        ConfigError,
        DependencyError,
        PreflightError,
        BaselineError,
        LedgerError,
        PlanError,
    ) as exc:
        print(f"gorbackup: error: {exc}", file=sys.stderr)
        return 2

    if args.command == "baseline":
        extras = len(result.comparison.destination_extras)
        action = "reconciled and adopted" if result.reconciled else "verified and adopted"
        print(
            f"baseline: {action}; manifest={result.manifest_path}; "
            f"destination_extras={extras}"
        )
        return 0

    if args.command == "scan":
        print(
            f"scan: success; run_id={scan_result.run_id}; "
            f"files={scan_result.file_count}; bytes={scan_result.total_bytes}; "
            f"changed={scan_result.changed_files}; deleted={scan_result.deleted_paths}"
        )
        return 0

    if args.command == "plan":
        transfer_files = sum(
            plan_result.counts[name]
            for name in ("new_file", "changed_file", "rename_move_candidate")
        )
        leaving_files = sum(
            plan_result.counts[name]
            for name in (
                "delete_from_current",
                "changed_file",
                "rename_move_candidate",
            )
        )
        print(
            f"plan: {plan_result.status}; run_id={plan_result.run_id}; "
            f"transfer_files={transfer_files}; "
            f"transfer_bytes={plan_result.transfer_bytes}; "
            f"leave_current_files={leaving_files}; "
            f"leave_current_bytes={plan_result.leaving_current_bytes}; "
            f"rename_candidates={plan_result.counts['rename_move_candidate']}; "
            f"skipped_recent={plan_result.counts['skipped_recent']}; "
            f"errors={plan_result.counts['error']}; manifest={plan_result.manifest_path}"
        )
        return 0 if plan_result.status == "success" else 2

    version = ".".join(str(part) for part in rclone.version)
    print(
        f"{args.command}: prerequisites valid (rclone {version}); "
        "operation is not implemented yet."
    )
    return 0
