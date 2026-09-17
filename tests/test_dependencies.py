from types import SimpleNamespace

import pytest

from gorbackup.dependencies import DependencyError, check_rclone


def test_missing_rclone_has_actionable_error() -> None:
    with pytest.raises(DependencyError, match="not found on PATH"):
        check_rclone(which=lambda _: None)


def test_supported_rclone_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gorbackup.dependencies.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="rclone v1.70.3\n", stderr=""
        ),
    )

    result = check_rclone(which=lambda _: "/usr/local/bin/rclone")

    assert result.version == (1, 70, 3)


def test_old_rclone_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gorbackup.dependencies.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="rclone v1.64.2\n", stderr=""
        ),
    )

    with pytest.raises(DependencyError, match="unsupported"):
        check_rclone(which=lambda _: "/usr/bin/rclone")
