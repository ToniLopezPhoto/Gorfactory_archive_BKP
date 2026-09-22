"""Ledger-backed history discovery and non-destructive restores."""

import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Optional, Sequence, Tuple

from gorbackup.config import AppConfig
from gorbackup.ledger import Ledger, validate_state_location
from gorbackup.verification import VerificationError, hash_file, validate_relative_path


RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
COPY_CHUNK_SIZE = 4 * 1024 * 1024


class RecoveryError(RuntimeError):
    """Raised when history discovery or restore cannot be performed safely."""


@dataclass(frozen=True)
class HistoryVersion:
    location: str
    run_id: Optional[str]
    timestamp: Optional[str]
    run_status: Optional[str]
    size: Optional[int]
    checksum: Optional[str]
    reason: str
    path: Path
    exists: bool


@dataclass(frozen=True)
class HistoryResult:
    relative_path: str
    current: Optional[HistoryVersion]
    versions: Tuple[HistoryVersion, ...]


@dataclass(frozen=True)
class RestoreResult:
    restore_id: str
    relative_path: str
    source_run_id: str
    source_path: Path
    destination_path: Path
    bytes_copied: int
    checksum: str
    verification_method: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def validate_run_id(value: str) -> str:
    if not isinstance(value, str) or not RUN_ID_PATTERN.fullmatch(value):
        raise RecoveryError(f"unsafe run id: {value!r}")
    return value


def _relative(value: str) -> str:
    try:
        return validate_relative_path(value)
    except VerificationError as exc:
        raise RecoveryError(str(exc)) from exc


def _contained_path(root: Path, relative_path: str, *, must_exist: bool) -> Path:
    root = root.resolve()
    candidate = root.joinpath(*PurePosixPath(relative_path).parts)
    try:
        resolved = candidate.resolve(strict=must_exist)
    except OSError as exc:
        raise RecoveryError(f"cannot resolve {relative_path}: {exc}") from exc
    if resolved != root and root not in resolved.parents:
        raise RecoveryError(f"path escapes configured root: {relative_path}")
    return resolved


def _archive_child_root(config: AppConfig, configured: str) -> Path:
    relative = _relative(configured)
    archive = config.archive.root.resolve()
    root = archive.joinpath(*PurePosixPath(relative).parts).resolve(strict=False)
    if not _is_within(root, archive):
        raise RecoveryError(f"configured archive path escapes archive root: {configured}")
    return root


def _reason(classification: str, plan_category: Optional[str]) -> str:
    if plan_category == "changed_file":
        return "overwritten"
    if plan_category == "delete_from_current":
        return "deleted"
    if plan_category == "rename_move_candidate":
        return "moved/versioned"
    return "moved/versioned" if classification == "versioned" else classification


def find_history(config: AppConfig, requested_path: str) -> HistoryResult:
    """Read history from SQLite evidence and confirm each path on disk."""
    relative = _relative(requested_path)
    current_root = _archive_child_root(config, config.archive.current_dir)
    history_root = _archive_child_root(config, config.archive.history_dir)
    with Ledger(config.ledger_path, read_only=True) as ledger:
        current_evidence = ledger.current_version(relative)
        rows = ledger.historical_versions(relative)

    current_path = _contained_path(current_root, relative, must_exist=False)
    current = None
    if current_path.exists():
        if not current_path.is_file():
            raise RecoveryError(f"current path is not a regular file: {relative}")
        stat_result = current_path.stat()
        current = HistoryVersion(
            "current", None, None, None, stat_result.st_size,
            current_evidence.get("checksum") if current_evidence else None,
            "current", current_path, True,
        )

    versions = []
    for row in rows:
        run_id = validate_run_id(str(row["run_id"]))
        run_root = _contained_path(history_root, run_id, must_exist=False)
        historical_path = _contained_path(run_root, relative, must_exist=False)
        exists = historical_path.is_file()
        versions.append(HistoryVersion(
            "history", run_id, row["completed_at"] or row["started_at"],
            str(row["status"]), int(row["size"]), row["checksum"],
            _reason(str(row["classification"]), row["plan_category"]),
            historical_path, exists,
        ))
    if current is None and not versions:
        raise RecoveryError(f"no current or historical version recorded for: {relative}")
    return HistoryResult(relative, current, tuple(versions))


def _is_within(candidate: Path, root: Path) -> bool:
    return candidate == root or root in candidate.parents


def _destination_path(config: AppConfig, destination: Optional[Path],
                      restore_id: str, relative: str) -> Path:
    archive = config.archive.root.resolve()
    source = config.source.path.resolve()
    current = _archive_child_root(config, config.archive.current_dir)
    history = _archive_child_root(config, config.archive.history_dir)
    state = config.state_root.resolve()
    manifests = config.manifests_root.resolve()
    recovery = (archive / "recovery").resolve()
    if not _is_within(recovery, archive):
        raise RecoveryError("configured recovery path escapes archive root")

    if destination is None:
        root = recovery / restore_id
    else:
        if not destination.is_absolute():
            raise RecoveryError("explicit destination must be an absolute directory")
        root = destination.resolve(strict=False)
        if _is_within(root, archive):
            raise RecoveryError("explicit destination must be outside the archive")
    output = root.joinpath(*PurePosixPath(relative).parts).resolve(strict=False)
    if not _is_within(output, root):
        raise RecoveryError("destination path escapes its recovery root")
    for protected, label in (
        (source, "source"), (current, "current"), (history, "history"),
        (state, "state"), (manifests, "manifests"),
    ):
        if _is_within(output, protected):
            raise RecoveryError(f"destination must not be inside {label}: {output}")
    return output


def _copy_stream(source: Path, destination: Path, *,
                 chunk_size: int = COPY_CHUNK_SIZE) -> int:
    total = 0
    with source.open("rb") as reader, destination.open("xb") as writer:
        while True:
            chunk = reader.read(chunk_size)
            if not chunk:
                break
            writer.write(chunk)
            total += len(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    return total


def restore_version(
    config: AppConfig, requested_path: str, source_run_id: str, *,
    destination: Optional[Path] = None,
    now: Callable[[], datetime] = _now,
    restore_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    copier: Callable[[Path, Path], int] = _copy_stream,
    hasher: Callable[[Path], Tuple[str, int]] = hash_file,
) -> RestoreResult:
    """Copy one completed historical version into verified recovery staging."""
    validate_state_location(config)
    relative = _relative(requested_path)
    run_id = validate_run_id(source_run_id)
    restore_id = validate_run_id(restore_id_factory())
    # Restore is a mutating command and may safely apply the additive schema
    # migration before its read-only discovery phase.
    with Ledger(config.ledger_path):
        pass
    result = find_history(config, relative)
    matches = [item for item in result.versions if item.run_id == run_id]
    if len(matches) != 1:
        raise RecoveryError(
            f"run {run_id} does not contain one unambiguous historical version of {relative}"
        )
    version = matches[0]
    if version.run_status not in {"success", "warning"}:
        raise RecoveryError(
            f"run {run_id} is not eligible for restore (status={version.run_status})"
        )
    if not version.exists:
        raise RecoveryError(f"historical version is recorded but missing: {version.path}")
    source_stat = version.path.stat()
    if not version.path.is_file() or source_stat.st_size != version.size:
        raise RecoveryError(f"historical version size does not match ledger: {version.path}")
    output = _destination_path(config, destination, restore_id, relative)
    if output == version.path:
        raise RecoveryError("destination must not be the historical source")
    if output.exists():
        raise RecoveryError(f"destination already exists; refusing to overwrite: {output}")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.gorbackup-restore-{restore_id}.tmp")
    if temporary.exists():
        raise RecoveryError(f"restore temporary already exists: {temporary}")
    started_at = now().isoformat()
    with Ledger(config.ledger_path) as ledger:
        ledger.start_restore(restore_id, started_at, relative, run_id,
                             str(version.path), str(output))
    bytes_copied = 0
    checksum = None
    try:
        bytes_copied = copier(version.path, temporary)
        source_checksum, source_bytes = hasher(version.path)
        restored_checksum, restored_bytes = hasher(temporary)
        known = version.checksum
        if known:
            method, separator, expected_checksum = known.partition(":")
            if separator and method.lower() == "sha256" and source_checksum != expected_checksum:
                raise RecoveryError("historical source does not match its persisted SHA-256 checksum")
        if (source_bytes != restored_bytes or source_checksum != restored_checksum
                or restored_bytes != source_stat.st_size):
            raise RecoveryError("post-restore size or SHA-256 verification failed")
        try:
            os.link(temporary, output)
        except FileExistsError as exc:
            raise RecoveryError(f"destination appeared during restore: {output}") from exc
        temporary.unlink()
        checksum = source_checksum
        with Ledger(config.ledger_path) as ledger:
            ledger.finish_restore(
                restore_id, now().isoformat(), status="success",
                bytes_copied=restored_bytes, verification_method="sha256",
                checksum=f"sha256:{source_checksum}",
            )
        return RestoreResult(
            restore_id, relative, run_id, version.path, output,
            restored_bytes, source_checksum, "sha256",
        )
    except Exception as exc:
        detail = f"{exc}; destination={output}; temporary={temporary}"
        with Ledger(config.ledger_path) as ledger:
            ledger.finish_restore(
                restore_id, now().isoformat(), status="failed",
                bytes_copied=bytes_copied, verification_method="sha256",
                checksum=f"sha256:{checksum}" if checksum else None,
                error=detail,
            )
        raise RecoveryError(f"restore failed for {relative}: {detail}") from exc
