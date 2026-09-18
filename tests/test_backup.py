import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gorbackup.backup import BackupError, run_backup
from gorbackup.config import (
    AppConfig,
    ArchiveConfig,
    LoggingConfig,
    RetentionConfig,
    SafetyConfig,
    SourceConfig,
    StateConfig,
)
from gorbackup.dependencies import RcloneInfo
from gorbackup.ledger import Ledger
from gorbackup.planner import PlanItem, PlanResult
from gorbackup.safety import SafetyAssessment, SafetyError

FIXED_TIME = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
APPROVED = SafetyAssessment(1, 5, 10, 100, 90, 90.0)


def make_config(tmp_path: Path) -> AppConfig:
    source = tmp_path / "source-volume" / "catalogue"
    archive = tmp_path / "archive-volume"
    source.mkdir(parents=True)
    (archive / "current").mkdir(parents=True)
    (archive / "history").mkdir()
    return AppConfig(
        SourceConfig(source, source.parent, ".source-id", "source-id"),
        ArchiveConfig(archive, "current", "history", ".archive-id", "archive-id"),
        SafetyConfig(10, 1, 10, 15, 0, 0.8),
        RetentionConfig(False),
        LoggingConfig(),
        StateConfig(Path("state")),
    )


def successful_plan(config: AppConfig) -> PlanResult:
    items = (
        PlanItem("new_file", "new.tif", 3, "missing from current"),
        PlanItem("changed_file", "changed.tif", 7, "differs", leaving_size=4),
        PlanItem("delete_from_current", "old.tif", 5, "old", leaving_size=5),
    )
    counts = {
        "new_file": 1,
        "changed_file": 1,
        "delete_from_current": 1,
        "rename_move_candidate": 0,
        "skipped_recent": 0,
        "error": 0,
    }
    return PlanResult(
        "plan-1",
        "success",
        items,
        counts,
        {},
        10,
        9,
        config.manifests_root / "plan-plan-1.json",
    )


def seed_plan(config: AppConfig) -> None:
    with Ledger(config.ledger_path) as ledger:
        ledger.start_run(
            "plan-1", FIXED_TIME.isoformat(), "source-id", "archive-id", "plan"
        )
        ledger.save_plan("plan-1", FIXED_TIME.isoformat(), "success", [], {}, {}, [], [])


def test_backup_executes_sync_with_unique_versioned_history(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    seed_plan(config)
    commands = []

    def runner(command, **kwargs):
        commands.append(command)
        Path(command[command.index("--log-file") + 1]).write_text(
            '{"level":"info","msg":"sync complete"}\n', encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    result = run_backup(
        config,
        RcloneInfo("rclone", (1, 70, 0)),
        runner=runner,
        now=lambda: FIXED_TIME,
        run_id_factory=lambda: "backup-1",
        planner=lambda *args, **kwargs: successful_plan(config),
        safety_checker=lambda *args: APPROVED,
    )

    command = commands[0]
    assert command[1] == "sync"
    assert "--dry-run" not in command
    assert command[command.index("--backup-dir") + 1] == str(
        config.archive.root / "history" / "backup-1"
    )
    assert command[command.index("--exclude") + 1] == "/.source-id"
    assert command[command.index("--max-delete") + 1] == "10"
    assert command[command.index("--max-delete-size") + 1] == str(1024**3) + "B"
    assert result.transferred_files == 2
    assert result.archived_files == 2
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert payload["plan_run_id"] == "plan-1"
    assert payload["archived_bytes"] == 9
    assert payload["safety"]["delete_count"] == 1
    with Ledger(config.ledger_path) as ledger:
        run = next(item for item in ledger.run_history() if item["run_id"] == "backup-1")
        assert run["operation"] == "backup"
        assert run["status"] == "success"
        assert run["bytes_copied"] == 10
        assert run["bytes_archived"] == 9


def test_backup_failure_is_recorded_and_has_no_success_manifest(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    seed_plan(config)

    def runner(command, **kwargs):
        Path(command[command.index("--log-file") + 1]).write_text(
            '{"level":"error","msg":"destination full"}\n', encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 1, "", "")

    with pytest.raises(BackupError, match="destination full"):
        run_backup(
            config,
            RcloneInfo("rclone", (1, 70, 0)),
            runner=runner,
            now=lambda: FIXED_TIME,
            run_id_factory=lambda: "backup-failed",
            planner=lambda *args, **kwargs: successful_plan(config),
            safety_checker=lambda *args: APPROVED,
        )

    assert not (config.manifests_root / "backup-backup-failed.json").exists()
    with Ledger(config.ledger_path) as ledger:
        run = next(
            item for item in ledger.run_history()
            if item["run_id"] == "backup-failed"
        )
        assert run["status"] == "failed"
        assert "destination full" in run["errors_json"]


def test_backup_refuses_to_reuse_history_directory(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    (config.archive.root / "history" / "duplicate").mkdir()

    with pytest.raises(BackupError, match="history target already exists"):
        run_backup(
            config,
            RcloneInfo("rclone", (1, 70, 0)),
            run_id_factory=lambda: "duplicate",
            planner=lambda *args, **kwargs: successful_plan(config),
            safety_checker=lambda *args: APPROVED,
        )


def test_safety_rejection_blocks_execution_and_history_reservation(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    def reject(*args):
        raise SafetyError(["planned deletions exceed limit"])

    def unexpected_runner(*args, **kwargs):
        raise AssertionError("rclone execution must not start")

    with pytest.raises(SafetyError, match="planned deletions exceed limit"):
        run_backup(
            config,
            RcloneInfo("rclone", (1, 70, 0)),
            runner=unexpected_runner,
            run_id_factory=lambda: "blocked",
            planner=lambda *args, **kwargs: successful_plan(config),
            safety_checker=reject,
        )

    assert not (config.archive.root / "history" / "blocked").exists()
    assert not config.ledger_path.exists()
