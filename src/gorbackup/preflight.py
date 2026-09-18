"""Read-only safety checks performed before any backup operation."""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional

from gorbackup.config import AppConfig

GIB = 1024 ** 3


class PreflightError(RuntimeError):
    """Raised when one or more safety checks fail."""

    def __init__(self, reasons: List[str]) -> None:
        self.reasons = tuple(reasons)
        super().__init__("preflight failed: " + "; ".join(reasons))


@dataclass(frozen=True)
class SourceSummary:
    file_count: int
    total_size_bytes: int


@dataclass(frozen=True)
class PreflightResult:
    source: SourceSummary
    destination_free_percent: float


def _check_marker(root: Path, filename: str, expected: str, label: str) -> Optional[str]:
    marker = root / filename
    try:
        actual = marker.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return f"{label} identity marker is missing: {marker}"
    except OSError as exc:
        return f"cannot read {label} identity marker {marker}: {exc}"
    if actual != expected:
        return f"{label} identity mismatch at {marker}"
    return None


def summarize_source(source: Path, marker_file: str) -> SourceSummary:
    """Return a read-only recursive summary, excluding the identity marker."""
    files = 0
    size = 0
    marker = source / marker_file
    walk_errors: List[OSError] = []
    for directory, _, filenames in os.walk(source, onerror=walk_errors.append):
        for filename in filenames:
            path = Path(directory) / filename
            if path == marker:
                continue
            try:
                stat_result = path.lstat()
            except OSError as exc:
                raise PreflightError([f"cannot inspect source file {path}: {exc}"]) from exc
            files += 1
            size += stat_result.st_size
    if walk_errors:
        error = walk_errors[0]
        raise PreflightError([f"cannot scan source {source}: {error}"])
    return SourceSummary(files, size)


def run_preflight(
    config: AppConfig,
    *,
    baseline: Optional[SourceSummary] = None,
    is_mount: Callable[[Path], bool] = os.path.ismount,
    access: Callable[[Path, int], bool] = os.access,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
    same_filesystem: Optional[Callable[[Path, Path], bool]] = None,
) -> PreflightResult:
    """Validate identities and storage state without mutating either root."""
    source = config.source.path
    source_mount = config.source.mount_path
    archive = config.archive.root
    reasons: List[str] = []

    if not source_mount.is_dir() or not is_mount(source_mount):
        reasons.append(f"source mount is unavailable or not mounted: {source_mount}")
    if not source.is_dir():
        reasons.append(f"source directory is unavailable: {source}")
    elif not access(source, os.R_OK | os.X_OK):
        reasons.append(f"source directory is not readable: {source}")

    if not archive.is_dir() or not is_mount(archive):
        reasons.append(f"archive root is unavailable or not mounted: {archive}")
    elif not access(archive, os.W_OK | os.X_OK):
        reasons.append(f"archive root is not writable: {archive}")

    if source.is_dir():
        marker_error = _check_marker(
            source, config.source.marker_file, config.source.marker_id, "source"
        )
        if marker_error:
            reasons.append(marker_error)
    if archive.is_dir():
        marker_error = _check_marker(
            archive, config.archive.marker_file, config.archive.marker_id, "archive"
        )
        if marker_error:
            reasons.append(marker_error)

    try:
        source_resolved = source.resolve(strict=True)
        archive_resolved = archive.resolve(strict=True)
        same_device = (
            same_filesystem(source_resolved, archive_resolved)
            if same_filesystem is not None
            else source_resolved.stat().st_dev == archive_resolved.stat().st_dev
        )
        if (
            source_resolved == archive_resolved
            or source_resolved in archive_resolved.parents
            or archive_resolved in source_resolved.parents
            or same_device
        ):
            reasons.append("source and archive resolve to the same path or filesystem")
    except OSError:
        pass  # Missing/inaccessible roots already produce a more specific reason.

    for name in (config.archive.current_dir, config.archive.history_dir):
        expected = archive / name
        if not expected.is_dir():
            reasons.append(f"required archive directory is missing: {expected}")

    free_percent = 0.0
    if archive.is_dir():
        try:
            usage = disk_usage(archive)
            free_percent = (usage.free / usage.total * 100) if usage.total else 0.0
            if free_percent < config.safety.min_free_space_percent:
                reasons.append(
                    "archive free space "
                    f"({free_percent:.1f}%) is below the configured minimum "
                    f"({config.safety.min_free_space_percent:.1f}%)"
                )
        except OSError as exc:
            reasons.append(f"cannot determine archive free space: {exc}")

    summary = SourceSummary(0, 0)
    if source.is_dir() and access(source, os.R_OK | os.X_OK):
        try:
            summary = summarize_source(source, config.source.marker_file)
        except PreflightError as exc:
            reasons.extend(exc.reasons)
        if summary.file_count == 0:
            reasons.append("source contains no data files")
        minimum_bytes = config.safety.min_source_size_gb * GIB
        if summary.total_size_bytes < minimum_bytes:
            reasons.append(
                f"source size ({summary.total_size_bytes} bytes) is below the configured "
                f"minimum ({int(minimum_bytes)} bytes)"
            )
        if baseline is not None:
            ratio = config.safety.min_source_size_ratio
            if (
                summary.file_count < baseline.file_count * ratio
                or summary.total_size_bytes < baseline.total_size_bytes * ratio
            ):
                reasons.append(
                    "source is implausibly smaller than the last known-good manifest "
                    f"(minimum ratio {ratio:.2f})"
                )

    if reasons:
        raise PreflightError(reasons)
    return PreflightResult(summary, free_percent)
