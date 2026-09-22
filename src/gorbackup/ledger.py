"""SQLite catalogue, immutable plans, executions, and known-good state."""

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

SCHEMA_VERSION = 6


class LedgerError(RuntimeError):
    """Raised when ledger state cannot be recorded safely."""


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
    catalogue_file_count: int
    catalogue_total_bytes: int
    changed_files: int
    deleted_paths: int
    warnings: Sequence[str]
    summary_path: Path


SCHEMA = """
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    operation TEXT NOT NULL CHECK (operation IN ('scan', 'plan', 'backup')),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'blocked', 'success', 'warning', 'failed')),
    source_identity TEXT NOT NULL,
    destination_identity TEXT NOT NULL,
    catalogue_file_count INTEGER NOT NULL DEFAULT 0,
    catalogue_total_bytes INTEGER NOT NULL DEFAULT 0,
    planned_transfer_files INTEGER NOT NULL DEFAULT 0,
    planned_transfer_bytes INTEGER NOT NULL DEFAULT 0,
    planned_archive_files INTEGER NOT NULL DEFAULT 0,
    planned_archive_bytes INTEGER NOT NULL DEFAULT 0,
    planned_rename_files INTEGER NOT NULL DEFAULT 0,
    planned_rename_bytes INTEGER NOT NULL DEFAULT 0,
    executed_transfer_files INTEGER NOT NULL DEFAULT 0,
    executed_transfer_bytes INTEGER NOT NULL DEFAULT 0,
    executed_archive_files INTEGER NOT NULL DEFAULT 0,
    executed_archive_bytes INTEGER NOT NULL DEFAULT 0,
    executed_rename_files INTEGER NOT NULL DEFAULT 0,
    executed_rename_bytes INTEGER NOT NULL DEFAULT 0,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    errors_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE catalogue_files (
    relative_path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    checksum TEXT,
    last_run_id TEXT NOT NULL REFERENCES runs(run_id)
);
CREATE TABLE staged_catalogue_files (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    checksum TEXT,
    PRIMARY KEY (run_id, relative_path)
);
CREATE TABLE catalogue_changes (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    change_type TEXT NOT NULL CHECK (change_type IN ('added', 'modified', 'deleted')),
    old_size INTEGER, new_size INTEGER,
    old_mtime_ns INTEGER, new_mtime_ns INTEGER,
    old_checksum TEXT, new_checksum TEXT,
    PRIMARY KEY (run_id, relative_path)
);

CREATE TABLE plans (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    counts_json TEXT NOT NULL,
    bytes_json TEXT NOT NULL
);
CREATE TABLE plan_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES plans(run_id) ON DELETE CASCADE,
    category TEXT NOT NULL, path TEXT NOT NULL, related_path TEXT,
    size INTEGER NOT NULL DEFAULT 0,
    leaving_size INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL
);
CREATE TABLE plan_catalogue_files (
    run_id TEXT NOT NULL REFERENCES plans(run_id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    checksum TEXT,
    PRIMARY KEY (run_id, relative_path)
);

CREATE TABLE executions (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    plan_run_id TEXT NOT NULL REFERENCES plans(run_id),
    history_path TEXT NOT NULL,
    report_path TEXT NOT NULL,
    reconciliation_status TEXT NOT NULL CHECK
        (reconciliation_status IN ('exact', 'diverged', 'failed'))
);
CREATE TABLE safety_assessments (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    plan_run_id TEXT NOT NULL REFERENCES plans(run_id),
    assessment_json TEXT NOT NULL,
    failed_gates_json TEXT NOT NULL,
    override_requested INTEGER NOT NULL CHECK (override_requested IN (0, 1)),
    override_used INTEGER NOT NULL CHECK (override_used IN (0, 1)),
    overridden_gates_json TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE execution_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES executions(run_id) ON DELETE CASCADE,
    operation TEXT NOT NULL CHECK (operation IN ('transfer', 'archive', 'rename')),
    classification TEXT NOT NULL,
    path TEXT NOT NULL,
    related_path TEXT,
    size INTEGER NOT NULL,
    checksum TEXT,
    message TEXT NOT NULL
);
CREATE TABLE reconciliation_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES executions(run_id) ON DELETE CASCADE,
    severity TEXT NOT NULL CHECK (severity IN ('warning', 'failure')),
    divergence_type TEXT NOT NULL,
    operation TEXT NOT NULL,
    path TEXT NOT NULL,
    planned_bytes INTEGER,
    executed_bytes INTEGER,
    detail TEXT NOT NULL
);
CREATE TABLE verification_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES executions(run_id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    method TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('verified', 'mismatch', 'source_changed', 'error')),
    bytes_verified INTEGER NOT NULL,
    source_checksum TEXT,
    destination_checksum TEXT,
    detail TEXT NOT NULL
);
CREATE TABLE known_good_files (
    relative_path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    checksum TEXT,
    promoted_by_run_id TEXT NOT NULL REFERENCES runs(run_id)
);
CREATE TABLE known_good_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    catalogue_file_count INTEGER NOT NULL,
    catalogue_total_bytes INTEGER NOT NULL,
    promoted_at TEXT NOT NULL
);

CREATE TABLE restore_runs (
    restore_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed')),
    relative_path TEXT NOT NULL,
    source_run_id TEXT NOT NULL,
    historical_source_path TEXT,
    destination_path TEXT,
    bytes INTEGER NOT NULL DEFAULT 0,
    verification_method TEXT,
    checksum TEXT,
    error TEXT
);

CREATE INDEX idx_runs_status_completed ON runs(status, completed_at DESC);
CREATE INDEX idx_plan_items_run_category ON plan_items(run_id, category);
CREATE INDEX idx_execution_items_run_operation ON execution_items(run_id, operation);
CREATE INDEX idx_reconciliation_items_run ON reconciliation_items(run_id);
CREATE INDEX idx_verification_items_run_status ON verification_items(run_id, status);
CREATE INDEX idx_restore_runs_started ON restore_runs(started_at DESC);
PRAGMA user_version = 6;
"""

_V1_TABLES = (
    "safety_assessments", "verification_items", "reconciliation_items", "execution_items", "executions",
    "known_good_state", "known_good_files", "plan_catalogue_files", "catalogue_changes",
    "staged_catalogue_files", "catalogue_files", "backup_runs", "file_changes",
    "current_files", "staged_files", "plan_items", "plans", "runs",
)


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
    """Transactional access to catalogue, plan, and execution evidence."""

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = path
        if read_only:
            self.connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA synchronous = FULL")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        self.schema_version = int(version)
        has_runs = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'"
        ).fetchone()
        if version not in (0, 1, 2, 3, 4, 5, SCHEMA_VERSION):
            raise LedgerError(f"unsupported ledger schema version: {version}")
        if read_only:
            if not has_runs:
                raise LedgerError(f"ledger does not exist or is not initialized: {path}")
            return
        if version == 5:
            self._migrate_v5()
            self.schema_version = SCHEMA_VERSION
            return
        if version == 4:
            self._migrate_v4()
            columns = {row[1] for row in self.connection.execute("PRAGMA table_info(runs)")}
            if "planned_rename_files" not in columns:
                self._migrate_v5()
            else:
                self.connection.execute("PRAGMA user_version = 6")
            self.schema_version = SCHEMA_VERSION
            return
        if version in (1, 2, 3) or (version == 0 and has_runs):
            self._migrate_v1()
            self.schema_version = SCHEMA_VERSION
        elif not has_runs:
            self.connection.executescript(SCHEMA)
            self.schema_version = SCHEMA_VERSION

    def _migrate_v4(self) -> None:
        """Add restore evidence without rewriting existing backup evidence."""
        try:
            self.connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE restore_runs (
                    restore_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed')),
                    relative_path TEXT NOT NULL,
                    source_run_id TEXT NOT NULL,
                    historical_source_path TEXT,
                    destination_path TEXT,
                    bytes INTEGER NOT NULL DEFAULT 0,
                    verification_method TEXT,
                    checksum TEXT,
                    error TEXT
                );
                ALTER TABLE execution_items ADD COLUMN checksum TEXT;
                CREATE INDEX idx_restore_runs_started ON restore_runs(started_at DESC);
                PRAGMA user_version = 5;
                COMMIT;
                """
            )
        except Exception:
            self.connection.rollback()
            raise

    def _migrate_v5(self) -> None:
        """Add explicit rename evidence and metrics while preserving v5 rows."""
        try:
            self.connection.executescript(
                """
                BEGIN IMMEDIATE;
                ALTER TABLE runs ADD COLUMN planned_rename_files INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE runs ADD COLUMN planned_rename_bytes INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE runs ADD COLUMN executed_rename_files INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE runs ADD COLUMN executed_rename_bytes INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE execution_items RENAME TO execution_items_v5;
                CREATE TABLE execution_items (
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES executions(run_id) ON DELETE CASCADE,
                    operation TEXT NOT NULL CHECK (operation IN ('transfer', 'archive', 'rename')),
                    classification TEXT NOT NULL, path TEXT NOT NULL, related_path TEXT,
                    size INTEGER NOT NULL, checksum TEXT, message TEXT NOT NULL
                );
                INSERT INTO execution_items
                    (item_id, run_id, operation, classification, path, size, checksum, message)
                    SELECT item_id, run_id, operation, classification, path, size, checksum, message
                    FROM execution_items_v5;
                DROP TABLE execution_items_v5;
                CREATE INDEX idx_execution_items_run_operation ON execution_items(run_id, operation);
                PRAGMA user_version = 6;
                COMMIT;
                """
            )
        except Exception:
            self.connection.rollback()
            raise

    def _migrate_v1(self) -> None:
        """Pre-production v1 migration: replace ambiguous metrics atomically."""
        drops = "\n".join(f"DROP TABLE IF EXISTS {table};" for table in _V1_TABLES)
        try:
            self.connection.executescript(f"BEGIN IMMEDIATE;\n{drops}\n{SCHEMA}\nCOMMIT;")
        except Exception:
            self.connection.rollback()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def start_run(self, run_id: str, started_at: str, source_identity: str,
                  destination_identity: str, operation: str = "scan") -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO runs (run_id, operation, started_at, status, source_identity, destination_identity) VALUES (?, ?, ?, 'running', ?, ?)",
                (run_id, operation, started_at, source_identity, destination_identity),
            )

    def save_plan(self, run_id: str, completed_at: str, status: str,
                  items: Sequence[Dict[str, object]], counts: Dict[str, int],
                  byte_totals: Dict[str, int], warnings: Sequence[str],
                  errors: Sequence[str], catalogue_files: Sequence[FileMetadata] = ()) -> None:
        if status not in {"success", "failed"}:
            raise LedgerError(f"invalid plan status: {status}")
        catalogue_file_count = len(catalogue_files)
        catalogue_total_bytes = sum(item.size for item in catalogue_files)
        transfer_categories = {"new_file", "changed_file"}
        archive_categories = {"delete_from_current", "changed_file"}
        planned_transfer_files = sum(counts.get(name, 0) for name in transfer_categories)
        planned_transfer_bytes = sum(byte_totals.get(name, 0) for name in transfer_categories)
        planned_archive_items = [item for item in items if item["category"] in archive_categories]
        with self.connection:
            self.connection.execute(
                "INSERT INTO plans (run_id, created_at, status, counts_json, bytes_json) VALUES (?, ?, ?, ?, ?)",
                (run_id, completed_at, status, json.dumps(counts, sort_keys=True), json.dumps(byte_totals, sort_keys=True)),
            )
            self.connection.executemany(
                "INSERT INTO plan_items (run_id, category, path, related_path, size, leaving_size, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ((run_id, item["category"], item["path"], item.get("related_path"), item["size"], item["leaving_size"], item["reason"]) for item in items),
            )
            self.connection.executemany(
                "INSERT INTO plan_catalogue_files (run_id, relative_path, size, mtime_ns, checksum) VALUES (?, ?, ?, ?, ?)",
                ((run_id, item.relative_path, item.size, item.mtime_ns, item.checksum) for item in catalogue_files),
            )
            cursor = self.connection.execute(
                """UPDATE runs SET completed_at=?, status=?, catalogue_file_count=?,
                   catalogue_total_bytes=?, planned_transfer_files=?, planned_transfer_bytes=?,
                   planned_archive_files=?, planned_archive_bytes=?,
                   planned_rename_files=?, planned_rename_bytes=?, warnings_json=?, errors_json=?
                   WHERE run_id=? AND status='running'""",
                (completed_at, status, catalogue_file_count, catalogue_total_bytes,
                 planned_transfer_files, planned_transfer_bytes, len(planned_archive_items),
                 sum(int(item["leaving_size"]) for item in planned_archive_items),
                 counts.get("rename_move_candidate", 0),
                 byte_totals.get("rename_move_candidate", 0),
                 json.dumps(list(warnings)), json.dumps(list(errors)), run_id),
            )
            if cursor.rowcount != 1:
                raise LedgerError(f"run is not active: {run_id}")

    def plan_items(self, run_id: str) -> List[Dict[str, object]]:
        rows = self.connection.execute(
            "SELECT category, path, related_path, size, leaving_size, reason FROM plan_items WHERE run_id=? ORDER BY item_id", (run_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def record_execution_evidence(self, run_id: str, plan_run_id: str,
                                  history_path: str, report_path: str,
                                  execution_items: Sequence[Dict[str, object]],
                                  divergences: Sequence[Dict[str, object]]) -> None:
        """Persist immutable rclone/reconciliation evidence before verification."""
        reconciliation = (
            "failed" if any(item["severity"] == "failure" for item in divergences)
            else "diverged" if divergences else "exact"
        )
        prior_checksums = {
            row["relative_path"]: (int(row["size"]), row["checksum"])
            for row in self.connection.execute(
                "SELECT relative_path, size, checksum FROM known_good_files"
            )
        }
        evidence = []
        for item in execution_items:
            checksum = item.get("checksum")
            prior = prior_checksums.get(str(item["path"]))
            if (checksum is None and item["operation"] == "archive" and prior
                    and prior[0] == int(item["size"])):
                checksum = prior[1]
            evidence.append((
                run_id, item["operation"], item["classification"], item["path"],
                item.get("related_path"), item["size"], checksum, item["message"],
            ))
        with self.connection:
            self.connection.execute(
                "INSERT INTO executions (run_id, plan_run_id, history_path, report_path, reconciliation_status) VALUES (?, ?, ?, ?, ?)",
                (run_id, plan_run_id, history_path, report_path, reconciliation),
            )
            self.connection.executemany(
                "INSERT INTO execution_items (run_id, operation, classification, path, related_path, size, checksum, message) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                evidence,
            )
            self.connection.executemany(
                "INSERT INTO reconciliation_items (run_id, severity, divergence_type, operation, path, planned_bytes, executed_bytes, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ((run_id, item["severity"], item["divergence_type"], item["operation"], item["path"], item.get("planned_bytes"), item.get("executed_bytes"), item["detail"]) for item in divergences),
            )

    def finalize_execution(self, run_id: str, completed_at: str, status: str,
                           verification_items: Sequence[Dict[str, object]],
                           warnings: Sequence[str], errors: Sequence[str]) -> None:
        if status not in {"success", "warning", "failed"}:
            raise LedgerError(f"invalid execution status: {status}")
        execution = self.connection.execute(
            "SELECT plan_run_id FROM executions WHERE run_id=?", (run_id,)
        ).fetchone()
        if execution is None:
            raise LedgerError(f"execution evidence does not exist: {run_id}")
        plan = self.connection.execute(
            "SELECT catalogue_file_count, catalogue_total_bytes, planned_transfer_files, planned_transfer_bytes, planned_archive_files, planned_archive_bytes, planned_rename_files, planned_rename_bytes FROM runs WHERE run_id=? AND operation='plan'",
            (execution["plan_run_id"],),
        ).fetchone()
        if plan is None:
            raise LedgerError(f"plan does not exist: {execution['plan_run_id']}")
        transfers = self.connection.execute(
            "SELECT COUNT(*) AS count, COALESCE(SUM(size), 0) AS bytes FROM execution_items WHERE run_id=? AND operation='transfer'",
            (run_id,),
        ).fetchone()
        archives = self.connection.execute(
            "SELECT COUNT(*) AS count, COALESCE(SUM(size), 0) AS bytes FROM execution_items WHERE run_id=? AND operation='archive'",
            (run_id,),
        ).fetchone()
        renames = self.connection.execute(
            "SELECT COUNT(*) AS count, COALESCE(SUM(size), 0) AS bytes FROM execution_items WHERE run_id=? AND operation='rename'",
            (run_id,),
        ).fetchone()
        with self.connection:
            self.connection.executemany(
                """INSERT INTO verification_items
                   (run_id, path, method, status, bytes_verified,
                    source_checksum, destination_checksum, detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ((run_id, item["path"], item["method"], item["status"],
                  item["bytes_verified"], item.get("source_checksum"),
                  item.get("destination_checksum"), item["detail"])
                 for item in verification_items),
            )
            cursor = self.connection.execute(
                """UPDATE runs SET completed_at=?, status=?, catalogue_file_count=?, catalogue_total_bytes=?,
                   planned_transfer_files=?, planned_transfer_bytes=?, planned_archive_files=?, planned_archive_bytes=?,
                   planned_rename_files=?, planned_rename_bytes=?,
                   executed_transfer_files=?, executed_transfer_bytes=?, executed_archive_files=?, executed_archive_bytes=?,
                   executed_rename_files=?, executed_rename_bytes=?,
                   warnings_json=?, errors_json=? WHERE run_id=? AND status='running'""",
                (completed_at, status, *tuple(plan), transfers["count"], transfers["bytes"],
                 archives["count"], archives["bytes"], renames["count"], renames["bytes"], json.dumps(list(warnings)),
                 json.dumps(list(errors)), run_id),
            )
            if cursor.rowcount != 1:
                raise LedgerError(f"run is not active: {run_id}")

    def record_safety_assessment(
        self, run_id: str, plan_run_id: str, assessment: Dict[str, object],
        failed_gates: Sequence[str], *, override_requested: bool,
        override_used: bool, overridden_gates: Sequence[str], message: str,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO safety_assessments
                   (run_id, plan_run_id, assessment_json, failed_gates_json,
                    override_requested, override_used, overridden_gates_json, message)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, plan_run_id, json.dumps(assessment, sort_keys=True),
                 json.dumps(list(failed_gates)), int(override_requested),
                 int(override_used), json.dumps(list(overridden_gates)), message),
            )

    def block_run(self, run_id: str, completed_at: str, plan_run_id: str,
                  error: str) -> None:
        plan = self.connection.execute(
            """SELECT catalogue_file_count, catalogue_total_bytes,
                      planned_transfer_files, planned_transfer_bytes,
                      planned_archive_files, planned_archive_bytes,
                      planned_rename_files, planned_rename_bytes
               FROM runs WHERE run_id=? AND operation='plan'""", (plan_run_id,),
        ).fetchone()
        if plan is None:
            raise LedgerError(f"plan does not exist: {plan_run_id}")
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE runs SET completed_at=?, status='blocked',
                   catalogue_file_count=?, catalogue_total_bytes=?,
                   planned_transfer_files=?, planned_transfer_bytes=?,
                   planned_archive_files=?, planned_archive_bytes=?,
                   planned_rename_files=?, planned_rename_bytes=?, errors_json=?
                   WHERE run_id=? AND status='running'""",
                (completed_at, *tuple(plan), json.dumps([error]), run_id),
            )
            if cursor.rowcount != 1:
                raise LedgerError(f"run is not active: {run_id}")

    def safety_assessment(self, run_id: str) -> Optional[Dict[str, object]]:
        row = self.connection.execute(
            "SELECT * FROM safety_assessments WHERE run_id=?", (run_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def promote_known_good(self, run_id: str) -> None:
        """Promote the protected snapshot, retaining old metadata for recent files."""
        row = self.connection.execute(
            """SELECT executions.plan_run_id, runs.catalogue_file_count,
                      runs.catalogue_total_bytes, runs.completed_at
               FROM executions JOIN runs USING (run_id)
               WHERE run_id=? AND runs.status='success'
                 AND executions.reconciliation_status='exact'""", (run_id,),
        ).fetchone()
        if row is None:
            raise LedgerError(f"run is not eligible for known-good promotion: {run_id}")
        recent_paths = {
            item["path"] for item in self.connection.execute(
                "SELECT path FROM plan_items WHERE run_id=? AND category='skipped_recent'",
                (row["plan_run_id"],),
            )
        }
        previous = {
            item["relative_path"]: tuple(item)
            for item in self.connection.execute(
                "SELECT relative_path, size, mtime_ns, checksum FROM known_good_files"
            )
        }
        planned = {
            item["relative_path"]: tuple(item)
            for item in self.connection.execute(
                "SELECT relative_path, size, mtime_ns, checksum FROM plan_catalogue_files WHERE run_id=?",
                (row["plan_run_id"],),
            )
            if item["relative_path"] not in recent_paths
        }
        verified_checksums = {
            item["path"]: f"{item['method']}:{item['source_checksum']}"
            for item in self.connection.execute(
                """SELECT path, method, source_checksum FROM verification_items
                   WHERE run_id=? AND status='verified'""", (run_id,)
            )
        }
        planned = {
            path: (item[0], item[1], item[2],
                   verified_checksums.get(path) or item[3]
                   or (previous.get(path, (None, None, None, None))[3]
                       if previous.get(path, (None, None, None, None))[1:3] == item[1:3]
                       else None))
            for path, item in planned.items()
        }
        protected = dict(planned)
        for path in recent_paths:
            if path in previous:
                protected[path] = previous[path]
        protected_count = len(protected)
        protected_bytes = sum(int(item[1]) for item in protected.values())
        with self.connection:
            self.connection.execute("DELETE FROM known_good_files")
            self.connection.executemany(
                """INSERT INTO known_good_files
                   (relative_path, size, mtime_ns, checksum, promoted_by_run_id)
                   VALUES (?, ?, ?, ?, ?)""",
                ((*item, run_id) for item in protected.values()),
            )
            self.connection.execute(
                """INSERT INTO known_good_state (singleton, run_id, catalogue_file_count, catalogue_total_bytes, promoted_at)
                   VALUES (1, ?, ?, ?, ?) ON CONFLICT(singleton) DO UPDATE SET
                   run_id=excluded.run_id, catalogue_file_count=excluded.catalogue_file_count,
                   catalogue_total_bytes=excluded.catalogue_total_bytes, promoted_at=excluded.promoted_at""",
                (run_id, protected_count, protected_bytes, row["completed_at"]),
            )

    def invalidate_completed_run(self, run_id: str, error: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET status='failed', errors_json=? WHERE run_id=? AND status IN ('success','warning')",
                (json.dumps([error]), run_id),
            )

    def execution_items(self, run_id: str) -> List[Dict[str, object]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT operation, classification, path, related_path, size, checksum, message FROM execution_items WHERE run_id=? ORDER BY item_id", (run_id,)
        ).fetchall()]

    def reconciliation_items(self, run_id: str) -> List[Dict[str, object]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT severity, divergence_type, operation, path, planned_bytes, executed_bytes, detail FROM reconciliation_items WHERE run_id=? ORDER BY item_id", (run_id,)
        ).fetchall()]

    def verification_items(self, run_id: str) -> List[Dict[str, object]]:
        return [dict(row) for row in self.connection.execute(
            """SELECT path, method, status, bytes_verified, source_checksum,
                      destination_checksum, detail FROM verification_items
               WHERE run_id=? ORDER BY item_id""", (run_id,)
        ).fetchall()]

    def historical_versions(self, relative_path: str) -> List[Dict[str, object]]:
        """Return ledger-backed archive evidence newest first."""
        checksum_column = "ei.checksum" if self.schema_version >= 5 else "NULL AS checksum"
        rows = self.connection.execute(
            f"""SELECT r.run_id, r.started_at, r.completed_at, r.status,
                      e.history_path, ei.path, ei.size, ei.classification,
                      p.category AS plan_category, {checksum_column}
               FROM execution_items ei
               JOIN executions e ON e.run_id=ei.run_id
               JOIN runs r ON r.run_id=ei.run_id
               LEFT JOIN plan_items p ON p.run_id=e.plan_run_id
                    AND (p.path=ei.path OR p.related_path=ei.path)
                    AND p.category IN ('changed_file','delete_from_current','rename_move_candidate')
               WHERE ei.operation='archive' AND ei.classification!='deleted'
                 AND ei.path=?
               ORDER BY COALESCE(r.completed_at, r.started_at) DESC, ei.item_id DESC""",
            (relative_path,),
        ).fetchall()
        return [dict(row) for row in rows]

    def logical_renames(self, old_path: str) -> List[Dict[str, object]]:
        """Return non-restorable evidence that an old path moved elsewhere."""
        return [dict(row) for row in self.connection.execute(
            """SELECT r.run_id, r.started_at, r.completed_at, r.status,
                      ei.path, ei.related_path, ei.size, ei.checksum
               FROM execution_items ei JOIN runs r ON r.run_id=ei.run_id
               WHERE ei.operation='rename' AND ei.related_path=?
               ORDER BY COALESCE(r.completed_at, r.started_at) DESC, ei.item_id DESC""",
            (old_path,),
        ).fetchall()]

    def current_version(self, relative_path: str) -> Optional[Dict[str, object]]:
        row = self.connection.execute(
            """SELECT relative_path, size, mtime_ns, checksum, promoted_by_run_id
               FROM known_good_files WHERE relative_path=?""",
            (relative_path,),
        ).fetchone()
        return dict(row) if row is not None else None

    def start_restore(self, restore_id: str, started_at: str, relative_path: str,
                      source_run_id: str, historical_source_path: str,
                      destination_path: str) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO restore_runs
                   (restore_id, started_at, status, relative_path, source_run_id,
                    historical_source_path, destination_path)
                   VALUES (?, ?, 'running', ?, ?, ?, ?)""",
                (restore_id, started_at, relative_path, source_run_id,
                 historical_source_path, destination_path),
            )

    def finish_restore(self, restore_id: str, completed_at: str, *, status: str,
                       bytes_copied: int = 0, verification_method: Optional[str] = None,
                       checksum: Optional[str] = None, error: Optional[str] = None) -> None:
        if status not in {"success", "failed"}:
            raise LedgerError(f"invalid restore status: {status}")
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE restore_runs SET completed_at=?, status=?, bytes=?,
                   verification_method=?, checksum=?, error=?
                   WHERE restore_id=? AND status='running'""",
                (completed_at, status, bytes_copied, verification_method,
                 checksum, error, restore_id),
            )
            if cursor.rowcount != 1:
                raise LedgerError(f"restore is not active: {restore_id}")

    def restore_history(self) -> List[Dict[str, object]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM restore_runs ORDER BY started_at DESC"
        ).fetchall()]

    def stage_files(self, run_id: str, files: Iterable[FileMetadata]) -> None:
        with self.connection:
            self.connection.executemany(
                "INSERT INTO staged_catalogue_files (run_id, relative_path, size, mtime_ns, checksum) VALUES (?, ?, ?, ?, ?)",
                ((run_id, item.relative_path, item.size, item.mtime_ns, item.checksum) for item in files),
            )

    def complete_scan(self, run_id: str, completed_at: str, file_count: int,
                      total_bytes: int, warnings: Sequence[str]) -> None:
        connection = self.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None or row["status"] != "running":
                raise LedgerError(f"run is not active: {run_id}")
            connection.execute(
                """UPDATE staged_catalogue_files SET checksum=(SELECT catalogue_files.checksum FROM catalogue_files
                   WHERE catalogue_files.relative_path=staged_catalogue_files.relative_path
                   AND catalogue_files.size=staged_catalogue_files.size AND catalogue_files.mtime_ns=staged_catalogue_files.mtime_ns)
                   WHERE run_id=? AND checksum IS NULL""", (run_id,),
            )
            connection.execute(
                """INSERT INTO catalogue_changes (run_id, relative_path, change_type, old_size, new_size, old_mtime_ns, new_mtime_ns, old_checksum, new_checksum)
                   SELECT ?, staged.relative_path, CASE WHEN current.relative_path IS NULL THEN 'added' ELSE 'modified' END,
                   current.size, staged.size, current.mtime_ns, staged.mtime_ns, current.checksum, staged.checksum
                   FROM staged_catalogue_files staged LEFT JOIN catalogue_files current ON current.relative_path=staged.relative_path
                   WHERE staged.run_id=? AND (current.relative_path IS NULL OR current.size!=staged.size OR current.mtime_ns!=staged.mtime_ns
                   OR (current.checksum IS NOT NULL AND staged.checksum IS NOT NULL AND current.checksum!=staged.checksum))""", (run_id, run_id),
            )
            connection.execute(
                """INSERT INTO catalogue_changes (run_id, relative_path, change_type, old_size, old_mtime_ns, old_checksum)
                   SELECT ?, current.relative_path, 'deleted', current.size, current.mtime_ns, current.checksum
                   FROM catalogue_files current LEFT JOIN staged_catalogue_files staged ON staged.run_id=? AND staged.relative_path=current.relative_path
                   WHERE staged.relative_path IS NULL""", (run_id, run_id),
            )
            connection.execute("DELETE FROM catalogue_files")
            connection.execute(
                "INSERT INTO catalogue_files SELECT relative_path, size, mtime_ns, checksum, ? FROM staged_catalogue_files WHERE run_id=?", (run_id, run_id),
            )
            connection.execute("DELETE FROM staged_catalogue_files WHERE run_id=?", (run_id,))
            connection.execute(
                "UPDATE runs SET completed_at=?, status='success', catalogue_file_count=?, catalogue_total_bytes=?, warnings_json=? WHERE run_id=?",
                (completed_at, file_count, total_bytes, json.dumps(list(warnings)), run_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def fail_run(self, run_id: str, completed_at: str, error: str) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM staged_catalogue_files WHERE run_id=?", (run_id,))
            self.connection.execute(
                "UPDATE runs SET completed_at=?, status='failed', errors_json=? WHERE run_id=? AND status='running'",
                (completed_at, json.dumps([error]), run_id),
            )

    def latest_successful_run(self) -> Optional[Dict[str, object]]:
        row = self.connection.execute(
            "SELECT * FROM runs WHERE status='success' ORDER BY completed_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row is not None else None

    def known_good_state(self) -> Optional[Dict[str, object]]:
        row = self.connection.execute("SELECT * FROM known_good_state WHERE singleton=1").fetchone()
        return dict(row) if row is not None else None

    def totals(self) -> Dict[str, int]:
        row = self.connection.execute(
            "SELECT COUNT(*) catalogue_file_count, COALESCE(SUM(size),0) catalogue_total_bytes FROM catalogue_files"
        ).fetchone()
        return dict(row)

    def changed_files(self, run_id: str) -> List[Dict[str, object]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM catalogue_changes WHERE run_id=? AND change_type IN ('added','modified') ORDER BY relative_path", (run_id,)
        ).fetchall()]

    def deleted_paths(self, run_id: str) -> List[str]:
        return [row["relative_path"] for row in self.connection.execute(
            "SELECT relative_path FROM catalogue_changes WHERE run_id=? AND change_type='deleted' ORDER BY relative_path", (run_id,)
        ).fetchall()]

    def change_counts(self, run_id: str) -> Dict[str, int]:
        counts = {"added": 0, "modified": 0, "deleted": 0}
        counts.update({row["change_type"]: row["count"] for row in self.connection.execute(
            "SELECT change_type, COUNT(*) count FROM catalogue_changes WHERE run_id=? GROUP BY change_type", (run_id,)
        ).fetchall()})
        return counts

    def run_history(self, limit: int = 100) -> List[Dict[str, object]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()]


def inventory_source(source: Path, marker_file: str) -> Iterator[FileMetadata]:
    marker = source / marker_file
    def raise_walk_error(error: OSError) -> None:
        raise error
    for directory, _, filenames in os.walk(source, onerror=raise_walk_error):
        for filename in filenames:
            path = Path(directory) / filename
            if path == marker:
                continue
            metadata = path.lstat()
            if stat.S_ISREG(metadata.st_mode):
                yield FileMetadata(path.relative_to(source).as_posix(), metadata.st_size, metadata.st_mtime_ns)


def scan_catalogue(config: AppConfig, *, now: Callable[[], datetime] = _utc_now,
                   run_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
                   inventory: Callable[[Path, str], Iterable[FileMetadata]] = inventory_source) -> ScanResult:
    validate_state_location(config)
    run_id, started_at = run_id_factory(), now().isoformat()
    warnings: List[str] = []
    file_count = total_bytes = 0
    with Ledger(config.ledger_path) as ledger:
        ledger.start_run(run_id, started_at, config.source.marker_id, config.archive.marker_id)
        try:
            batch: List[FileMetadata] = []
            for item in inventory(config.source.path, config.source.marker_file):
                batch.append(item); file_count += 1; total_bytes += item.size
                if len(batch) >= 1000:
                    ledger.stage_files(run_id, batch); batch = []
            if batch:
                ledger.stage_files(run_id, batch)
            completed_at = now().isoformat()
            ledger.complete_scan(run_id, completed_at, file_count, total_bytes, warnings)
            counts = ledger.change_counts(run_id)
        except Exception as exc:
            ledger.fail_run(run_id, now().isoformat(), str(exc))
            if isinstance(exc, LedgerError):
                raise
            raise LedgerError(f"catalogue scan failed: {exc}") from exc
    summary_path = config.manifests_root / f"scan-{run_id}.json"
    summary = {"schema_version": 2, "run_id": run_id, "status": "success", "started_at": started_at,
               "completed_at": completed_at, "source_identity": config.source.marker_id,
               "destination_identity": config.archive.marker_id, "catalogue_file_count": file_count,
               "catalogue_total_bytes": total_bytes, "changed_files": counts["added"] + counts["modified"],
               "deleted_paths": counts["deleted"], "warnings": warnings}
    try:
        _atomic_json(summary_path, summary)
        _atomic_json(config.manifests_root / "latest-scan.json", summary)
    except OSError as exc:
        raise LedgerError(f"could not write scan summary: {exc}") from exc
    return ScanResult(run_id, "success", file_count, total_bytes, counts["added"] + counts["modified"],
                      counts["deleted"], tuple(warnings), summary_path)
