from pathlib import Path
from types import SimpleNamespace

import pytest

from gorbackup.cli import COMMANDS, main
from gorbackup.backup import BackupResult
from gorbackup.baseline import BaselineResult, ComparisonReport
from gorbackup.ledger import ScanResult
from gorbackup.planner import PlanResult
from gorbackup.preflight import PreflightError, PreflightResult, SourceSummary
from gorbackup.safety import SafetyAssessment


@pytest.mark.parametrize(
    "command",
    [
        item
        for item in COMMANDS
        if item not in {"backup", "baseline", "scan", "plan"}
    ],
)
def test_placeholder_commands_validate_without_touching_paths(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("gorbackup.cli.load_config", lambda path: object())
    monkeypatch.setattr(
        "gorbackup.cli.check_rclone",
        lambda: SimpleNamespace(version=(1, 70, 0)),
    )
    monkeypatch.setattr("gorbackup.cli.run_preflight", lambda config, **kwargs: None)
    monkeypatch.setattr("gorbackup.cli.load_baseline_summary", lambda config: None)

    assert main(["--config", "unused.yaml", command]) == 0
    assert "operation is not implemented yet" in capsys.readouterr().out


def test_missing_config_returns_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = main(["--config", str(tmp_path / "missing.yaml"), "status"])

    assert result == 2
    assert "configuration file not found" in capsys.readouterr().err


def test_backup_runs_preflight(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = object()
    calls = []
    monkeypatch.setattr("gorbackup.cli.load_config", lambda path: config)
    monkeypatch.setattr(
        "gorbackup.cli.check_rclone",
        lambda: SimpleNamespace(version=(1, 70, 0)),
    )
    monkeypatch.setattr(
        "gorbackup.cli.run_preflight", lambda value, **kwargs: calls.append(value)
    )
    monkeypatch.setattr("gorbackup.cli.load_baseline_summary", lambda config: None)
    monkeypatch.setattr(
        "gorbackup.cli.run_backup",
        lambda *args: BackupResult(
            "backup-1",
            "plan-1",
            "success",
            1,
            10,
            1,
            5,
            SafetyAssessment(1, 5, 10, 100, 90, 90.0),
            Path("history/backup-1"),
            Path("execution.json"),
            Path("backup.json"),
            (),
        ),
    )

    assert main(["--config", "unused.yaml", "backup"]) == 0
    assert calls == [config]
    output = capsys.readouterr().out
    assert "delete_count=1" in output
    assert "projected_free_percent=90.0" in output


def test_preflight_failure_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("gorbackup.cli.load_config", lambda path: object())
    monkeypatch.setattr(
        "gorbackup.cli.check_rclone",
        lambda: SimpleNamespace(version=(1, 70, 0)),
    )
    monkeypatch.setattr(
        "gorbackup.cli.run_preflight",
        lambda config, **kwargs: (_ for _ in ()).throw(
            PreflightError(["wrong archive disk"])
        ),
    )
    monkeypatch.setattr("gorbackup.cli.load_baseline_summary", lambda config: None)

    assert main(["--config", "unused.yaml", "backup"]) == 2
    assert "wrong archive disk" in capsys.readouterr().err


def test_baseline_command_adopts_verified_dump(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = object()
    rclone = SimpleNamespace(version=(1, 70, 0))
    preflight = PreflightResult(SourceSummary(1, 10), 80.0)
    comparison = ComparisonReport(1, (), (), (), ())
    expected = BaselineResult(
        Path("state/baseline.json"),
        Path("state/baseline-report.json"),
        comparison,
        False,
    )
    calls = []
    monkeypatch.setattr("gorbackup.cli.load_config", lambda path: config)
    monkeypatch.setattr("gorbackup.cli.check_rclone", lambda: rclone)
    monkeypatch.setattr(
        "gorbackup.cli.run_preflight", lambda value, **kwargs: preflight
    )
    monkeypatch.setattr(
        "gorbackup.cli.adopt_baseline",
        lambda *args, **kwargs: calls.append((args, kwargs)) or expected,
    )

    assert main(["--config", "unused.yaml", "baseline"]) == 0
    assert calls[0][0] == (config, rclone, preflight)
    assert "verified and adopted" in capsys.readouterr().out


def test_scan_command_updates_inventory(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = object()
    result = ScanResult("run-1", "success", 2, 30, 1, 0, (), Path("summary.json"))
    monkeypatch.setattr("gorbackup.cli.load_config", lambda path: config)
    monkeypatch.setattr(
        "gorbackup.cli.check_rclone",
        lambda: SimpleNamespace(version=(1, 70, 0)),
    )
    monkeypatch.setattr("gorbackup.cli.load_baseline_summary", lambda value: None)
    monkeypatch.setattr("gorbackup.cli.run_preflight", lambda *args, **kwargs: object())
    monkeypatch.setattr("gorbackup.cli.scan_catalogue", lambda value: result)

    assert main(["--config", "unused.yaml", "scan"]) == 0
    assert "run_id=run-1" in capsys.readouterr().out


def test_plan_command_prints_machine_counts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = object()
    counts = {
        "new_file": 1,
        "changed_file": 2,
        "delete_from_current": 3,
        "rename_move_candidate": 0,
        "skipped_recent": 4,
        "error": 0,
    }
    result = PlanResult(
        "plan-1", "success", (), counts, {}, 10, 1000, 3, 100, 5, 50,
        Path("plan.json")
    )
    monkeypatch.setattr("gorbackup.cli.load_config", lambda path: config)
    monkeypatch.setattr(
        "gorbackup.cli.check_rclone",
        lambda: SimpleNamespace(version=(1, 70, 0)),
    )
    monkeypatch.setattr("gorbackup.cli.load_baseline_summary", lambda value: None)
    monkeypatch.setattr("gorbackup.cli.run_preflight", lambda *args, **kwargs: object())
    monkeypatch.setattr("gorbackup.cli.create_plan", lambda *args: result)

    assert main(["--config", "unused.yaml", "plan"]) == 0
    output = capsys.readouterr().out
    assert "planned_transfer_bytes=100" in output
    assert "planned_archive_files=5" in output


def test_failed_plan_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = object()
    counts = {
        "new_file": 0,
        "changed_file": 0,
        "delete_from_current": 0,
        "rename_move_candidate": 0,
        "skipped_recent": 0,
        "error": 1,
    }
    result = PlanResult(
        "failed-plan", "failed", (), counts, {}, 0, 0, 0, 0, 0, 0,
        Path("failed-plan.json")
    )
    monkeypatch.setattr("gorbackup.cli.load_config", lambda path: config)
    monkeypatch.setattr(
        "gorbackup.cli.check_rclone",
        lambda: SimpleNamespace(version=(1, 70, 0)),
    )
    monkeypatch.setattr("gorbackup.cli.load_baseline_summary", lambda value: None)
    monkeypatch.setattr("gorbackup.cli.run_preflight", lambda *args, **kwargs: object())
    monkeypatch.setattr("gorbackup.cli.create_plan", lambda *args: result)

    assert main(["--config", "unused.yaml", "plan"]) == 2
    assert "errors=1" in capsys.readouterr().out
