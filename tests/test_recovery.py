import sqlite3
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gorbackup.config import (
    AppConfig, ArchiveConfig, LoggingConfig, RetentionConfig, SafetyConfig,
    SourceConfig, StateConfig,
)
from gorbackup.ledger import Ledger
from gorbackup.recovery import RecoveryError, find_history, restore_version


FIXED = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def make_config(tmp_path: Path) -> AppConfig:
    source = tmp_path / "sam" / "catalogue"
    archive = tmp_path / "archive"
    source.mkdir(parents=True)
    (archive / "current").mkdir(parents=True)
    (archive / "history").mkdir()
    return AppConfig(
        SourceConfig(source, source.parent, ".source-id", "source-id"),
        ArchiveConfig(archive, "current", "history", ".archive-id", "archive-id"),
        SafetyConfig(10, 1, 10, 5, 0, 0.8), RetentionConfig(False),
        LoggingConfig(), StateConfig(Path("state")),
    )


def seed_version(config: AppConfig, run_id: str, relative: str, content: bytes, *,
                 timestamp: str, status: str = "success",
                 category: str = "changed_file", physical: bool = True) -> Path:
    history = config.archive.root / config.archive.history_dir / run_id
    path = history / relative
    if physical:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    with Ledger(config.ledger_path) as ledger:
        connection = ledger.connection
        with connection:
            connection.execute(
                "INSERT INTO runs (run_id, operation, started_at, completed_at, status, source_identity, destination_identity) VALUES (?, 'plan', ?, ?, 'success', 'source-id', 'archive-id')",
                (f"plan-{run_id}", timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO plans VALUES (?, ?, 'success', '{}', '{}')",
                (f"plan-{run_id}", timestamp),
            )
            connection.execute(
                "INSERT INTO plan_items (run_id, category, path, size, leaving_size, reason) VALUES (?, ?, ?, ?, ?, 'test')",
                (f"plan-{run_id}", category, relative, len(content), len(content)),
            )
            connection.execute(
                "INSERT INTO runs (run_id, operation, started_at, completed_at, status, source_identity, destination_identity) VALUES (?, 'backup', ?, ?, ?, 'source-id', 'archive-id')",
                (run_id, timestamp, timestamp, status),
            )
            connection.execute(
                "INSERT INTO executions VALUES (?, ?, ?, 'report.json', 'exact')",
                (run_id, f"plan-{run_id}", str(history)),
            )
            connection.execute(
                "INSERT INTO execution_items (run_id, operation, classification, path, size, message) VALUES (?, 'archive', 'versioned', ?, ?, 'Moved')",
                (run_id, relative, len(content)),
            )
    return path


def test_history_shows_current_and_orders_multiple_versions(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    relative = "Campaign/photo.tif"
    current = config.archive.root / "current" / relative
    current.parent.mkdir(parents=True)
    current.write_bytes(b"new")
    seed_version(config, "old-run", relative, b"old", timestamp="2026-09-14T12:00:00+00:00")
    seed_version(config, "new-run", relative, b"middle", timestamp="2026-09-20T12:00:00+00:00")

    result = find_history(config, relative)

    assert result.current is not None and result.current.size == 3
    assert [item.run_id for item in result.versions] == ["new-run", "old-run"]
    assert all(item.reason == "overwritten" for item in result.versions)


def test_deleted_unicode_file_is_discovered_and_restored(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    relative = "Campaña Otoño/Selección José/DSC 001.tif"
    historical = seed_version(
        config, "delete-run", relative, b"original pixels",
        timestamp="2026-09-20T12:00:00+00:00", category="delete_from_current",
    )

    history = find_history(config, relative)
    restored = restore_version(
        config, relative, "delete-run", now=lambda: FIXED,
        restore_id_factory=lambda: "restore-1",
    )

    assert history.current is None
    assert history.versions[0].reason == "deleted"
    assert restored.destination_path.read_bytes() == b"original pixels"
    assert historical.read_bytes() == b"original pixels"
    assert restored.destination_path == config.archive.root / "recovery" / "restore-1" / relative
    with Ledger(config.ledger_path) as ledger:
        row = ledger.restore_history()[0]
        assert row["status"] == "success"
        assert row["verification_method"] == "sha256"


def test_restore_selects_old_version_without_changing_current(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    relative = "photo.tif"
    current = config.archive.root / "current" / relative
    current.write_bytes(b"new")
    seed_version(config, "old-run", relative, b"old", timestamp="2026-09-14T12:00:00+00:00")
    seed_version(config, "middle-run", relative, b"middle", timestamp="2026-09-20T12:00:00+00:00")

    result = restore_version(
        config, relative, "old-run", now=lambda: FIXED,
        restore_id_factory=lambda: "restore-old",
    )

    assert result.destination_path.read_bytes() == b"old"
    assert current.read_bytes() == b"new"


def test_missing_history_is_reported_and_restore_creates_no_output(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    seed_version(config, "missing-run", "gone.tif", b"gone", physical=False,
                 timestamp="2026-09-20T12:00:00+00:00")

    result = find_history(config, "gone.tif")
    assert result.versions[0].exists is False
    with pytest.raises(RecoveryError, match="recorded but missing"):
        restore_version(config, "gone.tif", "missing-run")
    assert not (config.archive.root / "recovery").exists()


def test_conflict_does_not_overwrite_existing_recovery(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    seed_version(config, "run-1", "photo.tif", b"history",
                 timestamp="2026-09-20T12:00:00+00:00")
    output = config.archive.root / "recovery" / "same-id" / "photo.tif"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"keep")

    with pytest.raises(RecoveryError, match="refusing to overwrite"):
        restore_version(config, "photo.tif", "run-1",
                        restore_id_factory=lambda: "same-id")
    assert output.read_bytes() == b"keep"


@pytest.mark.parametrize("location", ["source", "history", "current", "state"])
def test_protected_destinations_are_rejected(tmp_path: Path, location: str) -> None:
    config = make_config(tmp_path)
    seed_version(config, "run-1", "photo.tif", b"history",
                 timestamp="2026-09-20T12:00:00+00:00")
    roots = {
        "source": config.source.path,
        "history": config.archive.root / "history",
        "current": config.archive.root / "current",
        "state": config.state_root,
    }
    with pytest.raises(RecoveryError, match="destination"):
        restore_version(config, "photo.tif", "run-1", destination=roots[location])


@pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd", "", "a\\b"])
def test_unsafe_relative_paths_are_rejected(tmp_path: Path, path: str) -> None:
    config = make_config(tmp_path)
    with pytest.raises(RecoveryError, match="unsafe relative path"):
        find_history(config, path)


@pytest.mark.parametrize("run_id", ["../run", "/run", "run/id", "run id"])
def test_malicious_run_ids_are_rejected(tmp_path: Path, run_id: str) -> None:
    config = make_config(tmp_path)
    with pytest.raises(RecoveryError, match="unsafe run id"):
        restore_version(config, "photo.tif", run_id)


def test_corruption_and_partial_copy_fail_without_final_output(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    seed_version(config, "run-corrupt", "photo.tif", b"original",
                 timestamp="2026-09-20T12:00:00+00:00")

    def corrupt(source: Path, target: Path) -> int:
        target.write_bytes(b"corrupt!")
        return 8

    with pytest.raises(RecoveryError, match="verification failed"):
        restore_version(
            config, "photo.tif", "run-corrupt", copier=corrupt,
            restore_id_factory=lambda: "corrupt-id", now=lambda: FIXED,
        )
    final = config.archive.root / "recovery" / "corrupt-id" / "photo.tif"
    assert not final.exists()
    assert final.with_name(".photo.tif.gorbackup-restore-corrupt-id.tmp").exists()

    with Ledger(config.ledger_path) as ledger:
        assert ledger.restore_history()[0]["status"] == "failed"


def test_copy_exception_leaves_diagnostic_temp_and_failed_ledger(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    seed_version(config, "run-partial", "photo.tif", b"original",
                 timestamp="2026-09-20T12:00:00+00:00")

    def partial(source: Path, target: Path) -> int:
        target.write_bytes(b"part")
        raise OSError("disk full")

    with pytest.raises(RecoveryError, match="disk full"):
        restore_version(
            config, "photo.tif", "run-partial", copier=partial,
            restore_id_factory=lambda: "partial-id", now=lambda: FIXED,
        )
    final = config.archive.root / "recovery" / "partial-id" / "photo.tif"
    assert not final.exists()
    assert final.with_name(".photo.tif.gorbackup-restore-partial-id.tmp").exists()
    with Ledger(config.ledger_path) as ledger:
        assert ledger.restore_history()[0]["status"] == "failed"


def test_restore_does_not_change_known_good(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    seed_version(config, "run-1", "photo.tif", b"history",
                 timestamp="2026-09-20T12:00:00+00:00")
    with Ledger(config.ledger_path) as ledger:
        before = ledger.known_good_state()
    restore_version(config, "photo.tif", "run-1",
                    restore_id_factory=lambda: "restore-1", now=lambda: FIXED)
    with Ledger(config.ledger_path) as ledger:
        assert ledger.known_good_state() == before


def test_persisted_historical_sha256_is_checked(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    path = seed_version(config, "run-1", "photo.tif", b"before!",
                        timestamp="2026-09-20T12:00:00+00:00")
    expected = hashlib.sha256(b"before!").hexdigest()
    with Ledger(config.ledger_path) as ledger:
        with ledger.connection:
            ledger.connection.execute(
                "UPDATE execution_items SET checksum=? WHERE run_id='run-1'",
                (f"sha256:{expected}",),
            )
    path.write_bytes(b"changed")  # same size, so only the version-specific hash detects drift

    with pytest.raises(RecoveryError, match="persisted SHA-256"):
        restore_version(config, "photo.tif", "run-1",
                        restore_id_factory=lambda: "restore-checksum")


def test_symlinked_history_root_escape_is_rejected(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (config.archive.root / "history").rmdir()
    (config.archive.root / "history").symlink_to(outside, target_is_directory=True)
    with Ledger(config.ledger_path):
        pass

    with pytest.raises(RecoveryError, match="escapes archive root"):
        find_history(config, "photo.tif")


def test_read_only_history_does_not_migrate_v4_ledger(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    with Ledger(config.ledger_path):
        pass
    connection = sqlite3.connect(config.ledger_path)
    connection.execute("PRAGMA user_version = 4")
    connection.commit()
    connection.close()

    with Ledger(config.ledger_path, read_only=True) as ledger:
        assert ledger.connection.execute("PRAGMA user_version").fetchone()[0] == 4
    connection = sqlite3.connect(config.ledger_path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
    connection.close()
