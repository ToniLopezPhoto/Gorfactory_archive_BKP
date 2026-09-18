from pathlib import Path
from types import SimpleNamespace

import pytest

from gorbackup.cli import COMMANDS, main
from gorbackup.preflight import PreflightError


@pytest.mark.parametrize("command", COMMANDS)
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
    monkeypatch.setattr("gorbackup.cli.run_preflight", lambda config: None)

    assert main(["--config", "unused.yaml", command]) == 0
    assert "operation is not implemented yet" in capsys.readouterr().out


def test_missing_config_returns_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = main(["--config", str(tmp_path / "missing.yaml"), "status"])

    assert result == 2
    assert "configuration file not found" in capsys.readouterr().err


def test_backup_runs_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    config = object()
    calls = []
    monkeypatch.setattr("gorbackup.cli.load_config", lambda path: config)
    monkeypatch.setattr(
        "gorbackup.cli.check_rclone",
        lambda: SimpleNamespace(version=(1, 70, 0)),
    )
    monkeypatch.setattr("gorbackup.cli.run_preflight", lambda value: calls.append(value))

    assert main(["--config", "unused.yaml", "backup"]) == 0
    assert calls == [config]


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
        lambda config: (_ for _ in ()).throw(PreflightError(["wrong archive disk"])),
    )

    assert main(["--config", "unused.yaml", "backup"]) == 2
    assert "wrong archive disk" in capsys.readouterr().err
