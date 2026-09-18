"""Adopt an existing archive copy as a verified baseline."""

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Protocol, Sequence, Tuple

from gorbackup.config import AppConfig
from gorbackup.dependencies import RcloneInfo
from gorbackup.preflight import PreflightResult


class BaselineError(RuntimeError):
    """Raised when a baseline cannot be safely adopted."""


class BaselineDifferencesError(BaselineError):
    """Raised when source differences require reconciliation."""


@dataclass(frozen=True)
class Difference:
    path: str
    reason: str


@dataclass(frozen=True)
class ComparisonReport:
    matched: int
    missing_destination: Tuple[Difference, ...]
    destination_extras: Tuple[Difference, ...]
    mismatched: Tuple[Difference, ...]
    errors: Tuple[Difference, ...]

    @property
    def requires_reconciliation(self) -> bool:
        return bool(self.missing_destination or self.mismatched or self.errors)

    @property
    def has_differences(self) -> bool:
        return bool(
            self.missing_destination
            or self.destination_extras
            or self.mismatched
            or self.errors
        )


@dataclass(frozen=True)
class BaselineResult:
    manifest_path: Path
    report_path: Path
    comparison: ComparisonReport
    reconciled: bool


class DiskUsage(Protocol):
    total: int
    free: int


def _ensure_state_is_outside_source(config: AppConfig) -> None:
    source = config.source.path.resolve()
    state = config.state.directory.resolve()
    if state == source or source in state.parents:
        raise BaselineError(
            f"state directory must not be inside the read-only source: {state}"
        )


def load_baseline_summary(config: AppConfig) -> Optional["SourceSummary"]:
    """Load the known-good summary used by later preflight comparisons."""
    from gorbackup.preflight import SourceSummary

    path = config.state.directory / config.state.baseline_manifest
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        source = payload["source"]
        if payload["status"] != "known-good":
            raise ValueError("status is not known-good")
        file_count = source["file_count"]
        total_size = source["total_size_bytes"]
        if (
            isinstance(file_count, bool)
            or not isinstance(file_count, int)
            or file_count < 0
            or isinstance(total_size, bool)
            or not isinstance(total_size, int)
            or total_size < 0
        ):
            raise ValueError("summary values are invalid")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BaselineError(f"invalid baseline manifest {path}: {exc}") from exc
    return SourceSummary(file_count, total_size)


def parse_combined_report(lines: Sequence[str]) -> ComparisonReport:
    """Parse rclone's combined logger format into explicit path/reason records."""
    matched = 0
    grouped: Dict[str, List[Difference]] = {symbol: [] for symbol in "+-*!"}
    reasons = {
        "+": "missing on destination",
        "-": "extra on destination",
        "*": "content mismatch",
        "!": "read or hash error",
    }
    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if not line:
            continue
        if len(line) < 3 or line[1] != " " or line[0] not in "=+-*!":
            grouped["!"].append(Difference(line, "unrecognized rclone report line"))
            continue
        symbol, path = line[0], line[2:]
        if symbol == "=":
            matched += 1
        else:
            grouped[symbol].append(Difference(path, reasons[symbol]))
    return ComparisonReport(
        matched=matched,
        missing_destination=tuple(grouped["+"]),
        destination_extras=tuple(grouped["-"]),
        mismatched=tuple(grouped["*"]),
        errors=tuple(grouped["!"]),
    )


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _comparison_payload(report: ComparisonReport) -> Dict[str, object]:
    return {
        "matched": report.matched,
        "missing_destination": [asdict(item) for item in report.missing_destination],
        "destination_extras": [asdict(item) for item in report.destination_extras],
        "mismatched": [asdict(item) for item in report.mismatched],
        "errors": [asdict(item) for item in report.errors],
    }


def compare_with_rclone(
    rclone: RcloneInfo,
    config: AppConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> ComparisonReport:
    """Run a non-destructive rclone check and return its structured report."""
    _ensure_state_is_outside_source(config)
    config.state.directory.mkdir(parents=True, exist_ok=True)
    descriptor, report_name = tempfile.mkstemp(
        prefix=".rclone-check-", suffix=".txt", dir=str(config.state.directory)
    )
    os.close(descriptor)
    report_path = Path(report_name)
    command = [
        rclone.executable,
        "check",
        str(config.source.path),
        str(config.archive.root / config.archive.current_dir),
        "--combined",
        str(report_path),
        "--exclude",
        f"/{config.source.marker_file}",
        "--retries",
        "1",
    ]
    try:
        completed = runner(command, check=False, capture_output=True, text=True)
        lines = report_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BaselineError(f"could not run baseline comparison: {exc}") from exc
    finally:
        if report_path.exists():
            report_path.unlink()
    report = parse_combined_report(lines)
    if completed.returncode not in (0, 1):
        detail = (completed.stderr or completed.stdout).strip()
        raise BaselineError(
            f"rclone check failed with exit code {completed.returncode}: {detail}"
        )
    if completed.returncode == 1 and not report.has_differences:
        detail = (completed.stderr or completed.stdout).strip()
        raise BaselineError(f"rclone check failed without a usable report: {detail}")
    return report


def reconcile_with_rclone(
    rclone: RcloneInfo,
    config: AppConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> None:
    """Copy missing/changed source files without deleting destination extras."""
    command = [
        rclone.executable,
        "copy",
        str(config.source.path),
        str(config.archive.root / config.archive.current_dir),
        "--check-first",
        "--metadata",
        "--exclude",
        f"/{config.source.marker_file}",
    ]
    try:
        completed = runner(command, check=False, capture_output=True, text=True)
    except OSError as exc:
        raise BaselineError(f"could not run baseline reconciliation: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise BaselineError(
            f"rclone copy failed with exit code {completed.returncode}: {detail}"
        )


def adopt_baseline(
    config: AppConfig,
    rclone: RcloneInfo,
    preflight: PreflightResult,
    *,
    reconcile: bool = False,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    disk_usage: Callable[[Path], DiskUsage] = shutil.disk_usage,
) -> BaselineResult:
    """Verify, optionally reconcile, and atomically persist a trusted baseline."""
    started_at = now()
    comparison = compare_with_rclone(rclone, config, runner=runner)
    report_path = config.state.directory / config.state.baseline_report
    report_payload: Dict[str, object] = {
        "schema_version": 1,
        "checked_at": started_at.isoformat(),
        "comparison": _comparison_payload(comparison),
        "reconciliation_requested": reconcile,
    }
    _atomic_json(report_path, report_payload)

    if comparison.errors:
        raise BaselineError(f"baseline comparison reported {len(comparison.errors)} errors")
    if (comparison.missing_destination or comparison.mismatched) and not reconcile:
        raise BaselineDifferencesError(
            "baseline has differences requiring reconciliation: "
            f"{len(comparison.missing_destination)} missing, "
            f"{len(comparison.mismatched)} mismatched; see {report_path}"
        )

    reconciled = False
    if comparison.missing_destination or comparison.mismatched:
        reconcile_with_rclone(rclone, config, runner=runner)
        reconciled = True
        comparison = compare_with_rclone(rclone, config, runner=runner)
        report_payload.update(
            {
                "reconciled": True,
                "verified_at": now().isoformat(),
                "comparison": _comparison_payload(comparison),
            }
        )
        _atomic_json(report_path, report_payload)
        if comparison.requires_reconciliation:
            raise BaselineError(
                "baseline still differs after reconciliation; "
                f"see {report_path}"
            )

    completed_at = now()
    try:
        usage = disk_usage(config.archive.root)
        free_bytes = usage.free
        total_bytes = usage.total
        final_free_percent = free_bytes / total_bytes * 100 if total_bytes else 0.0
    except OSError as exc:
        raise BaselineError(f"cannot record final archive free space: {exc}") from exc
    manifest_path = config.state.directory / config.state.baseline_manifest
    manifest = {
        "schema_version": 1,
        "status": "known-good",
        "created_at": completed_at.isoformat(),
        "source": {
            "path": str(config.source.path),
            "marker_id": config.source.marker_id,
            "file_count": preflight.source.file_count,
            "total_size_bytes": preflight.source.total_size_bytes,
        },
        "archive": {
            "path": str(config.archive.root / config.archive.current_dir),
            "marker_id": config.archive.marker_id,
            "free_bytes": free_bytes,
            "free_percent": final_free_percent,
        },
        "verification": _comparison_payload(comparison),
        "reconciled": reconciled,
    }
    _atomic_json(manifest_path, manifest)

    run_id = completed_at.strftime("%Y%m%dT%H%M%S.%fZ")
    run_path = config.state.directory / "runs" / f"baseline-{run_id}.json"
    _atomic_json(
        run_path,
        {
            "schema_version": 1,
            "operation": "baseline",
            "status": "success",
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "manifest": str(manifest_path),
            "report": str(report_path),
            "reconciled": reconciled,
        },
    )
    return BaselineResult(manifest_path, report_path, comparison, reconciled)
