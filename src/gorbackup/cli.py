"""Command-line interface for gorbackup."""

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from gorbackup import __version__
from gorbackup.config import ConfigError, load_config
from gorbackup.dependencies import DependencyError, check_rclone
from gorbackup.preflight import PreflightError, run_preflight

COMMANDS = ("backup", "plan", "status", "verify", "restore", "audit")


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
        subparsers.add_parser(
            command,
            help=f"{command} operation (placeholder)",
            description=f"Validate prerequisites for the future {command} operation.",
        )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        rclone = check_rclone()
        if args.command == "backup":
            run_preflight(config)
    except (ConfigError, DependencyError, PreflightError) as exc:
        print(f"gorbackup: error: {exc}", file=sys.stderr)
        return 2

    version = ".".join(str(part) for part in rclone.version)
    print(
        f"{args.command}: prerequisites valid (rclone {version}); "
        "operation is not implemented yet."
    )
    return 0
