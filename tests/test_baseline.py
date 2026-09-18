import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from gorbackup.baseline import (
    BaselineError,
    BaselineDifferencesError,
    adopt_baseline,
    load_baseline_summary,
    parse_combined_report,
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
from gorbackup.preflight import PreflightResult, SourceSummary


def make_config(tmp_path: Path) -> AppConfig:
    source = tmp_path / "source"
    archive = tmp_path / "archive"
    source.mkdir()
    archive.mkdir()
    (archive / "current").mkdir()
    return AppConfig(
        SourceConfig(source, source, ".source-id", "source-id"),
        ArchiveConfig(archive, "current", "history", ".archive-id", "archive-id"),
        SafetyConfig(10, 1, 10, 5, 0, 0.8),
        RetentionConfig(False),
        LoggingConfig(),
        StateConfig(tmp_path / "state"),
    )


def fake_runner(reports, commands):
    remaining = iter(reports)

    def run(command, **kwargs):
        commands.append(command)
        if command[1] == "check":
            report_path = Path(command[command.index("--combined") + 1])
            report = next(remaining)
            report_path.write_text(report, encoding="utf-8")
            return_code = 1 if any(
                line.startswith(("+ ", "- ", "* ", "! "))
                for line in report.splitlines()
            ) else 0
            return subprocess.CompletedProcess(command, return_code, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    return run


def fixed_clock():
    return datetime(2026, 9, 18, 10, 30, tzinfo=timezone.utc)


def test_combined_report_identifies_paths_and_reasons() -> None:
    report = parse_combined_report(
        ["= same.tif", "+ absent.tif", "- extra.tif", "* changed.tif", "! bad.tif"]
    )

    assert report.matched == 1
    assert report.missing_destination[0].path == "absent.tif"
    assert report.destination_extras[0].reason == "extra on destination"
    assert report.mismatched[0].path == "changed.tif"
    assert report.errors[0].reason == "read or hash error"


def test_matching_dump_is_adopted_and_extras_are_only_reported(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    commands = []
    preflight = PreflightResult(SourceSummary(2, 1000), 80.0)

    result = adopt_baseline(
        config,
        RcloneInfo("rclone", (1, 70, 0)),
        preflight,
        runner=fake_runner(["= one.tif\n= two.tif\n- review-me.tif\n"], commands),
        now=fixed_clock,
        disk_usage=lambda path: SimpleNamespace(total=1000, used=250, free=750),
    )

    assert result.reconciled is False
    assert [command[1] for command in commands] == ["check"]
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "known-good"
    assert manifest["source"]["file_count"] == 2
    assert manifest["archive"]["free_bytes"] == 750
    assert manifest["verification"]["destination_extras"][0]["path"] == "review-me.tif"
    assert list((config.state.directory / "runs").glob("baseline-*.json"))


def test_differences_require_explicit_reconciliation(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    commands = []

    with pytest.raises(BaselineDifferencesError, match="requiring reconciliation"):
        adopt_baseline(
            config,
            RcloneInfo("rclone", (1, 70, 0)),
            PreflightResult(SourceSummary(1, 10), 80.0),
            runner=fake_runner(["+ missing.tif\n* changed.tif\n"], commands),
            now=fixed_clock,
        )

    assert [command[1] for command in commands] == ["check"]
    assert not (config.state.directory / config.state.baseline_manifest).exists()
    assert (config.state.directory / config.state.baseline_report).exists()


def test_reconciliation_uses_copy_then_rechecks_without_deleting(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    commands = []

    result = adopt_baseline(
        config,
        RcloneInfo("rclone", (1, 70, 0)),
        PreflightResult(SourceSummary(2, 20), 80.0),
        reconcile=True,
        runner=fake_runner(
            ["+ missing.tif\n* changed.tif\n", "= missing.tif\n= changed.tif\n- extra.tif\n"],
            commands,
        ),
        now=fixed_clock,
        disk_usage=lambda path: SimpleNamespace(total=100, used=30, free=70),
    )

    assert result.reconciled is True
    assert [command[1] for command in commands] == ["check", "copy", "check"]
    assert all(command[1] not in {"sync", "delete", "purge"} for command in commands)
    copy_command = commands[1]
    assert "--check-first" in copy_command
    assert f"/{config.source.marker_file}" in copy_command


def test_known_good_manifest_loads_preflight_summary(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.state.directory.mkdir()
    (config.state.directory / config.state.baseline_manifest).write_text(
        json.dumps(
            {
                "status": "known-good",
                "source": {"file_count": 42, "total_size_bytes": 1234},
            }
        ),
        encoding="utf-8",
    )

    assert load_baseline_summary(config) == SourceSummary(42, 1234)


def test_state_cannot_be_written_inside_read_only_source(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    unsafe_state = config.source.path / "state"
    config = AppConfig(
        config.source,
        config.archive,
        config.safety,
        config.retention,
        config.logging,
        StateConfig(unsafe_state),
    )

    with pytest.raises(BaselineError, match="must not be inside"):
        adopt_baseline(
            config,
            RcloneInfo("rclone", (1, 70, 0)),
            PreflightResult(SourceSummary(1, 10), 80.0),
            runner=fake_runner([], []),
        )

    assert not unsafe_state.exists()
