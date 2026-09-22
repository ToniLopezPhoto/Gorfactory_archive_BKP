"""Command-line interface for gorbackup."""

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from gorbackup import __version__
from gorbackup.baseline import BaselineError, adopt_baseline, load_baseline_summary
from gorbackup.backup import BackupError, run_backup
from gorbackup.config import ConfigError, load_config
from gorbackup.dependencies import DependencyError, check_rclone
from gorbackup.ledger import LedgerError, scan_catalogue
from gorbackup.locking import LockError
from gorbackup.planner import PlanError, create_plan
from gorbackup.preflight import PreflightError, run_preflight
from gorbackup.safety import SafetyError
from gorbackup.verification import VerificationError, verify_selected

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
                "run a versioned incremental backup"
                if command == "backup"
                else "verify and adopt an existing first dump"
                if command == "baseline"
                else "inventory catalogue metadata"
                if command == "scan"
                else "generate a non-destructive backup plan"
                if command == "plan"
                else f"{command} operation (placeholder)"
            ),
            description=(
                "Plan and sync the source while preserving displaced files in history."
                if command == "backup"
                else "Verify an existing archive copy and record a trusted baseline."
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
        if command == "backup":
            subparser.add_argument(
                "--override-safety",
                action="store_true",
                help="manually accept overridable volume anomalies (interactive terminal only)",
            )
        if command == "verify":
            subparser.add_argument(
                "paths", nargs="+", metavar="PATH",
                help="relative source file or directory (directories are recursive)",
            )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        rclone = check_rclone()
        preflight = None
        if args.command in {"backup", "baseline", "scan", "plan", "verify"}:
            baseline = (
                load_baseline_summary(config)
                if args.command in {"scan", "plan"}
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
        if args.command == "backup":
            manual_context = bool(sys.stdin.isatty() and sys.stdout.isatty())
            if args.override_safety and not manual_context:
                raise BackupError(
                    "--override-safety is manual-only and requires an interactive terminal"
                )
            backup_result = run_backup(
                config, rclone, override_safety=args.override_safety,
                manual_context=manual_context,
            )
        if args.command == "scan":
            scan_result = scan_catalogue(config)
        if args.command == "plan":
            plan_result = create_plan(config, rclone)
        if args.command == "verify":
            verification_result = verify_selected(
                config.source.path,
                config.archive.root / config.archive.current_dir,
                args.paths,
            )
    except (
        ConfigError,
        DependencyError,
        PreflightError,
        BaselineError,
        LedgerError,
        PlanError,
        BackupError,
        SafetyError,
        LockError,
        VerificationError,
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

    if args.command == "backup":
        print(
            f"backup: {backup_result.status}; run_id={backup_result.run_id}; "
            f"plan_run_id={backup_result.plan_run_id}; "
            f"executed_transfer_files={backup_result.executed_transfer_files}; "
            f"executed_transfer_bytes={backup_result.executed_transfer_bytes}; "
            f"executed_archive_files={backup_result.executed_archive_files}; "
            f"executed_archive_bytes={backup_result.executed_archive_bytes}; "
            f"verified_files={backup_result.verification.verified_files}; "
            f"verified_bytes={backup_result.verification.verified_bytes}; "
            f"verification_failures={backup_result.verification.failures}; "
            f"delete_count={backup_result.safety.delete_count}; "
            f"delete_bytes={backup_result.safety.delete_bytes}; "
            f"projected_free_percent="
            f"{backup_result.safety.free_percent_after:.1f}; "
            f"history={backup_result.history_path}; "
            f"manifest={backup_result.manifest_path}"
        )
        return 0

    if args.command == "scan":
        print(
            f"scan: success; run_id={scan_result.run_id}; "
            f"catalogue_files={scan_result.catalogue_file_count}; "
            f"catalogue_bytes={scan_result.catalogue_total_bytes}; "
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
            f"catalogue_files={plan_result.catalogue_file_count}; "
            f"catalogue_bytes={plan_result.catalogue_total_bytes}; "
            f"planned_transfer_files={plan_result.planned_transfer_files}; "
            f"planned_transfer_bytes={plan_result.planned_transfer_bytes}; "
            f"planned_archive_files={plan_result.planned_archive_files}; "
            f"planned_archive_bytes={plan_result.planned_archive_bytes}; "
            f"rename_candidates={plan_result.counts['rename_move_candidate']}; "
            f"skipped_recent={plan_result.counts['skipped_recent']}; "
            f"errors={plan_result.counts['error']}; manifest={plan_result.manifest_path}"
        )
        return 0 if plan_result.status == "success" else 2

    if args.command == "verify":
        for item in verification_result.items:
            print(
                f"{item.status}: {item.path}; method={item.method}; "
                f"bytes_verified={item.bytes_verified}; detail={item.detail}"
            )
        print(
            f"verify: {'success' if not verification_result.failures else 'failed'}; "
            f"verified_files={verification_result.verified_files}; "
            f"verified_bytes={verification_result.verified_bytes}; "
            f"failures={verification_result.failures}"
        )
        return 0 if not verification_result.failures else 2

    version = ".".join(str(part) for part in rclone.version)
    print(
        f"{args.command}: prerequisites valid (rclone {version}); "
        "operation is not implemented yet."
    )
    return 0
