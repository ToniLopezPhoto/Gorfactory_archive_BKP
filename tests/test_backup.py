import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gorbackup.backup import (
    BackupError,
    ExecutionItem,
    parse_execution_log,
    reconcile_execution,
    run_backup,
)
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
from gorbackup.ledger import FileMetadata, Ledger
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
        3,
        15,
        2,
        10,
        2,
        9,
        config.manifests_root / "plan-plan-1.json",
    )


def seed_plan(config: AppConfig) -> None:
    plan = successful_plan(config)
    with Ledger(config.ledger_path) as ledger:
        ledger.start_run(
            "plan-1", FIXED_TIME.isoformat(), "source-id", "archive-id", "plan"
        )
        ledger.save_plan(
            "plan-1", FIXED_TIME.isoformat(), "success",
            [
                {
                    "category": item.category, "path": item.path,
                    "related_path": item.related_path, "size": item.size,
                    "leaving_size": item.leaving_size, "reason": item.reason,
                }
                for item in plan.items
            ],
            plan.counts,
            {"new_file": 3, "changed_file": 7, "delete_from_current": 5,
             "rename_move_candidate": 0, "skipped_recent": 0, "error": 0},
            [], [],
            [FileMetadata("new.tif", 3, 1), FileMetadata("changed.tif", 7, 1),
             FileMetadata("stable.tif", 5, 1)],
        )


def test_backup_executes_sync_with_unique_versioned_history(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    seed_plan(config)
    commands = []

    def runner(command, **kwargs):
        commands.append(command)
        history = Path(command[command.index("--backup-dir") + 1])
        (history / "changed.tif").write_bytes(b"old!")
        (history / "old.tif").write_bytes(b"older")
        Path(command[command.index("--log-file") + 1]).write_text(
            '{"level":"info","msg":"Copied (new)","object":"new.tif","size":3}\n'
            '{"level":"info","msg":"Moved (server-side)","object":"changed.tif","size":4}\n'
            '{"level":"info","msg":"Copied (replaced existing)","object":"changed.tif","size":7}\n'
            '{"level":"info","msg":"Moved (server-side)","object":"old.tif","size":5}\n',
            encoding="utf-8",
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
    assert payload["executed_archive_bytes"] == 9
    assert payload["safety"]["delete_count"] == 1
    with Ledger(config.ledger_path) as ledger:
        run = next(item for item in ledger.run_history() if item["run_id"] == "backup-1")
        assert run["operation"] == "backup"
        assert run["status"] == "success"
        assert run["planned_transfer_bytes"] == 10
        assert run["executed_transfer_bytes"] == 10
        assert run["executed_archive_bytes"] == 9
        assert ledger.known_good_state()["run_id"] == "backup-1"


def test_backup_failure_is_recorded_and_has_no_success_manifest(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    seed_plan(config)

    def runner(command, **kwargs):
        Path(command[command.index("--log-file") + 1]).write_text(
            '{"level":"error","msg":"destination full"}\n', encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 1, "", "")

    with pytest.raises(BackupError, match="could not be reconciled"):
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


def test_execution_log_is_machine_readable_and_rejects_ambiguous_events() -> None:
    items, warnings, errors = parse_execution_log([
        '{"level":"info","msg":"Copied (new)","object":"new.tif","size":3}',
        '{"level":"info","gorbackup_operation":"archive",'
        '"gorbackup_classification":"versioned","object":"old.tif","size":5}',
        'not-json',
    ])

    assert [(item.operation, item.classification, item.path, item.size) for item in items] == [
        ("transfer", "new", "new.tif", 3),
        ("archive", "versioned", "old.tif", 5),
    ]
    assert warnings == []
    assert errors == ["invalid JSON execution log line 3"]


def test_reported_archive_must_exist_in_history(tmp_path: Path) -> None:
    from gorbackup.backup import audit_history

    history = tmp_path / "history"
    history.mkdir()
    divergences = audit_history(
        history, [ExecutionItem("archive", "versioned", "missing.tif", 5, "Moved")]
    )

    assert divergences[0].divergence_type == "archive_missing_from_history"
    assert divergences[0].severity == "failure"


@pytest.mark.parametrize(
    ("case", "executed", "divergence_type", "severity"),
    [
        ("new file", [ExecutionItem("transfer", "new", "late.tif", 2, "Copied (new)")], "unplanned_execution", "warning"),
        ("deleted file", [], "planned_not_executed", "warning"),
        ("modified file", [ExecutionItem("transfer", "new", "new.tif", 4, "Copied (new)")], "byte_mismatch", "failure"),
        ("became eligible", [ExecutionItem("transfer", "new", "recent.tif", 6, "Copied (new)")], "unplanned_execution", "warning"),
    ],
)
def test_source_changes_between_plan_and_execution_are_classified(
    tmp_path: Path, case: str, executed, divergence_type: str, severity: str
) -> None:
    config = make_config(tmp_path)
    source = config.source.path
    (source / "new.tif").write_bytes(b"new")
    if case == "new file":
        (source / "late.tif").write_bytes(b"xx")
    elif case == "deleted file":
        (source / "new.tif").unlink()
    elif case == "modified file":
        (source / "new.tif").write_bytes(b"four")
    else:
        recent = source / "recent.tif"
        recent.write_bytes(b"recent")
        os.utime(recent, (1, 1))  # deliberately ages out of the recent-file window
    plan = successful_plan(config)
    if case in {"new file", "became eligible"}:
        # Retain the exact planned evidence and append the newly observed action.
        executed = [
            ExecutionItem("transfer", "new", "new.tif", 3, "Copied (new)"),
            ExecutionItem("archive", "versioned", "changed.tif", 4, "Moved"),
            ExecutionItem("transfer", "replaced", "changed.tif", 7, "Copied"),
            ExecutionItem("archive", "versioned", "old.tif", 5, "Moved"),
        ] + executed
    divergences = reconcile_execution(plan, executed)

    assert any(
        item.divergence_type == divergence_type and item.severity == severity
        for item in divergences
    ), case


def test_warning_run_does_not_advance_known_good_state(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    seed_plan(config)

    def runner(command, **kwargs):
        Path(command[command.index("--log-file") + 1]).write_text(
            '{"level":"info","msg":"Copied (new)","object":"late.tif","size":2}\n',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    result = run_backup(
        config, RcloneInfo("rclone", (1, 70, 0)), runner=runner,
        now=lambda: FIXED_TIME, run_id_factory=lambda: "warning-run",
        planner=lambda *args, **kwargs: successful_plan(config),
        safety_checker=lambda *args: APPROVED,
    )

    assert result.status == "warning"
    with Ledger(config.ledger_path) as ledger:
        assert ledger.known_good_state() is None
