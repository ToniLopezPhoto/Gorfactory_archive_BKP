import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

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
from gorbackup.planner import (
    create_plan,
    parse_combined_plan,
    parse_json_log,
)

FIXTURES = Path(__file__).parent / "fixtures"
FIXED_TIME = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def make_config(tmp_path: Path) -> AppConfig:
    source = tmp_path / "source-volume" / "catalogue"
    archive = tmp_path / "archive-volume"
    current = archive / "current"
    source.mkdir(parents=True)
    current.mkdir(parents=True)
    (archive / "history").mkdir()
    return AppConfig(
        SourceConfig(source, source.parent, ".source-id", "source-id"),
        ArchiveConfig(archive, "current", "history", ".archive-id", "archive-id"),
        SafetyConfig(10, 1, 10, 15, 0, 0.8),
        RetentionConfig(False),
        LoggingConfig(),
        StateConfig(Path("state")),
    )


def test_fixture_parsers_classify_paths_reasons_and_json_errors(tmp_path: Path) -> None:
    source = tmp_path / "source"
    current = tmp_path / "current"
    for path, content in (
        (source / "incoming/new-photo.tif", b"new"),
        (source / "changed/edit.tif", b"changed"),
        (current / "changed/edit.tif", b"old-edit"),
        (current / "retired/old-photo.tif", b"old"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    combined = parse_combined_plan(
        (FIXTURES / "rclone_plan_combined.txt").read_text(encoding="utf-8").splitlines(),
        source,
        current,
    )
    log_errors, warnings = parse_json_log(
        (FIXTURES / "rclone_plan.jsonl").read_text(encoding="utf-8").splitlines()
    )

    assert [(item.category, item.path) for item in combined] == [
        ("new_file", "incoming/new-photo.tif"),
        ("changed_file", "changed/edit.tif"),
        ("delete_from_current", "retired/old-photo.tif"),
        ("error", "unreadable/broken.raw"),
    ]
    assert combined[0].size == 3
    assert log_errors[0].path == "unreadable/broken.raw"
    assert log_errors[0].size == 456
    assert warnings == ["Excluded recent file"]


def test_plan_is_dry_run_persisted_and_classifies_rename_and_recent(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    source = config.source.path
    current = config.archive.root / config.archive.current_dir
    (source / "new.tif").write_bytes(b"new")
    (source / "changed.tif").write_bytes(b"changed")
    (current / "changed.tif").write_bytes(b"old")
    (current / "deleted.tif").write_bytes(b"deleted")
    (source / "renamed-new.tif").write_bytes(b"rename")
    (current / "renamed-old.tif").write_bytes(b"rename")
    old_ns = 1_700_000_000_000_000_000
    os.utime(source / "renamed-new.tif", ns=(old_ns, old_ns))
    os.utime(current / "renamed-old.tif", ns=(old_ns, old_ns))
    (source / "recent.tif").write_bytes(b"recent")
    recent_ns = int(FIXED_TIME.timestamp() * 1_000_000_000)
    os.utime(source / "recent.tif", ns=(recent_ns, recent_ns))
    original_current = {
        path.relative_to(current).as_posix(): path.read_bytes()
        for path in current.rglob("*")
        if path.is_file()
    }
    commands = []

    def runner(command, **kwargs):
        commands.append(command)
        combined = Path(command[command.index("--combined") + 1])
        log = Path(command[command.index("--log-file") + 1])
        combined.write_text(
            "+ new.tif\n* changed.tif\n- deleted.tif\n"
            "+ renamed-new.tif\n- renamed-old.tif\n",
            encoding="utf-8",
        )
        log.write_text(
            '{"level":"info","msg":"dry-run complete"}\n', encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    result = create_plan(
        config,
        RcloneInfo("rclone", (1, 70, 0)),
        runner=runner,
        now=lambda: FIXED_TIME,
        run_id_factory=lambda: "plan-run-1",
    )

    assert result.status == "success"
    assert result.counts == {
        "new_file": 1,
        "changed_file": 1,
        "delete_from_current": 1,
        "rename_move_candidate": 1,
        "skipped_recent": 1,
        "error": 0,
    }
    assert result.transfer_bytes == 3 + 7 + 6
    assert result.leaving_current_bytes == 7 + 3 + 6
    changed = next(item for item in result.items if item.category == "changed_file")
    assert changed.size == 7
    assert changed.leaving_size == 3
    rename = next(item for item in result.items if item.category == "rename_move_candidate")
    assert rename.path == "renamed-new.tif"
    assert rename.related_path == "renamed-old.tif"
    command = commands[0]
    assert command[1] == "sync"
    assert "--dry-run" in command
    assert "--use-json-log" in command
    assert "--min-age" in command
    assert {
        path.relative_to(current).as_posix(): path.read_bytes()
        for path in current.rglob("*")
        if path.is_file()
    } == original_current
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert payload["dry_run"] is True
    assert payload["transfer_bytes"] == result.transfer_bytes
    with Ledger(config.ledger_path) as ledger:
        run = ledger.latest_successful_run()
        assert run is not None and run["run_id"] == "plan-run-1"
        assert len(ledger.plan_items("plan-run-1")) == 5


def test_rclone_json_error_persists_failed_plan(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    def runner(command, **kwargs):
        Path(command[command.index("--combined") + 1]).write_text("", encoding="utf-8")
        Path(command[command.index("--log-file") + 1]).write_text(
            '{"level":"error","msg":"permission denied","object":"blocked.tif"}\n',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 1, "", "")

    result = create_plan(
        config,
        RcloneInfo("rclone", (1, 70, 0)),
        runner=runner,
        now=lambda: FIXED_TIME,
        run_id_factory=lambda: "failed-plan",
    )

    assert result.status == "failed"
    assert result.counts["error"] == 1
    with Ledger(config.ledger_path) as ledger:
        history = ledger.run_history()
        assert history[0]["status"] == "failed"
        assert "permission denied" in history[0]["errors_json"]
