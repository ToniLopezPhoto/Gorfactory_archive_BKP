"""Fail-closed file rename optimization for local rclone backends."""

import json
import os
import errno
import ctypes
import stat
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
    source_path: str
    source_fingerprint: Tuple[int, int, int, int]
    old_fingerprint: Tuple[int, int, int, int]


class RenameOptimizationUnavailable(RuntimeError):
    """A guaranteed no-op prevented optimization; normal sync may fall back."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class RenameStateAmbiguous(RuntimeError):
    """The rename post-state is unsafe to interpret; retain the journal."""


def _fingerprint(value: os.stat_result) -> Tuple[int, int, int, int]:
    return (value.st_size, value.st_mtime_ns, value.st_dev, value.st_ino)


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
        if _fingerprint(source_before) != _fingerprint(source_after) or _fingerprint(old_before) != _fingerprint(old_after):
            return None
        if source_bytes != item.size or old_bytes != item.leaving_size or source_hash != old_hash:
            return None
        return ProvenRename(
            item.related_path, item.path, item.size, source_hash, str(source),
            _fingerprint(source_after), _fingerprint(old_after),
        )
    except OSError:
        return None


RENAME_EXCL = 0x00000004


def rename_no_replace(source_dir_fd: int, source_name: str,
                      destination_dir_fd: int, destination_name: str) -> None:
    """Use macOS' atomic no-replace rename; never emulate it unsafely."""
    try:
        function = ctypes.CDLL(None, use_errno=True).renameatx_np
    except AttributeError as exc:
        raise RenameOptimizationUnavailable("atomic rename-no-replace is unsupported") from exc
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    result = function(
        source_dir_fd, os.fsencode(source_name), destination_dir_fd,
        os.fsencode(destination_name), RENAME_EXCL,
    )
    if result == 0:
        return
    code = ctypes.get_errno()
    labels = {
        errno.EEXIST: "destination already exists or appeared concurrently",
        errno.ENOENT: "source disappeared",
        errno.EXDEV: "cross-device rename is unsupported",
        errno.ENOTSUP: "filesystem does not support exclusive rename",
        errno.EPERM: "rename permission denied",
        errno.EACCES: "rename permission denied",
    }
    if code in labels:
        raise RenameOptimizationUnavailable(labels[code])
    raise RenameStateAmbiguous(f"rename-no-replace failed with errno {code}: {os.strerror(code)}")


def _open_directory_chain(root: Path, parts: Tuple[str, ...], *, create: bool) -> int:
    """Open a directory chain by fd, rejecting symlinks at every component."""
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    if root.is_symlink():
        raise RenameOptimizationUnavailable("current root must not be a symlink")
    root_resolved = root.resolve(strict=True)
    descriptor = os.open(root_resolved, flags)
    try:
        for part in parts:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, mode=0o755, dir_fd=descriptor)
                child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _leaf_state(directory_fd: int, leaf: str) -> Optional[os.stat_result]:
    try:
        return os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def execute_local_rename(
    current_root: Path, proven: ProvenRename, *,
    primitive: Callable[[int, str, int, str], None] = rename_no_replace,
) -> None:
    """Move exactly the proved inode within current, atomically without replace."""
    old_relative = PurePosixPath(proven.old_path)
    new_relative = PurePosixPath(proven.new_path)
    if old_relative == new_relative:
        raise RenameOptimizationUnavailable("source and destination paths are identical")
    old_fd = new_fd = None
    try:
        old_fd = _open_directory_chain(current_root, tuple(old_relative.parts[:-1]), create=False)
        new_fd = _open_directory_chain(current_root, tuple(new_relative.parts[:-1]), create=True)
        old_state = _leaf_state(old_fd, old_relative.name)
        if old_state is None:
            raise RenameOptimizationUnavailable("source disappeared")
        if not stat.S_ISREG(old_state.st_mode) or _fingerprint(old_state) != proven.old_fingerprint:
            raise RenameOptimizationUnavailable("source is not the proved regular file")
        source_state = os.stat(proven.source_path, follow_symlinks=False)
        if not stat.S_ISREG(source_state.st_mode) or _fingerprint(source_state) != proven.source_fingerprint:
            raise RenameOptimizationUnavailable("source catalogue file changed after identity proof")
        if _leaf_state(new_fd, new_relative.name) is not None:
            raise RenameOptimizationUnavailable("destination already exists")
        try:
            primitive(old_fd, old_relative.name, new_fd, new_relative.name)
        except Exception as exc:
            old_after = _leaf_state(old_fd, old_relative.name)
            new_after = _leaf_state(new_fd, new_relative.name)
            if old_after is not None and _fingerprint(old_after) == proven.old_fingerprint:
                if isinstance(exc, RenameOptimizationUnavailable):
                    raise
                raise RenameOptimizationUnavailable(
                    f"rename primitive failed without moving source: {exc}"
                ) from exc
            if old_after is None and new_after is not None and _fingerprint(new_after) == proven.old_fingerprint:
                return
            raise RenameStateAmbiguous(
                f"rename primitive failure left an ambiguous filesystem state: {exc}"
            ) from exc
        old_after = _leaf_state(old_fd, old_relative.name)
        new_after = _leaf_state(new_fd, new_relative.name)
        if old_after is not None or new_after is None or _fingerprint(new_after) != proven.old_fingerprint:
            raise RenameStateAmbiguous(
                "rename reported success but did not publish exactly the proved file"
            )
    except RenameOptimizationUnavailable:
        raise
    except RenameStateAmbiguous:
        raise
    except (OSError, ValueError) as exc:
        raise RenameOptimizationUnavailable(f"unsafe or unavailable rename path: {exc}") from exc
    finally:
        if old_fd is not None:
            os.close(old_fd)
        if new_fd is not None:
            os.close(new_fd)
