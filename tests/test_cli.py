from pathlib import Path
from types import SimpleNamespace

import pytest

from gorbackup.cli import COMMANDS, main
from gorbackup.backup import BackupResult
from gorbackup.pruning import PruneResult
from gorbackup.baseline import BaselineResult, ComparisonReport
from gorbackup.ledger import ScanResult
from gorbackup.planner import PlanResult
from gorbackup.preflight import PreflightError, PreflightResult, SourceSummary
from gorbackup.recovery import HistoryResult, HistoryVersion, RestoreResult
from gorbackup.safety import SafetyAssessment


@pytest.mark.parametrize(
    "command",
    [
        item
        for item in COMMANDS
        if item not in {"backup", "baseline", "scan", "plan", "verify", "history", "restore", "prune"}
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
        lambda *args, **kwargs: BackupResult(
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


def test_override_never_bypasses_critical_preflight(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("gorbackup.cli.load_config", lambda path: object())
    monkeypatch.setattr(
        "gorbackup.cli.check_rclone", lambda: SimpleNamespace(version=(1, 70, 0))
    )
    monkeypatch.setattr(
        "gorbackup.cli.run_preflight",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            PreflightError(["source identity mismatch"])
        ),
    )
    monkeypatch.setattr(
        "gorbackup.cli.run_backup",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("backup must not start")
        ),
    )

    assert main(["--config", "unused.yaml", "backup", "--override-safety"]) == 2
    assert "source identity mismatch" in capsys.readouterr().err


def test_override_is_rejected_in_unattended_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("gorbackup.cli.load_config", lambda path: object())
    monkeypatch.setattr(
        "gorbackup.cli.check_rclone", lambda: SimpleNamespace(version=(1, 70, 0))
    )
    monkeypatch.setattr("gorbackup.cli.run_preflight", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "gorbackup.cli.run_backup",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("backup must not start")
        ),
    )

    assert main(["--config", "unused.yaml", "backup", "--override-safety"]) == 2
    assert "manual-only" in capsys.readouterr().err


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


def test_verify_command_returns_success_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source"
    archive = tmp_path / "archive"
    source.mkdir()
    (archive / "current").mkdir(parents=True)
    (source / "photo.tif").write_bytes(b"same")
    (archive / "current" / "photo.tif").write_bytes(b"same")
    config = SimpleNamespace(
        source=SimpleNamespace(path=source),
        archive=SimpleNamespace(root=archive, current_dir="current"),
    )
    monkeypatch.setattr("gorbackup.cli.load_config", lambda path: config)
    monkeypatch.setattr(
        "gorbackup.cli.check_rclone",
        lambda: SimpleNamespace(version=(1, 70, 0)),
    )
    monkeypatch.setattr("gorbackup.cli.run_preflight", lambda *args, **kwargs: None)

    assert main(["--config", "unused.yaml", "verify", "photo.tif"]) == 0
    assert "verify: success" in capsys.readouterr().out

    (archive / "current" / "photo.tif").write_bytes(b"oops")
    assert main(["--config", "unused.yaml", "verify", "photo.tif"]) == 2
    assert "verify: failed" in capsys.readouterr().out


def test_history_cli_is_read_only_and_does_not_require_rclone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = object()
    result = HistoryResult(
        "Campaign/photo.tif",
        HistoryVersion(
            "current", None, None, None, 3, None, "current", Path("current"),
            True, "differs_from_known_good",
        ),
        (HistoryVersion(
            "history", "run-1", "2026-09-20T12:00:00+00:00", "success",
            4, None, "overwritten", Path("history"), False,
        ),),
    )
    monkeypatch.setattr("gorbackup.cli.load_config", lambda path: config)
    monkeypatch.setattr(
        "gorbackup.cli.check_rclone",
        lambda: (_ for _ in ()).throw(AssertionError("rclone must not be checked")),
    )
    monkeypatch.setattr("gorbackup.cli.find_history", lambda *args: result)

    assert main(["--config", "unused.yaml", "history", "Campaign/photo.tif"]) == 0
    output = capsys.readouterr().out
    assert "run run-1" in output
    assert "recorded but missing" in output
    assert "differs_from_known_good" in output


def test_restore_cli_reports_verified_destination_without_rclone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = object()
    result = RestoreResult(
        "restore-1", "photo.tif", "run-1", Path("history/photo.tif"),
        Path("recovery/restore-1/photo.tif"), 4, "abcd", "sha256",
    )
    monkeypatch.setattr("gorbackup.cli.load_config", lambda path: config)
    monkeypatch.setattr(
        "gorbackup.cli.check_rclone",
        lambda: (_ for _ in ()).throw(AssertionError("rclone must not be checked")),
    )
    monkeypatch.setattr("gorbackup.cli.restore_version", lambda *args, **kwargs: result)

    assert main([
        "--config", "unused.yaml", "restore", "photo.tif", "--run", "run-1"
    ]) == 0
    output = capsys.readouterr().out
    assert "restore: success" in output
    assert "checksum=sha256:abcd" in output


def test_prune_dry_run_reports_plan_without_rclone(monkeypatch, capsys) -> None:
    monkeypatch.setattr("gorbackup.cli.load_config", lambda path: object())
    monkeypatch.setattr("gorbackup.cli.check_rclone", lambda: pytest.fail("no rclone"))
    result = PruneResult("p1", "planned", 2, 30, 1, Path("plan.json"), 4,
                         "2020-01-01", "2020-01-02",
                         {"free_percent": 10.0}, {"free_percent": 11.0})
    monkeypatch.setattr("gorbackup.cli.plan_prune", lambda config: result)
    assert main(["--config", "unused.yaml", "prune", "--dry-run"]) == 0
    assert "prune_id=p1" in capsys.readouterr().out


def test_prune_execute_requires_yes(monkeypatch, capsys) -> None:
    monkeypatch.setattr("gorbackup.cli.load_config", lambda path: object())
    assert main(["--config", "unused.yaml", "prune", "--execute", "p1"]) == 2
    assert "requires explicit --yes" in capsys.readouterr().err
