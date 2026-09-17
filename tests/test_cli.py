from pathlib import Path
from types import SimpleNamespace

import pytest

from gorbackup.cli import COMMANDS, main


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

    assert main(["--config", "unused.yaml", command]) == 0
    assert "operation is not implemented yet" in capsys.readouterr().out


def test_missing_config_returns_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = main(["--config", str(tmp_path / "missing.yaml"), "status"])

    assert result == 2
    assert "configuration file not found" in capsys.readouterr().err
