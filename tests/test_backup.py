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
from gorbackup.locking import BackupLock, LockError
from gorbackup.planner import PlanItem, PlanResult
from gorbackup.safety import GateFailure, SafetyAssessment, SafetyError

FIXED_TIME = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
APPROVED = SafetyAssessment(1, 5, 10, 100, 90, 90.0)


def rejected_assessment() -> SafetyAssessment:
    failure = GateFailure(
        "high_change_count", 5000, 2000, "files",
        "planned changed files exceed safety.max_changed_files_per_run", True,
    )
    return SafetyAssessment(1, 5, 10, 100, 90, 90.0, failures=(failure,))


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
        safety_checker=lambda *args, **kwargs: APPROVED,
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
    assert not (config.state_root / "backup.lock").exists()


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
            safety_checker=lambda *args, **kwargs: APPROVED,
        )

    assert not (config.manifests_root / "backup-backup-failed.json").exists()
    with Ledger(config.ledger_path) as ledger:
        run = next(
            item for item in ledger.run_history()
            if item["run_id"] == "backup-failed"
        )
        assert run["status"] == "failed"
        assert "destination full" in run["errors_json"]
    assert not (config.state_root / "backup.lock").exists()


def test_backup_refuses_to_reuse_history_directory(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    seed_plan(config)
    (config.archive.root / "history" / "duplicate").mkdir()

    with pytest.raises(BackupError, match="history target already exists"):
        run_backup(
            config,
            RcloneInfo("rclone", (1, 70, 0)),
            run_id_factory=lambda: "duplicate",
            planner=lambda *args, **kwargs: successful_plan(config),
            safety_checker=lambda *args, **kwargs: APPROVED,
        )


def test_safety_rejection_blocks_execution_and_history_reservation(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    seed_plan(config)

    def reject(*args, **kwargs):
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
    with Ledger(config.ledger_path) as ledger:
        run = next(item for item in ledger.run_history() if item["run_id"] == "blocked")
        assert run["status"] == "blocked"
        assessment = ledger.safety_assessment("blocked")
        assert assessment is not None
        assert "planned deletions exceed limit" in assessment["message"]
        assert ledger.known_good_state() is None
    assert not (config.state_root / "backup.lock").exists()


def test_active_lock_stops_before_planner_and_creates_no_run(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    active = BackupLock(config.state_root / "backup.lock", run_id="other")
    active.acquire()
    called = False

    def unexpected_planner(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("planner must not run")

    try:
        with pytest.raises(LockError, match="backup already running"):
            run_backup(
                config, RcloneInfo("rclone", (1, 70, 0)), planner=unexpected_planner
            )
        assert called is False
        assert not config.ledger_path.exists()
    finally:
        active.release()


def test_recent_metadata_does_not_replace_last_protected_version(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    seed_plan(config)
    with Ledger(config.ledger_path) as ledger:
        with ledger.connection:
            ledger.connection.execute(
                "INSERT INTO plan_items (run_id, category, path, size, leaving_size, reason) "
                "VALUES ('plan-1', 'skipped_recent', 'stable.tif', 5, 0, 'recent')"
            )
            ledger.connection.execute(
                "INSERT INTO known_good_files VALUES ('stable.tif', 4, 0, NULL, 'plan-1')"
            )
            ledger.connection.execute(
                "INSERT INTO known_good_state VALUES (1, 'plan-1', 1, 4, ?)",
                (FIXED_TIME.isoformat(),),
            )

    def runner(command, **kwargs):
        excludes = [
            command[index + 1]
            for index, value in enumerate(command)
            if value == "--exclude"
        ]
        assert "/stable.tif" in excludes
        history = Path(command[command.index("--backup-dir") + 1])
        (history / "changed.tif").write_bytes(b"old!")
        (history / "old.tif").write_bytes(b"older")
        Path(command[command.index("--log-file") + 1]).write_text(
            '{"level":"info","msg":"Copied (new)","object":"new.tif","size":3}\n'
            '{"level":"info","msg":"Moved","object":"changed.tif","size":4}\n'
            '{"level":"info","msg":"Copied (replaced)","object":"changed.tif","size":7}\n'
            '{"level":"info","msg":"Moved","object":"old.tif","size":5}\n',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    plan = successful_plan(config)
    plan = PlanResult(
        plan.run_id, plan.status,
        plan.items + (PlanItem("skipped_recent", "stable.tif", 5, "recent"),),
        {**plan.counts, "skipped_recent": 1}, plan.byte_totals,
        plan.catalogue_file_count, plan.catalogue_total_bytes,
        plan.planned_transfer_files, plan.planned_transfer_bytes,
        plan.planned_archive_files, plan.planned_archive_bytes, plan.manifest_path,
    )
    run_backup(
        config, RcloneInfo("rclone", (1, 70, 0)), runner=runner,
        now=lambda: FIXED_TIME, run_id_factory=lambda: "backup-recent",
        planner=lambda *args, **kwargs: plan,
        safety_checker=lambda *args, **kwargs: APPROVED,
    )
    with Ledger(config.ledger_path) as ledger:
        row = ledger.connection.execute(
            "SELECT size, mtime_ns FROM known_good_files WHERE relative_path='stable.tif'"
        ).fetchone()
        assert tuple(row) == (4, 0)
        state = ledger.known_good_state()
        assert state["catalogue_file_count"] == 3
        assert state["catalogue_total_bytes"] == 14


def test_skipped_recent_is_not_expected_execution_or_divergence(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    plan = successful_plan(config)
    recent_only = PlanResult(
        plan.run_id, plan.status,
        (PlanItem("skipped_recent", "recent.tif", 6, "recent"),),
        {name: (1 if name == "skipped_recent" else 0) for name in plan.counts},
        {}, 1, 6, 0, 0, 0, 0, plan.manifest_path,
    )
    assert reconcile_execution(recent_only, []) == ()


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
        safety_checker=lambda *args, **kwargs: APPROVED,
    )

    assert result.status == "warning"
    with Ledger(config.ledger_path) as ledger:
        assert ledger.known_good_state() is None


def test_manual_override_is_audited_and_allows_only_volume_gate(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    seed_plan(config)

    def reject(*args, **kwargs):
        assessment = rejected_assessment()
        raise SafetyError([assessment.failures[0].message], assessment)

    def runner(command, **kwargs):
        history = Path(command[command.index("--backup-dir") + 1])
        (history / "changed.tif").write_bytes(b"old!")
        (history / "old.tif").write_bytes(b"older")
        Path(command[command.index("--log-file") + 1]).write_text(
            '{"level":"info","msg":"Copied (new)","object":"new.tif","size":3}\n'
            '{"level":"info","msg":"Moved","object":"changed.tif","size":4}\n'
            '{"level":"info","msg":"Copied (replaced)","object":"changed.tif","size":7}\n'
            '{"level":"info","msg":"Moved","object":"old.tif","size":5}\n',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    result = run_backup(
        config, RcloneInfo("rclone", (1, 70, 0)), runner=runner,
        now=lambda: FIXED_TIME, run_id_factory=lambda: "overridden",
        planner=lambda *args, **kwargs: successful_plan(config),
        safety_checker=reject, override_safety=True, manual_context=True,
    )

    assert result.status == "success"
    with Ledger(config.ledger_path) as ledger:
        audit = ledger.safety_assessment("overridden")
        assert audit["override_requested"] == 1
        assert audit["override_used"] == 1
        assert json.loads(audit["failed_gates_json"]) == ["high_change_count"]
        assert json.loads(audit["overridden_gates_json"]) == ["high_change_count"]


def test_override_is_impossible_without_manual_context(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    with pytest.raises(BackupError, match="interactive manual session"):
        run_backup(
            config, RcloneInfo("rclone", (1, 70, 0)),
            override_safety=True, manual_context=False,
            planner=lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("planning must not start")
            ),
        )


def test_non_overridable_capacity_gate_stays_blocked(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    seed_plan(config)
    failure = GateFailure("archive_capacity", 10, 5, "bytes", "insufficient space", False)
    assessment = SafetyAssessment(0, 0, 10, 5, -5, -1.0, failures=(failure,))

    def reject(*args, **kwargs):
        raise SafetyError([failure.message], assessment)

    with pytest.raises(SafetyError, match="insufficient space"):
        run_backup(
            config, RcloneInfo("rclone", (1, 70, 0)),
            run_id_factory=lambda: "capacity-blocked",
            planner=lambda *args, **kwargs: successful_plan(config),
            safety_checker=reject, override_safety=True, manual_context=True,
        )
    with Ledger(config.ledger_path) as ledger:
        assert ledger.safety_assessment("capacity-blocked")["override_used"] == 0
        run = next(r for r in ledger.run_history() if r["run_id"] == "capacity-blocked")
        assert run["status"] == "blocked"


def test_successful_run_after_blocked_run_promotes_known_good(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    seed_plan(config)
    assessment = rejected_assessment()

    with pytest.raises(SafetyError):
        run_backup(
            config, RcloneInfo("rclone", (1, 70, 0)),
            run_id_factory=lambda: "first-blocked",
            planner=lambda *args, **kwargs: successful_plan(config),
            safety_checker=lambda *args, **kwargs: (_ for _ in ()).throw(
                SafetyError([assessment.failures[0].message], assessment)
            ),
        )

    def runner(command, **kwargs):
        history = Path(command[command.index("--backup-dir") + 1])
        (history / "changed.tif").write_bytes(b"old!")
        (history / "old.tif").write_bytes(b"older")
        Path(command[command.index("--log-file") + 1]).write_text(
            '{"level":"info","msg":"Copied (new)","object":"new.tif","size":3}\n'
            '{"level":"info","msg":"Moved","object":"changed.tif","size":4}\n'
            '{"level":"info","msg":"Copied (replaced)","object":"changed.tif","size":7}\n'
            '{"level":"info","msg":"Moved","object":"old.tif","size":5}\n',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    result = run_backup(
        config, RcloneInfo("rclone", (1, 70, 0)), runner=runner,
        now=lambda: FIXED_TIME, run_id_factory=lambda: "then-success",
        planner=lambda *args, **kwargs: successful_plan(config),
        safety_checker=lambda *args, **kwargs: APPROVED,
    )
    assert result.status == "success"
    with Ledger(config.ledger_path) as ledger:
        assert ledger.known_good_state()["run_id"] == "then-success"
        statuses = {row["run_id"]: row["status"] for row in ledger.run_history()}
        assert statuses["first-blocked"] == "blocked"


@pytest.mark.parametrize("with_known_good", [False, True])
def test_safety_reference_prefers_known_good_then_baseline(
    tmp_path: Path, with_known_good: bool
) -> None:
    config = make_config(tmp_path)
    seed_plan(config)
    config.manifests_root.mkdir(parents=True, exist_ok=True)
    (config.manifests_root / config.state.baseline_manifest).write_text(
        json.dumps({
            "status": "known-good",
            "source": {"file_count": 99, "total_size_bytes": 999},
        }),
        encoding="utf-8",
    )
    if with_known_good:
        with Ledger(config.ledger_path) as ledger, ledger.connection:
            ledger.connection.execute(
                """INSERT INTO known_good_state
                   (singleton, run_id, catalogue_file_count,
                    catalogue_total_bytes, promoted_at)
                   VALUES (1, 'plan-1', 3, 15, ?)""",
                (FIXED_TIME.isoformat(),),
            )
    seen = []
    assessment = rejected_assessment()

    def reject(*args, **kwargs):
        seen.append(kwargs["reference"])
        raise SafetyError([assessment.failures[0].message], assessment)

    with pytest.raises(SafetyError):
        run_backup(
            config, RcloneInfo("rclone", (1, 70, 0)),
            run_id_factory=lambda: "reference-blocked",
            planner=lambda *args, **kwargs: successful_plan(config),
            safety_checker=reject,
        )

    assert seen[0].kind == ("known_good" if with_known_good else "baseline")
    assert seen[0].file_count == (3 if with_known_good else 99)
