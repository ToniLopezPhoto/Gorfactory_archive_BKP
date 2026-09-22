"""Ledger-driven retention planning and explicit history pruning."""

import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from gorbackup.config import AppConfig
from gorbackup.ledger import Ledger, LedgerError, _atomic_json, validate_state_location
from gorbackup.locking import BackupLock, HistoryLock
from gorbackup.recovery import RUN_ID_PATTERN
from gorbackup.verification import VerificationError, validate_relative_path


class PruneError(RuntimeError):
    """Raised when a prune cannot be planned or executed safely."""


class MissingHistoryError(PruneError):
    pass


@dataclass(frozen=True)
class PruneResult:
    prune_id: str
    status: str
    files: int
    bytes: int
    runs: int
    manifest_path: Path
    protected_count: int = 0
    oldest_candidate: Optional[str] = None
    newest_candidate: Optional[str] = None
    free_before: Optional[Dict[str, float]] = None
    free_projected: Optional[Dict[str, float]] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_relative(value: str) -> str:
    try:
        return validate_relative_path(value)
    except VerificationError as exc:
        raise PruneError(str(exc)) from exc


def _history_root(config: AppConfig) -> Path:
    configured = _safe_relative(config.archive.history_dir)
    archive = config.archive.root.resolve(strict=True)
    root = archive.joinpath(*PurePosixPath(configured).parts)
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise PruneError(f"history root is unavailable: {root}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PruneError(f"history root must be a real directory: {root}")
    if root.resolve(strict=True).parent != archive and archive not in root.resolve(strict=True).parents:
        raise PruneError("history root escapes archive root")
    return root


def _validate_archive_identity(config: AppConfig) -> None:
    marker = config.archive.root / config.archive.marker_file
    try:
        metadata = marker.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise PruneError(f"archive identity marker is not a regular file: {marker}")
        actual = marker.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PruneError(f"cannot read archive identity marker {marker}: {exc}") from exc
    if actual != config.archive.marker_id:
        raise PruneError(f"archive identity mismatch at {marker}")


def _safe_history_file(root: Path, run_id: str, relative: str,
                       *, require_file: bool) -> Tuple[Path, os.stat_result]:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise PruneError(f"unsafe run id: {run_id!r}")
    relative = _safe_relative(relative)
    candidate = root / run_id / Path(*PurePosixPath(relative).parts)
    current = root
    parts = (run_id,) + PurePosixPath(relative).parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if require_file:
                raise MissingHistoryError(f"planned history file is missing: {run_id}/{relative}")
            raise
        if stat.S_ISLNK(metadata.st_mode):
            raise PruneError(f"symlink is forbidden in history path: {run_id}/{relative}")
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise PruneError(f"history path component is not a directory: {current}")
    if require_file and not stat.S_ISREG(metadata.st_mode):
        raise PruneError(f"history candidate is not a regular file: {candidate}")
    resolved = candidate.resolve(strict=True)
    resolved_root = root.resolve(strict=True)
    if resolved_root not in resolved.parents:
        raise PruneError(f"history path escapes configured root: {run_id}/{relative}")
    return candidate, metadata


def _space(root: Path) -> Dict[str, float]:
    usage = shutil.disk_usage(root)
    return {"total": usage.total, "used": usage.used, "free": usage.free,
            "free_percent": (usage.free * 100.0 / usage.total) if usage.total else 0.0}


def _secure_unlink(root: Path, run_id: str, relative: str,
                   fingerprint: Tuple[int, int, int, int]) -> None:
    """Unlink through no-follow directory descriptors, rechecking at syscall time."""
    descriptors: List[int] = []
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(descriptor)
        parts = (run_id,) + PurePosixPath(relative).parts
        for part in parts[:-1]:
            descriptor = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            descriptors.append(descriptor)
        metadata = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
        actual = (metadata.st_size, metadata.st_mtime_ns, metadata.st_dev, metadata.st_ino)
        if not stat.S_ISREG(metadata.st_mode) or actual != fingerprint:
            raise PruneError(f"history candidate changed at deletion time: {run_id}/{relative}")
        os.unlink(parts[-1], dir_fd=descriptor)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _policy(config: AppConfig) -> Dict[str, object]:
    return {"auto_prune": False, "keep_days": config.retention.keep_days,
            "keep_min_runs": config.retention.keep_min_runs,
            "target_free_percent": config.retention.target_free_percent}


def plan_prune(config: AppConfig, *, now: Callable[[], datetime] = _now,
               prune_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex) -> PruneResult:
    """Persist an immutable evidence-based plan without deleting anything."""
    validate_state_location(config)
    _validate_archive_identity(config)
    root = _history_root(config)
    timestamp = now()
    prune_id = prune_id_factory()
    if not RUN_ID_PATTERN.fullmatch(prune_id):
        raise PruneError(f"unsafe prune id: {prune_id!r}")
    cutoff = timestamp - timedelta(days=config.retention.keep_days)
    protected_detail: List[Dict[str, str]] = []
    missing: List[Dict[str, str]] = []
    candidates: List[Dict[str, object]] = []
    with Ledger(config.ledger_path) as ledger:
        rows = ledger.history_archive_items()
        special = ledger.protected_history_runs()
        completed_runs = []
        for row in rows:
            if row["status"] in {"success", "warning"} and row["run_id"] not in completed_runs:
                completed_runs.append(str(row["run_id"]))
        newest = set(completed_runs[-config.retention.keep_min_runs:]) if config.retention.keep_min_runs else set()
        for row in rows:
            run_id, relative = str(row["run_id"]), str(row["path"])
            reasons = list(special.get(run_id, []))
            if not RUN_ID_PATTERN.fullmatch(run_id):
                raise PruneError(f"unsafe run id in ledger evidence: {run_id!r}")
            recorded_root = Path(str(row["history_path"]))
            expected_root = root / run_id
            try:
                if recorded_root.resolve(strict=False) != expected_root.resolve(strict=False):
                    raise PruneError(
                        f"ledger history path is not the configured run root: {recorded_root}"
                    )
            except OSError as exc:
                raise PruneError(f"cannot validate ledger history path: {recorded_root}: {exc}") from exc
            relevant = datetime.fromisoformat(str(row["completed_at"] or row["started_at"]))
            if row["status"] not in {"success", "warning"}:
                reasons.append(f"run_status_{row['status']}")
            if run_id in newest:
                reasons.append("keep_min_runs")
            if relevant > cutoff:
                reasons.append("too_recent")
            if reasons:
                protected_detail.append({"run_id": run_id, "relative_path": relative,
                                         "reasons": ",".join(sorted(set(reasons)))})
                continue
            try:
                path, metadata = _safe_history_file(root, run_id, relative, require_file=True)
            except MissingHistoryError:
                missing.append({"run_id": run_id, "relative_path": relative,
                                "reason": "missing_before_prune"})
                continue
            except PruneError:
                raise
            if metadata.st_size != int(row["size"]):
                protected_detail.append({"run_id": run_id, "relative_path": relative,
                                         "reasons": "ledger_size_mismatch"})
                continue
            candidates.append({
                "source_run_id": run_id, "relative_path": relative,
                "historical_path": f"{run_id}/{relative}", "bytes": metadata.st_size,
                "timestamp": relevant.isoformat(), "reason": "older_than_keep_days",
                "mtime_ns": metadata.st_mtime_ns, "device": metadata.st_dev,
                "inode": metadata.st_ino, "checksum": row["checksum"],
            })
        candidates.sort(key=lambda item: (str(item["timestamp"]), str(item["source_run_id"]),
                                          str(item["relative_path"])))
        before = _space(root)
        target = config.retention.target_free_percent
        if target is not None:
            needed = max(0, int(before["total"] * target / 100.0 - before["free"]))
            selected, accumulated = [], 0
            for item in candidates:
                if accumulated >= needed:
                    break
                selected.append(item)
                accumulated += int(item["bytes"])
            candidates = selected
        ledger.create_prune_plan(prune_id, timestamp.isoformat(), _policy(config), candidates)
    reclaimed = sum(int(item["bytes"]) for item in candidates)
    projected = dict(before)
    projected["free"] = min(int(before["total"]), int(before["free"]) + reclaimed)
    projected["used"] = max(0, int(before["used"]) - reclaimed)
    projected["free_percent"] = projected["free"] * 100.0 / projected["total"] if projected["total"] else 0.0
    manifest = {
        "schema_version": 1, "prune_id": prune_id, "created_at": timestamp.isoformat(),
        "status": "planned", "policy": _policy(config), "items": candidates,
        "protected": protected_detail, "missing_before_prune": missing,
        "summary": {"files": len(candidates), "bytes": reclaimed,
                    "runs": len({item['source_run_id'] for item in candidates}),
                    "protected_versions": len(protected_detail),
                    "oldest_candidate": candidates[0]["timestamp"] if candidates else None,
                    "newest_candidate": candidates[-1]["timestamp"] if candidates else None,
                    "free_before": before, "free_projected": projected,
                    "projection_note": "filesystem accounting may vary"},
    }
    path = config.manifests_root / f"prune-plan-{prune_id}.json"
    _atomic_json(path, manifest)
    return PruneResult(prune_id, "planned", len(candidates), reclaimed,
                       manifest["summary"]["runs"], path, len(protected_detail),
                       manifest["summary"]["oldest_candidate"],
                       manifest["summary"]["newest_candidate"], before, projected)


def _load_and_match_plan(config: AppConfig, prune_id: str,
                         ledger: Ledger) -> Tuple[Dict[str, object], List[Dict[str, object]]]:
    if not RUN_ID_PATTERN.fullmatch(prune_id):
        raise PruneError(f"unsafe prune id: {prune_id!r}")
    path = config.manifests_root / f"prune-plan-{prune_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PruneError(f"cannot load prune plan {prune_id}: {exc}") from exc
    run = ledger.prune_run(prune_id)
    if run is None:
        raise PruneError(f"prune plan does not exist in ledger: {prune_id}")
    if run["status"] != "planned":
        raise PruneError(f"prune plan is not executable (status={run['status']}): {prune_id}")
    db_items = ledger.prune_items(prune_id)
    manifest_items = payload.get("items")
    comparable = [{key: item.get(key) for key in
                   ("source_run_id","relative_path","historical_path","bytes","mtime_ns","device","inode","checksum")}
                  for item in manifest_items] if isinstance(manifest_items, list) else None
    db_comparable = [{key: item.get(key) for key in
                     ("source_run_id","relative_path","historical_path","bytes","mtime_ns","device","inode","checksum")}
                    for item in db_items]
    if payload.get("prune_id") != prune_id or comparable != db_comparable:
        raise PruneError("prune plan manifest does not match immutable ledger evidence")
    return payload, db_items


def execute_prune(config: AppConfig, prune_id: str, *, yes: bool,
                  now: Callable[[], datetime] = _now,
                  unlinker: Optional[Callable[[Path], None]] = None) -> PruneResult:
    """Prevalidate and then apply exactly one persisted plan."""
    if not yes:
        raise PruneError("prune execution requires explicit --yes")
    validate_state_location(config)
    _validate_archive_identity(config)
    root = _history_root(config)
    lock = BackupLock(config.state_root / "backup.lock", run_id=f"prune-{prune_id}")
    with lock, HistoryLock(config.state_root / ".history-access.lock", exclusive=True):
        with Ledger(config.ledger_path) as ledger:
            payload, items = _load_and_match_plan(config, prune_id, ledger)
            special = ledger.protected_history_runs()
            current = {(str(row["run_id"]), str(row["path"])): row
                       for row in ledger.history_archive_items()}
            # Validate every item before the first destructive syscall.
            validated = []
            for item in items:
                key = (str(item["source_run_id"]), str(item["relative_path"]))
                row = current.get(key)
                if row is None or row["status"] not in {"success", "warning"}:
                    raise PruneError(f"stale prune plan: ledger eligibility changed for {key}")
                if key[0] in special:
                    raise PruneError(f"stale prune plan: run became protected: {key[0]}")
                expected_path = f"{key[0]}/{key[1]}"
                if item["historical_path"] != expected_path:
                    raise PruneError(f"unsafe historical path in plan: {item['historical_path']}")
                path, metadata = _safe_history_file(root, key[0], key[1], require_file=True)
                fingerprint = (metadata.st_size, metadata.st_mtime_ns, metadata.st_dev, metadata.st_ino)
                planned = (item["bytes"], item["mtime_ns"], item["device"], item["inode"])
                if fingerprint != planned:
                    raise PruneError(f"stale prune plan: fingerprint changed for {expected_path}")
                validated.append((item, path))
            before = _space(root)
            ledger.begin_prune(prune_id)
            deleted_files = deleted_bytes = 0
            errors: List[str] = []
            cleanup_warnings: List[str] = []
            for item, path in validated:
                try:
                    if unlinker is None:
                        _secure_unlink(
                            root, str(item["source_run_id"]), str(item["relative_path"]),
                            (int(item["bytes"]), int(item["mtime_ns"]),
                             int(item["device"]), int(item["inode"])),
                        )
                    else:
                        unlinker(path)
                    stamp = now().isoformat()
                    ledger.mark_prune_item(prune_id, item["source_run_id"],
                                           item["relative_path"], "deleted", stamp,
                                           "deleted by explicit prune execution")
                    deleted_files += 1
                    deleted_bytes += int(item["bytes"])
                except Exception as exc:
                    detail = f"delete failed for {item['historical_path']}: {exc}"
                    errors.append(detail)
                    ledger.mark_prune_item(prune_id, item["source_run_id"],
                                           item["relative_path"], "failed", None, detail)
                    break
            for run_id in sorted({str(item["source_run_id"]) for item in items}):
                run_root = root / run_id
                try:
                    run_metadata = run_root.lstat()
                    unknown = stat.S_ISLNK(run_metadata.st_mode)
                    if stat.S_ISDIR(run_metadata.st_mode) and not unknown:
                        tracked = {relative for candidate_run, relative in current
                                   if candidate_run == run_id}
                        for directory, dirnames, filenames in os.walk(run_root, followlinks=False):
                            base = Path(directory)
                            if any((base / name).is_symlink() for name in dirnames):
                                unknown = True
                                break
                            for filename in filenames:
                                relative = (base / filename).relative_to(run_root).as_posix()
                                if relative not in tracked:
                                    unknown = True
                                    break
                            if unknown:
                                break
                except FileNotFoundError:
                    unknown = False
                if unknown:
                    cleanup_warnings.append(
                        f"untracked history content prevents directory cleanup: {run_id}"
                    )
            status = "failed" if errors else "success"
            finished = now().isoformat()
            ledger.finish_prune(prune_id, finished, status, deleted_files, deleted_bytes, errors)
            after = _space(root)
            execution = {"schema_version": 1, "prune_id": prune_id, "status": status,
                         "policy": payload["policy"], "plan_manifest": f"prune-plan-{prune_id}.json",
                         "executed_at": finished, "deleted_files": deleted_files,
                         "deleted_bytes": deleted_bytes, "errors": errors,
                         "cleanup_warnings": cleanup_warnings,
                         "free_before": before, "free_after": after,
                         "items": ledger.prune_items(prune_id)}
        path = config.manifests_root / f"prune-execution-{prune_id}.json"
        _atomic_json(path, execution)
    result = PruneResult(prune_id, status, deleted_files, deleted_bytes,
                         len({item["source_run_id"] for item in items}), path)
    if errors:
        raise PruneError(errors[0])
    return result
