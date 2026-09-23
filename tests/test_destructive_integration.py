"""Filesystem integration scenarios confined to pytest's temporary directory.

The runner substitutes only rclone, which is not available on every developer
machine. Planning, safety, execution reconciliation, verification and the
ledger use their production implementations.
"""

import json
import os
import shutil
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from gorbackup.backup import BackupError, run_backup
from gorbackup.config import (
    AppConfig, ArchiveConfig, LoggingConfig, RenameOptimizationConfig,
    RetentionConfig, SafetyConfig, SourceConfig, StateConfig,
)
from gorbackup.dependencies import RcloneInfo
from gorbackup.ledger import Ledger
from gorbackup.locking import BackupLock, LockError
from gorbackup.planner import create_plan
from gorbackup.preflight import PreflightError, run_preflight
from gorbackup.pruning import execute_prune, plan_prune
from gorbackup.recovery import restore_version
from gorbackup.safety import SafetyError, assess_plan_safety


NOW = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


class FixtureRclone:
    """Small local rclone boundary with real file moves and copies."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.fail_execution = False
        self.corrupt_execution = False

    def __call__(self, command, **kwargs):
        source, current = (Path(command[2]).resolve(), Path(command[3]).resolve())
        history = Path(command[command.index("--backup-dir") + 1]).resolve()
        assert source.is_relative_to(self.root)
        assert current.is_relative_to(self.root)
        assert history.is_relative_to(self.root)
        log = Path(command[command.index("--log-file") + 1])
        dry = "--dry-run" in command
        excluded = [command[index + 1].lstrip("/").replace("\\", "")
                    for index, value in enumerate(command[:-1]) if value == "--exclude"]
        source_files = {p.relative_to(source).as_posix(): p for p in source.rglob("*")
                        if p.is_file() and p.relative_to(source).as_posix() not in excluded}
        current_files = {p.relative_to(current).as_posix(): p for p in current.rglob("*")
                         if p.is_file()}
        combined = []
        events = []
        for name in sorted(source_files.keys() | current_files.keys()):
            incoming, existing = source_files.get(name), current_files.get(name)
            if incoming and existing and incoming.read_bytes() == existing.read_bytes():
                combined.append("= " + name)
                continue
            code = "*" if incoming and existing else "+" if incoming else "-"
            combined.append(f"{code} {name}")
            if dry or name in excluded:
                continue
            if existing:
                old_size = existing.stat().st_size
                archived = history / name
                archived.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(existing), str(archived))
                events.append({"level": "info", "msg": "Moved", "object": name,
                               "size": old_size})
            if incoming:
                target = current / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(incoming, target)
                if self.corrupt_execution:
                    target.write_bytes(b"corrupt")
                events.append({"level": "info", "msg": "Copied (replaced)" if existing
                               else "Copied (new)", "object": name,
                               "size": incoming.stat().st_size})
        if dry:
            Path(command[command.index("--combined") + 1]).write_text(
                "\n".join(combined) + "\n", encoding="utf-8")
        log.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        return subprocess.CompletedProcess(command, 1 if self.fail_execution and not dry else 0,
                                           "", "injected rclone failure")


@pytest.fixture
def sandbox(tmp_path):
    source = tmp_path / "source volume" / "catalogue"
    archive = tmp_path / "archive volume"
    source.mkdir(parents=True)
    (archive / "current").mkdir(parents=True)
    (archive / "history").mkdir()
    (source / ".source-id").write_text("source-id\n", encoding="utf-8")
    (archive / ".archive-id").write_text("archive-id\n", encoding="utf-8")
    config = AppConfig(
        SourceConfig(source, source.parent, ".source-id", "source-id"),
        ArchiveConfig(archive, "current", "history", ".archive-id", "archive-id"),
        SafetyConfig(10, 1, 0, 0, 0, 0.5,
                     max_source_file_count_drop_percent=100,
                     max_source_bytes_drop_percent=100,
                     max_changed_catalogue_percent=100),
        RetentionConfig(False, 0, 0),
        LoggingConfig(), StateConfig(Path("state")), RenameOptimizationConfig(False),
    )
    return config, FixtureRclone(tmp_path)


def put(config, name, content):
    path = config.source.path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (NOW.timestamp() - 86400, NOW.timestamp() - 86400))
    return path


def backup(config, runner, number, **kwargs):
    return run_backup(config, RcloneInfo("fixture-rclone", (1, 70, 0)),
                      runner=runner, now=lambda: NOW,
                      run_id_factory=lambda: f"backup-{number}", **kwargs)


def test_first_sync_update_delete_restore_and_unicode(sandbox, tmp_path):
    config, runner = sandbox
    name = "Campaña José/selección 東京 01.tif"
    original = put(config, name, b"original")
    first = backup(config, runner, 1)
    assert first.status == "success"
    assert (config.archive.root / "current" / name).read_bytes() == b"original"
    put(config, name, b"updated-longer")
    second = backup(config, runner, 2)
    assert second.status == "success"
    assert (second.history_path / name).read_bytes() == b"original"
    original.unlink()
    put(config, "new file.tif", b"new")
    third = backup(config, runner, 3)
    assert third.status == "success"
    assert not (config.archive.root / "current" / name).exists()
    assert (third.history_path / name).read_bytes() == b"updated-longer"
    restored_old = restore_version(config, name, second.run_id,
                                   destination=tmp_path / "restore old")
    restored_deleted = restore_version(config, name, third.run_id,
                                       destination=tmp_path / "restore deleted")
    assert restored_old.destination_path.read_bytes() == b"original"
    assert restored_deleted.destination_path.read_bytes() == b"updated-longer"
    with Ledger(config.ledger_path) as ledger:
        assert ledger.known_good_state()["run_id"] == third.run_id


def test_preflight_rejects_empty_and_wrong_markers(sandbox):
    config, _ = sandbox
    def check():
        return run_preflight(config, is_mount=lambda _: True,
                             same_filesystem=lambda *_: False)
    with pytest.raises(PreflightError, match="source contains no data files"):
        check()
    put(config, "safe.tif", b"safe")
    assert check().source.file_count == 1
    (config.source.path / ".source-id").write_text("wrong")
    with pytest.raises(PreflightError, match="source identity mismatch"):
        check()
    (config.source.path / ".source-id").write_text("source-id")
    (config.archive.root / ".archive-id").write_text("wrong")
    with pytest.raises(PreflightError, match="archive identity mismatch"):
        check()


def test_failed_execution_never_becomes_known_good(sandbox):
    config, runner = sandbox
    put(config, "safe.tif", b"safe")
    first = backup(config, runner, 1)
    put(config, "safe.tif", b"changed")
    runner.fail_execution = True
    with pytest.raises(BackupError, match="backup execution or verification failed"):
        backup(config, runner, 2)
    with Ledger(config.ledger_path) as ledger:
        assert ledger.known_good_state()["run_id"] == first.run_id


def test_checksum_mismatch_never_becomes_known_good(sandbox):
    config, runner = sandbox
    put(config, "safe.tif", b"safe")
    first = backup(config, runner, 1)
    put(config, "safe.tif", b"changed")
    runner.corrupt_execution = True
    with pytest.raises(BackupError, match="backup execution or verification failed"):
        backup(config, runner, 2)
    with Ledger(config.ledger_path) as ledger:
        assert ledger.known_good_state()["run_id"] == first.run_id


def test_prune_changes_only_fixture_history(sandbox):
    config, runner = sandbox
    name = "José/old.tif"
    put(config, name, b"old")
    backup(config, runner, 1)
    put(config, name, b"new-longer")
    second = backup(config, runner, 2)
    historical = second.history_path / name
    assert historical.read_bytes() == b"old"
    backup(config, runner, 3)  # Move known-good protection off backup-2.
    later = NOW + timedelta(days=1)
    plan = plan_prune(config, now=lambda: later, prune_id_factory=lambda: "prune-1")
    assert historical.exists()  # dry run is read only
    payload = json.loads(plan.manifest_path.read_text())
    history_root = (config.archive.root / "history").resolve()
    assert all((history_root / item["historical_path"]).resolve().is_relative_to(
        history_root) for item in payload["items"])
    execute_prune(config, plan.prune_id, yes=True, now=lambda: later)
    assert not historical.exists()
    assert (config.archive.root / "current" / name).read_bytes() == b"new-longer"


@pytest.mark.parametrize("old,new", [
    ("old.tif", "moved.tif"),
    ("old folder/José.tif", "new folder/José.tif"),
])
def test_rename_or_folder_move_preserves_original(sandbox, old, new):
    config, runner = sandbox
    source = put(config, old, b"same")
    backup(config, runner, 1)
    target = config.source.path / new
    target.parent.mkdir(parents=True, exist_ok=True)
    source.rename(target)
    result = backup(config, runner, 2)
    assert result.status == "success"
    assert (config.archive.root / "current" / new).read_bytes() == b"same"
    assert (result.history_path / old).read_bytes() == b"same"
    assert not (config.archive.root / "current" / old).exists()


def test_mass_delete_blocked_before_file_mutation(sandbox):
    config, runner = sandbox
    put(config, "one.tif", b"one")
    put(config, "two.tif", b"two")
    backup(config, runner, 1)
    (config.source.path / "one.tif").unlink()
    (config.source.path / "two.tif").unlink()
    guarded = replace(config, safety=replace(config.safety, max_deletes_per_run=1))
    with pytest.raises(SafetyError, match="planned deletions exceed"):
        backup(guarded, runner, 2)
    assert (config.archive.root / "current" / "one.tif").read_bytes() == b"one"
    assert (config.archive.root / "current" / "two.tif").read_bytes() == b"two"


def test_recent_file_is_ignored(sandbox):
    config, runner = sandbox
    recent_config = replace(config, safety=replace(config.safety, ignore_recent_minutes=15))
    path = put(config, "in progress.tif", b"unfinished")
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))
    plan = create_plan(recent_config, RcloneInfo("fixture-rclone", (1, 70, 0)),
                       runner=runner, now=lambda: NOW)
    assert plan.counts["skipped_recent"] == 1
    assert plan.planned_transfer_files == 0
    result = backup(recent_config, runner, 1)
    assert result.status == "success"
    assert not (config.archive.root / "current" / "in progress.tif").exists()


def test_concurrent_backup_stops_before_planning(sandbox):
    config, runner = sandbox
    put(config, "safe.tif", b"safe")
    with BackupLock(config.state_root / "backup.lock", run_id="owner", now=lambda: NOW):
        with pytest.raises(LockError):
            backup(config, runner, 1)
    assert not list((config.archive.root / "history").iterdir())


def test_insufficient_capacity_blocks_execution(sandbox):
    config, runner = sandbox
    put(config, "safe.tif", b"safe")
    def no_space(cfg, plan, **kwargs):
        return assess_plan_safety(cfg, plan,
                                  disk_usage=lambda _: SimpleNamespace(total=100, free=0),
                                  **kwargs)
    with pytest.raises(SafetyError, match="insufficient archive space"):
        backup(config, runner, 1, safety_checker=no_space)
    assert not (config.archive.root / "current" / "safe.tif").exists()
