"""Checks for external executables required by gorbackup."""

import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional, Sequence, Tuple

MINIMUM_RCLONE_VERSION: Tuple[int, int, int] = (1, 65, 0)


class DependencyError(RuntimeError):
    """Raised when an external runtime dependency is unavailable."""


@dataclass(frozen=True)
class RcloneInfo:
    executable: str
    version: Tuple[int, int, int]


def _format_version(version: Sequence[int]) -> str:
    return ".".join(str(part) for part in version)


def check_rclone(
    minimum: Tuple[int, int, int] = MINIMUM_RCLONE_VERSION,
    *,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> RcloneInfo:
    """Return installed rclone details or raise a user-facing error."""
    executable = which("rclone")
    if executable is None:
        raise DependencyError(
            "rclone is required but was not found on PATH. "
            f"Install rclone {_format_version(minimum)} or newer."
        )

    try:
        result = subprocess.run(
            [executable, "version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DependencyError(f"could not run rclone: {exc}") from exc

    output = (result.stdout or result.stderr).strip()
    if result.returncode != 0:
        raise DependencyError(
            f"rclone version check failed with exit code {result.returncode}: {output}"
        )

    match = re.search(r"(?im)^rclone v(\d+)\.(\d+)\.(\d+)", output)
    if match is None:
        raise DependencyError("could not determine rclone version from its output")
    installed = tuple(int(part) for part in match.groups())
    if installed < minimum:
        raise DependencyError(
            f"rclone {_format_version(installed)} is unsupported; "
            f"install {_format_version(minimum)} or newer."
        )
    return RcloneInfo(executable, installed)
