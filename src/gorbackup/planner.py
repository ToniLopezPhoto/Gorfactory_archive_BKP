"""Non-destructive rclone planning and machine-readable classification."""

import json
import os
import stat
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from gorbackup.config import AppConfig
from gorbackup.dependencies import RcloneInfo
from gorbackup.ledger import Ledger, inventory_source, validate_state_location

CATEGORIES = (
    "new_file",
    "changed_file",
    "delete_from_current",
    "rename_move_candidate",
    "skipped_recent",
    "error",
)


class PlanError(RuntimeError):
    """Raised when a plan cannot be generated or persisted."""


@dataclass(frozen=True)
class PlanItem:
    category: str
    path: str
    size: int
    reason: str
    related_path: Optional[str] = None
    leaving_size: int = 0


@dataclass(frozen=True)
class PlanResult:
    run_id: str
    status: str
    items: Tuple[PlanItem, ...]
    counts: Dict[str, int]
    byte_totals: Dict[str, int]
    catalogue_file_count: int
    catalogue_total_bytes: int
    planned_transfer_files: int
    planned_transfer_bytes: int
    planned_archive_files: int
    planned_archive_bytes: int
    manifest_path: Path

    @property
    def transfer_bytes(self) -> int:
        return self.planned_transfer_bytes

    @property
    def leaving_current_bytes(self) -> int:
        return self.planned_archive_bytes


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_path(root: Path, relative: str) -> Path:
    candidate = PurePosixPath(relative)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise PlanError(f"unsafe path in rclone report: {relative!r}")
    return root.joinpath(*candidate.parts)


def parse_json_log(lines: Iterable[str]) -> Tuple[List[PlanItem], List[str]]:
    """Extract structured errors from rclone NDJSON logs."""
    errors: List[PlanItem] = []
    warnings: List[str] = []
    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            errors.append(
                PlanItem("error", "", 0, f"invalid JSON log line {number}")
            )
            continue
        if not isinstance(entry, dict):
            errors.append(PlanItem("error", "", 0, f"invalid log entry {number}"))
            continue
        level = str(entry.get("level", "")).lower()
        message = str(entry.get("msg", "rclone error"))
        path = str(entry.get("object", ""))
        size = entry.get("size", 0)
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            size = 0
        if level in {"error", "fatal"}:
            errors.append(PlanItem("error", path, size, message))
        elif level in {"warning", "warn"}:
            warnings.append(message)
    return errors, warnings


def parse_combined_plan(
    lines: Iterable[str], source: Path, current: Path
) -> List[PlanItem]:
    """Parse rclone's stable combined report and resolve exact byte sizes."""
    items: List[PlanItem] = []
    mapping = {
        "+": ("new_file", source, None, "missing from current"),
        "*": ("changed_file", source, current, "source and current differ"),
        "-": ("delete_from_current", current, current, "exists only in current"),
        "!": ("error", source, None, "rclone could not compare path"),
    }
    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.rstrip("\r\n")
        if not line or line.startswith("= "):
            continue
        if len(line) < 3 or line[1] != " " or line[0] not in mapping:
            items.append(
                PlanItem("error", "", 0, f"invalid combined report line {number}")
            )
            continue
        category, root, leaving_root, reason = mapping[line[0]]
        relative = line[2:]
        try:
            path = _safe_path(root, relative)
            size = path.lstat().st_size if category != "error" else 0
            leaving_size = (
                _safe_path(leaving_root, relative).lstat().st_size
                if leaving_root is not None
                else 0
            )
        except (OSError, PlanError) as exc:
            items.append(PlanItem("error", relative, 0, f"cannot size path: {exc}"))
            continue
        items.append(
            PlanItem(
                category,
                relative,
                size,
                reason,
                leaving_size=leaving_size,
            )
        )
    return items


def find_recent_files(
    source: Path,
    marker_file: str,
    cutoff: datetime,
) -> List[PlanItem]:
    """Classify files excluded by the configured recent-file grace window."""
    marker = source / marker_file
    items: List[PlanItem] = []

    def raise_walk_error(error: OSError) -> None:
        raise error

    for directory, _, filenames in os.walk(source, onerror=raise_walk_error):
        for filename in filenames:
            path = Path(directory) / filename
            if path == marker:
                continue
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                continue
            modified = datetime.fromtimestamp(metadata.st_mtime, tz=timezone.utc)
            if modified > cutoff:
                items.append(
                    PlanItem(
                        "skipped_recent",
                        path.relative_to(source).as_posix(),
                        metadata.st_size,
                        "inside recent-file grace window",
                    )
                )
    return items


def _classify_rename_candidates(
    items: Sequence[PlanItem], source: Path, current: Path
) -> List[PlanItem]:
    new_items = [item for item in items if item.category == "new_file"]
    deleted_items = [item for item in items if item.category == "delete_from_current"]
    new_by_key: Dict[Tuple[int, int], List[PlanItem]] = {}
    deleted_by_key: Dict[Tuple[int, int], List[PlanItem]] = {}
    for collection, root, target in (
        (new_items, source, new_by_key),
        (deleted_items, current, deleted_by_key),
    ):
        for item in collection:
            try:
                metadata = _safe_path(root, item.path).lstat()
            except (OSError, PlanError):
                continue
            target.setdefault((metadata.st_size, metadata.st_mtime_ns), []).append(
                item
            )

    paired_new = set()
    paired_deleted = set()
    candidates: List[PlanItem] = []
    for key in new_by_key.keys() & deleted_by_key.keys():
        additions = new_by_key[key]
        deletions = deleted_by_key[key]
        if len(additions) == 1 and len(deletions) == 1:
            addition, deletion = additions[0], deletions[0]
            paired_new.add(addition.path)
            paired_deleted.add(deletion.path)
            candidates.append(
                PlanItem(
                    "rename_move_candidate",
                    addition.path,
                    addition.size,
                    "unique size and modification-time match",
                    related_path=deletion.path,
                    leaving_size=deletion.size,
                )
            )
    remaining = [
        item
        for item in items
        if not (
            (item.category == "new_file" and item.path in paired_new)
            or (item.category == "delete_from_current" and item.path in paired_deleted)
        )
    ]
    return remaining + candidates


def _summarize(items: Sequence[PlanItem]) -> Tuple[Dict[str, int], Dict[str, int]]:
    counts = {category: 0 for category in CATEGORIES}
    byte_totals = {category: 0 for category in CATEGORIES}
    for item in items:
        counts[item.category] += 1
        byte_totals[item.category] += item.size
    return counts, byte_totals


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


def create_plan(
    config: AppConfig,
    rclone: RcloneInfo,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    now: Callable[[], datetime] = _utc_now,
    run_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
) -> PlanResult:
    """Run rclone dry-run, classify operations, and persist the immutable plan."""
    validate_state_location(config)
    run_id = run_id_factory()
    started = now()
    current = config.archive.root / config.archive.current_dir
    backup_dir = config.archive.root / config.archive.history_dir / run_id
    config.state_root.mkdir(parents=True, exist_ok=True)
    combined_fd, combined_name = tempfile.mkstemp(
        prefix=".plan-combined-", suffix=".txt", dir=str(config.state_root)
    )
    log_fd, log_name = tempfile.mkstemp(
        prefix=".plan-log-", suffix=".jsonl", dir=str(config.state_root)
    )
    os.close(combined_fd)
    os.close(log_fd)
    combined_path = Path(combined_name)
    log_path = Path(log_name)
    command = [
        rclone.executable,
        "sync",
        str(config.source.path),
        str(current),
        "--dry-run",
        "--use-json-log",
        "--log-level",
        "INFO",
        "--log-file",
        str(log_path),
        "--combined",
        str(combined_path),
        "--backup-dir",
        str(backup_dir),
        "--exclude",
        f"/{config.source.marker_file}",
        "--retries",
        "1",
    ]
    if config.safety.ignore_recent_minutes:
        command.extend(
            ["--min-age", f"{config.safety.ignore_recent_minutes}m"]
        )

    with Ledger(config.ledger_path) as ledger:
        ledger.start_run(
            run_id,
            started.isoformat(),
            config.source.marker_id,
            config.archive.marker_id,
            operation="plan",
        )
        try:
            catalogue_before = tuple(sorted(
                inventory_source(config.source.path, config.source.marker_file),
                key=lambda item: item.relative_path,
            ))
            completed = runner(command, check=False, capture_output=True, text=True)
            combined_lines = combined_path.read_text(encoding="utf-8").splitlines()
            log_lines = log_path.read_text(encoding="utf-8").splitlines()
            items = parse_combined_plan(combined_lines, config.source.path, current)
            log_errors, warnings = parse_json_log(log_lines)
            items.extend(log_errors)
            catalogue_files = tuple(sorted(
                inventory_source(config.source.path, config.source.marker_file),
                key=lambda item: item.relative_path,
            ))
            if catalogue_files != catalogue_before:
                items.append(
                    PlanItem(
                        "error",
                        "",
                        0,
                        "catalogue changed while the immutable plan was being generated",
                    )
                )
            cutoff = started - timedelta(minutes=config.safety.ignore_recent_minutes)
            recent_items = find_recent_files(
                config.source.path, config.source.marker_file, cutoff
            )
            recent_paths = {item.path for item in recent_items}
            # Do not trust rclone's report to be the sole expression of the
            # grace window.  A recent source path cannot also be actionable.
            items = [
                item for item in items
                if item.category == "error" or item.path not in recent_paths
            ]
            items.extend(recent_items)
            items = _classify_rename_candidates(items, config.source.path, current)
            if completed.returncode != 0 and not log_errors:
                detail = (completed.stderr or completed.stdout).strip()
                items.append(
                    PlanItem(
                        "error",
                        "",
                        0,
                        f"rclone exited with {completed.returncode}: {detail}",
                    )
                )
            counts, byte_totals = _summarize(items)
            status = "failed" if counts["error"] else "success"
            completed_at = now().isoformat()
            serialized = [asdict(item) for item in items]
            errors = [item.reason for item in items if item.category == "error"]
            ledger.save_plan(
                run_id,
                completed_at,
                status,
                serialized,
                counts,
                byte_totals,
                warnings,
                errors,
                catalogue_files,
            )
        except Exception as exc:
            ledger.fail_run(run_id, now().isoformat(), str(exc))
            if isinstance(exc, PlanError):
                raise
            raise PlanError(f"could not generate plan: {exc}") from exc
        finally:
            for temporary in (combined_path, log_path):
                if temporary.exists():
                    temporary.unlink()

    planned_transfer_bytes = sum(
        byte_totals[name]
        for name in ("new_file", "changed_file", "rename_move_candidate")
    )
    planned_transfer_files = sum(
        counts[name] for name in ("new_file", "changed_file", "rename_move_candidate")
    )
    planned_archive_items = tuple(
        item for item in items
        if item.category in {"delete_from_current", "changed_file", "rename_move_candidate"}
    )
    planned_archive_files = len(planned_archive_items)
    planned_archive_bytes = sum(item.leaving_size for item in planned_archive_items)
    catalogue_file_count = len(catalogue_files)
    catalogue_total_bytes = sum(item.size for item in catalogue_files)
    manifest_path = config.manifests_root / f"plan-{run_id}.json"
    payload = {
        "schema_version": 2,
        "run_id": run_id,
        "status": status,
        "created_at": completed_at,
        "dry_run": True,
        "counts": counts,
        "bytes": byte_totals,
        "catalogue_file_count": catalogue_file_count,
        "catalogue_total_bytes": catalogue_total_bytes,
        "planned_transfer_files": planned_transfer_files,
        "planned_transfer_bytes": planned_transfer_bytes,
        "planned_archive_files": planned_archive_files,
        "planned_archive_bytes": planned_archive_bytes,
        "warnings": warnings,
        "items": serialized,
    }
    try:
        _atomic_json(manifest_path, payload)
        _atomic_json(config.manifests_root / "latest-plan.json", payload)
    except OSError as exc:
        raise PlanError(f"could not write plan manifest: {exc}") from exc
    return PlanResult(
        run_id,
        status,
        tuple(items),
        counts,
        byte_totals,
        catalogue_file_count,
        catalogue_total_bytes,
        planned_transfer_files,
        planned_transfer_bytes,
        planned_archive_files,
        planned_archive_bytes,
        manifest_path,
    )
