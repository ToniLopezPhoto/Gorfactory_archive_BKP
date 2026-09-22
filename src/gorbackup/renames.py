"""Fail-closed file rename optimization for local rclone backends."""

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, FrozenSet, Optional, Tuple

from gorbackup.dependencies import RcloneInfo
from gorbackup.planner import PlanItem
from gorbackup.verification import hash_file


@dataclass(frozen=True)
class BackendCapabilities:
    backend: str
    hashes: FrozenSet[str]
    move: bool
    copy: bool


@dataclass(frozen=True)
class RenameCapabilities:
    source: Optional[BackendCapabilities]
    destination: Optional[BackendCapabilities]
    common_hashes: FrozenSet[str]
    can_optimize: bool
    warning: Optional[str] = None


@dataclass(frozen=True)
class ProvenRename:
    old_path: str
    new_path: str
    size: int
    sha256: str


def _parse_features(stdout: str) -> BackendCapabilities:
    payload = json.loads(stdout)
    features = payload.get("Features", payload.get("features", {}))
    hashes = payload.get("Hashes", payload.get("hashes", []))
    name = str(payload.get("Name") or payload.get("name") or payload.get("Type") or "")
    if isinstance(hashes, dict):
        hashes = [key for key, enabled in hashes.items() if enabled]
    return BackendCapabilities(
        backend=name.lower(),
        hashes=frozenset(str(value).lower() for value in hashes),
        move=bool(features.get("Move")),
        copy=bool(features.get("Copy")),
    )


def probe_rename_capabilities(
    rclone: RcloneInfo, source: Path, destination: Path, *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> RenameCapabilities:
    """Probe both endpoints; any malformed/failed probe disables optimization."""
    try:
        endpoints = []
        for path in (source, destination):
            completed = runner(
                [rclone.executable, "backend", "features", str(path)],
                check=False, capture_output=True, text=True,
            )
            if completed.returncode != 0:
                raise RuntimeError((completed.stderr or completed.stdout).strip())
            endpoints.append(_parse_features(completed.stdout))
        source_cap, destination_cap = endpoints
        common = source_cap.hashes & destination_cap.hashes
        # This implementation deliberately uses an atomic destination-side
        # rename only for local paths. SHA-256 below remains the identity proof;
        # common rclone hashes are required as an additional capability gate.
        local = source_cap.backend == "local" and destination_cap.backend == "local"
        enabled = local and destination_cap.move and bool(common)
        warning = None if enabled else "rename optimization unavailable: local Move and a common hash are required"
        return RenameCapabilities(source_cap, destination_cap, common, enabled, warning)
    except (OSError, ValueError, TypeError, json.JSONDecodeError, RuntimeError) as exc:
        return RenameCapabilities(None, None, frozenset(), False, f"rename capability probe failed: {exc}")


def _path(root: Path, relative: str) -> Path:
    value = PurePosixPath(relative)
    if value.is_absolute() or not value.parts or ".." in value.parts:
        raise ValueError(f"unsafe rename path: {relative!r}")
    return root.joinpath(*value.parts)


def prove_identity(source_root: Path, current_root: Path, item: PlanItem) -> Optional[ProvenRename]:
    """Prove a candidate by stable SHA-256 reads; metadata alone never suffices."""
    if item.category != "rename_move_candidate" or not item.related_path:
        return None
    source = _path(source_root, item.path)
    old = _path(current_root, item.related_path)
    try:
        source_before, old_before = source.stat(), old.stat()
        if not source.is_file() or not old.is_file() or source_before.st_size != old_before.st_size:
            return None
        source_hash, source_bytes = hash_file(source)
        old_hash, old_bytes = hash_file(old)
        source_after, old_after = source.stat(), old.stat()
        fingerprint = lambda value: (value.st_size, value.st_mtime_ns, value.st_dev, value.st_ino)
        if fingerprint(source_before) != fingerprint(source_after) or fingerprint(old_before) != fingerprint(old_after):
            return None
        if source_bytes != item.size or old_bytes != item.leaving_size or source_hash != old_hash:
            return None
        return ProvenRename(item.related_path, item.path, item.size, source_hash)
    except OSError:
        return None


def execute_local_rename(current_root: Path, proven: ProvenRename) -> None:
    """Atomically move the proven current file, refusing overwrite or aliasing."""
    old = _path(current_root, proven.old_path)
    new = _path(current_root, proven.new_path)
    if new.exists() or not old.is_file():
        raise OSError("rename target exists or source disappeared")
    new.parent.mkdir(parents=True, exist_ok=True)
    os.rename(old, new)

