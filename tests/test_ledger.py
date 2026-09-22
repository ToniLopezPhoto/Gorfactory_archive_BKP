import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gorbackup.config import (
    AppConfig,
    ArchiveConfig,
    LoggingConfig,
    RetentionConfig,
    SafetyConfig,
    SourceConfig,
    StateConfig,
)
from gorbackup.ledger import Ledger, LedgerError, scan_catalogue


FIXED_TIME = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def make_config(tmp_path: Path) -> AppConfig:
    source = tmp_path / "source-volume" / "catalogue"
    archive = tmp_path / "archive-volume"
    source.mkdir(parents=True)
    archive.mkdir()
    return AppConfig(
        SourceConfig(source, source.parent, ".source-id", "source-identity"),
        ArchiveConfig(
            archive, "current", "history", ".archive-id", "archive-identity"
        ),
        SafetyConfig(10, 1, 10, 5, 0, 0.8),
        RetentionConfig(False),
        LoggingConfig(),
        StateConfig(Path("state")),
    )


def run_scan(config: AppConfig, run_id: str):
    return scan_catalogue(
        config,
        now=lambda: FIXED_TIME,
        run_id_factory=lambda: run_id,
    )


def test_scan_inventories_metadata_and_answers_required_queries(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    (config.source.path / "a.tif").write_bytes(b"aaa")
    (config.source.path / "folder").mkdir()
    (config.source.path / "folder" / "b.jpg").write_bytes(b"bb")
    (config.source.path / config.source.marker_file).write_text(
        config.source.marker_id, encoding="utf-8"
    )

    result = run_scan(config, "run-1")

    assert result.catalogue_file_count == 2
    assert result.catalogue_total_bytes == 5
    assert result.changed_files == 2
    assert result.deleted_paths == 0
    assert result.summary_path.parent == config.manifests_root
    assert json.loads(result.summary_path.read_text(encoding="utf-8"))["status"] == "success"
    with Ledger(config.ledger_path) as ledger:
        latest = ledger.latest_successful_run()
        assert latest is not None and latest["run_id"] == "run-1"
        assert ledger.totals() == {"catalogue_file_count": 2, "catalogue_total_bytes": 5}
        assert [item["relative_path"] for item in ledger.changed_files("run-1")] == [
            "a.tif",
            "folder/b.jpg",
        ]
        assert ledger.deleted_paths("run-1") == []
        assert ledger.run_history()[0]["status"] == "success"


def test_later_scan_records_modified_added_and_deleted_paths(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    first = config.source.path / "a.tif"
    deleted = config.source.path / "old.jpg"
    first.write_bytes(b"a")
    deleted.write_bytes(b"old")
    run_scan(config, "run-1")

    first.write_bytes(b"changed")
    deleted.unlink()
    (config.source.path / "new.png").write_bytes(b"new")
    result = run_scan(config, "run-2")

    assert result.changed_files == 2
    assert result.deleted_paths == 1
    with Ledger(config.ledger_path) as ledger:
        changes = {
            item["relative_path"]: item["change_type"]
            for item in ledger.changed_files("run-2")
        }
        assert changes == {"a.tif": "modified", "new.png": "added"}
        assert ledger.deleted_paths("run-2") == ["old.jpg"]
        assert len(ledger.run_history()) == 2


def test_failed_scan_does_not_replace_last_known_good_state(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    (config.source.path / "safe.tif").write_bytes(b"safe")
    run_scan(config, "good-run")

    def failing_inventory(source: Path, marker: str):
        yield from ()
        raise OSError("SAM disconnected during scan")

    with pytest.raises(LedgerError, match="SAM disconnected"):
        scan_catalogue(
            config,
            now=lambda: FIXED_TIME,
            run_id_factory=lambda: "failed-run",
            inventory=failing_inventory,
        )

    with Ledger(config.ledger_path) as ledger:
        assert ledger.totals() == {"catalogue_file_count": 1, "catalogue_total_bytes": 4}
        latest = ledger.latest_successful_run()
        assert latest is not None and latest["run_id"] == "good-run"
        history = ledger.run_history()
        failed = next(item for item in history if item["run_id"] == "failed-run")
        assert failed["status"] == "failed"
        assert "SAM disconnected" in failed["errors_json"]


def test_unchanged_file_keeps_known_optional_checksum(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    (config.source.path / "photo.tif").write_bytes(b"image")
    run_scan(config, "run-1")
    with Ledger(config.ledger_path) as ledger:
        with ledger.connection:
            ledger.connection.execute(
                "UPDATE catalogue_files SET checksum = 'sha1:known' WHERE relative_path = 'photo.tif'"
            )

    run_scan(config, "run-2")

    with Ledger(config.ledger_path) as ledger:
        row = ledger.connection.execute(
            "SELECT checksum FROM catalogue_files WHERE relative_path = 'photo.tif'"
        ).fetchone()
        assert row["checksum"] == "sha1:known"
        assert ledger.changed_files("run-2") == []


def test_state_database_must_live_under_archive(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config = AppConfig(
        config.source,
        config.archive,
        config.safety,
        config.retention,
        config.logging,
        StateConfig(tmp_path / "outside-state"),
    )

    with pytest.raises(LedgerError, match="must live under archive"):
        run_scan(config, "unsafe-run")


def test_v1_ledger_is_migrated_to_clean_unambiguous_schema(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(str(path))
    connection.executescript(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, file_count INTEGER, total_bytes INTEGER
        );
        INSERT INTO runs VALUES ('legacy', 2, 5);
        CREATE TABLE current_files (relative_path TEXT PRIMARY KEY);
        """
    )
    connection.close()

    with Ledger(path) as ledger:
        columns = {
            row["name"]
            for row in ledger.connection.execute("PRAGMA table_info(runs)").fetchall()
        }
        assert ledger.connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert "catalogue_file_count" in columns
        assert "planned_transfer_bytes" in columns
        assert "executed_archive_bytes" in columns
        assert "total_bytes" not in columns
        assert ledger.run_history() == []


def test_v4_migration_preserves_backup_evidence_and_adds_restore_tables(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v4.sqlite3"
    with Ledger(path) as ledger:
        ledger.start_run("kept-run", FIXED_TIME.isoformat(), "source", "archive")
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        DROP INDEX idx_restore_runs_started;
        DROP TABLE restore_runs;
        ALTER TABLE execution_items DROP COLUMN checksum;
        PRAGMA user_version = 4;
        """
    )
    connection.close()

    with Ledger(path) as ledger:
        assert ledger.connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert ledger.run_history()[0]["run_id"] == "kept-run"
        columns = {
            row["name"]
            for row in ledger.connection.execute(
                "PRAGMA table_info(execution_items)"
            ).fetchall()
        }
        assert "checksum" in columns
        assert ledger.restore_history() == []
