"""SQLite-backed catalogue inventory and auditable run history."""

import json
import os
import sqlite3
import stat
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence

from gorbackup.config import AppConfig


class LedgerError(RuntimeError):
    """Raised when inventory state cannot be recorded safely."""


@dataclass(frozen=True)
class FileMetadata:
    relative_path: str
    size: int
    mtime_ns: int
    checksum: Optional[str] = None


@dataclass(frozen=True)
class ScanResult:
    run_id: str
    status: str
    file_count: int
    total_bytes: int
    changed_files: int
    deleted_paths: int
    warnings: Sequence[str]
    summary_path: Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed')),
    source_identity TEXT NOT NULL,
    destination_identity TEXT NOT NULL,
    file_count INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    bytes_copied INTEGER NOT NULL DEFAULT 0,
    bytes_moved INTEGER NOT NULL DEFAULT 0,
    bytes_archived INTEGER NOT NULL DEFAULT 0,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    errors_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS current_files (
    relative_path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    checksum TEXT,
    last_run_id TEXT NOT NULL REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS staged_files (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    checksum TEXT,
    PRIMARY KEY (run_id, relative_path)
);

CREATE TABLE IF NOT EXISTS file_changes (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    change_type TEXT NOT NULL CHECK (change_type IN ('added', 'modified', 'deleted')),
    old_size INTEGER,
    new_size INTEGER,
    old_mtime_ns INTEGER,
    new_mtime_ns INTEGER,
    old_checksum TEXT,
    new_checksum TEXT,
    PRIMARY KEY (run_id, relative_path)
);

CREATE TABLE IF NOT EXISTS plans (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    counts_json TEXT NOT NULL,
    bytes_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES plans(run_id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    path TEXT NOT NULL,
    related_path TEXT,
    size INTEGER NOT NULL DEFAULT 0,
    leaving_size INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_status_completed
    ON runs(status, completed_at DESC);
CREATE INDEX IF NOT EXISTS idx_file_changes_run_type
    ON file_changes(run_id, change_type);
CREATE INDEX IF NOT EXISTS idx_plan_items_run_category
    ON plan_items(run_id, category);
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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


def validate_state_location(config: AppConfig) -> None:
    archive = config.archive.root.resolve()
    source = config.source.path.resolve()
    state_root = config.state_root.resolve()
    if state_root != archive and archive not in state_root.parents:
        raise LedgerError(f"state directory must live under archive root: {state_root}")
    if state_root == source or source in state_root.parents:
        raise LedgerError(f"state directory must not be inside source: {state_root}")


class Ledger:
    """Transactional access to current inventory and historical runs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA synchronous = FULL")
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def start_run(
        self,
        run_id: str,
        started_at: str,
        source_identity: str,
        destination_identity: str,
        operation: str = "scan",
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO runs (
                    run_id, operation, started_at, status,
                    source_identity, destination_identity
                ) VALUES (?, ?, ?, 'running', ?, ?)
                """,
                (
                    run_id,
                    operation,
                    started_at,
                    source_identity,
                    destination_identity,
                ),
            )

    def save_plan(
        self,
        run_id: str,
        completed_at: str,
        status: str,
        items: Sequence[Dict[str, object]],
        counts: Dict[str, int],
        byte_totals: Dict[str, int],
        warnings: Sequence[str],
        errors: Sequence[str],
    ) -> None:
        if status not in {"success", "failed"}:
            raise LedgerError(f"invalid plan status: {status}")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO plans
                    (run_id, created_at, status, counts_json, bytes_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    completed_at,
                    status,
                    json.dumps(counts, sort_keys=True),
                    json.dumps(byte_totals, sort_keys=True),
                ),
            )
            self.connection.executemany(
                """
                INSERT INTO plan_items
                    (run_id, category, path, related_path, size, leaving_size, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        run_id,
                        item["category"],
                        item["path"],
                        item.get("related_path"),
                        item["size"],
                        item["leaving_size"],
                        item["reason"],
                    )
                    for item in items
                ),
            )
            self.connection.execute(
                """
                UPDATE runs SET completed_at = ?, status = ?, file_count = ?,
                    total_bytes = ?, warnings_json = ?, errors_json = ?
                WHERE run_id = ? AND status = 'running'
                """,
                (
                    completed_at,
                    status,
                    sum(counts.values()),
                    sum(byte_totals.values()),
                    json.dumps(list(warnings)),
                    json.dumps(list(errors)),
                    run_id,
                ),
            )

    def plan_items(self, run_id: str) -> List[Dict[str, object]]:
        rows = self.connection.execute(
            """
            SELECT category, path, related_path, size, leaving_size, reason
            FROM plan_items WHERE run_id = ? ORDER BY item_id
            """,
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def stage_files(self, run_id: str, files: Iterable[FileMetadata]) -> None:
        with self.connection:
            self.connection.executemany(
                """
                INSERT INTO staged_files
                    (run_id, relative_path, size, mtime_ns, checksum)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    (run_id, item.relative_path, item.size, item.mtime_ns, item.checksum)
                    for item in files
                ),
            )

    def complete_scan(
        self,
        run_id: str,
        completed_at: str,
        file_count: int,
        total_bytes: int,
        warnings: Sequence[str],
    ) -> None:
        connection = self.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None or row["status"] != "running":
                raise LedgerError(f"run is not active: {run_id}")

            connection.execute(
                """
                UPDATE staged_files
                SET checksum = (
                    SELECT current_files.checksum
                    FROM current_files
                    WHERE current_files.relative_path = staged_files.relative_path
                      AND current_files.size = staged_files.size
                      AND current_files.mtime_ns = staged_files.mtime_ns
                )
                WHERE run_id = ? AND checksum IS NULL
                """,
                (run_id,),
            )
            connection.execute(
                """
                INSERT INTO file_changes (
                    run_id, relative_path, change_type,
                    old_size, new_size, old_mtime_ns, new_mtime_ns,
                    old_checksum, new_checksum
                )
                SELECT ?, staged.relative_path,
                    CASE WHEN current.relative_path IS NULL THEN 'added' ELSE 'modified' END,
                    current.size, staged.size, current.mtime_ns, staged.mtime_ns,
                    current.checksum, staged.checksum
                FROM staged_files AS staged
                LEFT JOIN current_files AS current
                    ON current.relative_path = staged.relative_path
                WHERE staged.run_id = ? AND (
                    current.relative_path IS NULL
                    OR current.size != staged.size
                    OR current.mtime_ns != staged.mtime_ns
                    OR (
                        current.checksum IS NOT NULL
                        AND staged.checksum IS NOT NULL
                        AND current.checksum != staged.checksum
                    )
                )
                """,
                (run_id, run_id),
            )
            connection.execute(
                """
                INSERT INTO file_changes (
                    run_id, relative_path, change_type,
                    old_size, old_mtime_ns, old_checksum
                )
                SELECT ?, current.relative_path, 'deleted',
                    current.size, current.mtime_ns, current.checksum
                FROM current_files AS current
                LEFT JOIN staged_files AS staged
                    ON staged.run_id = ?
                   AND staged.relative_path = current.relative_path
                WHERE staged.relative_path IS NULL
                """,
                (run_id, run_id),
            )
            connection.execute("DELETE FROM current_files")
            connection.execute(
                """
                INSERT INTO current_files
                    (relative_path, size, mtime_ns, checksum, last_run_id)
                SELECT relative_path, size, mtime_ns, checksum, ?
                FROM staged_files WHERE run_id = ?
                """,
                (run_id, run_id),
            )
            connection.execute("DELETE FROM staged_files WHERE run_id = ?", (run_id,))
            connection.execute(
                """
                UPDATE runs SET
                    completed_at = ?, status = 'success', file_count = ?,
                    total_bytes = ?, warnings_json = ?
                WHERE run_id = ?
                """,
                (completed_at, file_count, total_bytes, json.dumps(list(warnings)), run_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def fail_run(self, run_id: str, completed_at: str, error: str) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM staged_files WHERE run_id = ?", (run_id,))
            self.connection.execute(
                """
                UPDATE runs SET completed_at = ?, status = 'failed', errors_json = ?
                WHERE run_id = ? AND status = 'running'
                """,
                (completed_at, json.dumps([error]), run_id),
            )

    def latest_successful_run(self) -> Optional[Dict[str, object]]:
        row = self.connection.execute(
            """
            SELECT * FROM runs WHERE status = 'success'
            ORDER BY completed_at DESC LIMIT 1
            """
        ).fetchone()
        return dict(row) if row is not None else None

    def totals(self) -> Dict[str, int]:
        row = self.connection.execute(
            "SELECT COUNT(*) AS file_count, COALESCE(SUM(size), 0) AS total_bytes FROM current_files"
        ).fetchone()
        return {"file_count": row["file_count"], "total_bytes": row["total_bytes"]}

    def changed_files(self, run_id: str) -> List[Dict[str, object]]:
        rows = self.connection.execute(
            """
            SELECT * FROM file_changes
            WHERE run_id = ? AND change_type IN ('added', 'modified')
            ORDER BY relative_path
            """,
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def deleted_paths(self, run_id: str) -> List[str]:
        rows = self.connection.execute(
            """
            SELECT relative_path FROM file_changes
            WHERE run_id = ? AND change_type = 'deleted'
            ORDER BY relative_path
            """,
            (run_id,),
        ).fetchall()
        return [row["relative_path"] for row in rows]

    def change_counts(self, run_id: str) -> Dict[str, int]:
        rows = self.connection.execute(
            """
            SELECT change_type, COUNT(*) AS count
            FROM file_changes WHERE run_id = ? GROUP BY change_type
            """,
            (run_id,),
        ).fetchall()
        counts = {"added": 0, "modified": 0, "deleted": 0}
        counts.update({row["change_type"]: row["count"] for row in rows})
        return counts

    def run_history(self, limit: int = 100) -> List[Dict[str, object]]:
        rows = self.connection.execute(
            """
            SELECT run_id, operation, started_at, completed_at, status,
                   file_count, total_bytes, bytes_copied, bytes_moved,
                   bytes_archived, warnings_json, errors_json
            FROM runs ORDER BY started_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def inventory_source(source: Path, marker_file: str) -> Iterator[FileMetadata]:
    """Yield metadata without opening or hashing catalogue file contents."""
    marker = source / marker_file

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
            yield FileMetadata(
                relative_path=path.relative_to(source).as_posix(),
                size=metadata.st_size,
                mtime_ns=metadata.st_mtime_ns,
            )


def scan_catalogue(
    config: AppConfig,
    *,
    now: Callable[[], datetime] = _utc_now,
    run_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    inventory: Callable[[Path, str], Iterable[FileMetadata]] = inventory_source,
) -> ScanResult:
    """Inventory source metadata and publish it only after a complete scan."""
    validate_state_location(config)
    run_id = run_id_factory()
    started_at = now().isoformat()
    warnings: List[str] = []
    file_count = 0
    total_bytes = 0

    with Ledger(config.ledger_path) as ledger:
        ledger.start_run(
            run_id,
            started_at,
            config.source.marker_id,
            config.archive.marker_id,
        )
        try:
            batch: List[FileMetadata] = []
            for item in inventory(config.source.path, config.source.marker_file):
                batch.append(item)
                file_count += 1
                total_bytes += item.size
                if len(batch) >= 1000:
                    ledger.stage_files(run_id, batch)
                    batch = []
            if batch:
                ledger.stage_files(run_id, batch)
            completed_at = now().isoformat()
            ledger.complete_scan(
                run_id, completed_at, file_count, total_bytes, warnings
            )
            counts = ledger.change_counts(run_id)
            changed_count = counts["added"] + counts["modified"]
            deleted_count = counts["deleted"]
        except Exception as exc:
            ledger.fail_run(run_id, now().isoformat(), str(exc))
            if isinstance(exc, LedgerError):
                raise
            raise LedgerError(f"catalogue scan failed: {exc}") from exc

    summary_path = config.manifests_root / f"scan-{run_id}.json"
    summary = {
        "schema_version": 1,
        "run_id": run_id,
        "status": "success",
        "started_at": started_at,
        "completed_at": completed_at,
        "source_identity": config.source.marker_id,
        "destination_identity": config.archive.marker_id,
        "file_count": file_count,
        "total_bytes": total_bytes,
        "changed_files": changed_count,
        "deleted_paths": deleted_count,
        "warnings": warnings,
    }
    try:
        _atomic_json(summary_path, summary)
        _atomic_json(config.manifests_root / "latest-scan.json", summary)
    except OSError as exc:
        raise LedgerError(f"could not write scan summary: {exc}") from exc
    return ScanResult(
        run_id,
        "success",
        file_count,
        total_bytes,
        changed_count,
        deleted_count,
        tuple(warnings),
        summary_path,
    )
