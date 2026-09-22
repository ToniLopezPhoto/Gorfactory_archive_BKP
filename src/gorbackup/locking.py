"""Atomic, ownership-safe locking for mutable backup runs."""

import json
import os
import socket
import uuid
import fcntl
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional


class LockError(RuntimeError):
    """Raised when exclusive backup ownership cannot be established."""


class HistoryLock:
    """POSIX shared/exclusive lock coordinating history readers and writers."""

    def __init__(self, path: Path, *, exclusive: bool) -> None:
        self.path = path
        self.exclusive = exclusive
        self._descriptor: Optional[int] = None

    def acquire(self) -> "HistoryLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            mode = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
            fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            kind = "exclusive" if self.exclusive else "shared"
            raise LockError(f"history {kind} lock is busy: {self.path}") from exc
        self._descriptor = descriptor
        return self

    def release(self) -> None:
        if self._descriptor is not None:
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
            os.close(self._descriptor)
            self._descriptor = None

    def __enter__(self) -> "HistoryLock":
        return self.acquire()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()


def process_is_alive(pid: int) -> bool:
    """Return whether *pid* exists, treating inaccessible processes as alive."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass(frozen=True)
class LockOwner:
    pid: int
    hostname: str
    acquired_at: str
    token: str
    run_id: Optional[str]
    schema_version: int = 1


class BackupLock:
    """Filesystem lock acquired atomically and released only by its owner."""

    def __init__(
        self,
        path: Path,
        *,
        run_id: Optional[str] = None,
        pid: Optional[int] = None,
        hostname: Optional[str] = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        token_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        process_checker: Callable[[int], bool] = process_is_alive,
    ) -> None:
        self.path = path
        self.process_checker = process_checker
        self.owner = LockOwner(
            os.getpid() if pid is None else pid,
            socket.gethostname() if hostname is None else hostname,
            now().isoformat(),
            token_factory(),
            run_id,
        )
        self._acquired = False
        self._inode: Optional[tuple[int, int]] = None

    @contextmanager
    def _path_guard(self):
        """Serialize stale recovery and release around the atomic owner file."""
        guard_path = self.path.with_name(self.path.name + ".guard")
        descriptor = os.open(guard_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _read_owner(self) -> LockOwner:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("lock payload is not an object")
            owner = LockOwner(
                pid=int(payload["pid"]),
                hostname=str(payload["hostname"]),
                acquired_at=str(payload["acquired_at"]),
                token=str(payload["token"]),
                run_id=payload.get("run_id"),
                schema_version=int(payload["schema_version"]),
            )
            if owner.pid <= 0 or not owner.hostname or not owner.token or owner.schema_version != 1:
                raise ValueError("invalid lock fields")
            datetime.fromisoformat(owner.acquired_at)
            return owner
        except FileNotFoundError:
            raise
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise LockError(f"backup lock is corrupt or invalid: {self.path}: {exc}") from exc

    @staticmethod
    def _description(owner: LockOwner) -> str:
        run = f", run_id={owner.run_id}" if owner.run_id else ""
        return (
            f"pid={owner.pid}, hostname={owner.hostname}, "
            f"acquired_at={owner.acquired_at}{run}"
        )

    def acquire(self) -> "BackupLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._path_guard():
            return self._acquire_guarded()

    def _acquire_guarded(self) -> "BackupLock":
        payload = (json.dumps(self.owner.__dict__, sort_keys=True) + "\n").encode("utf-8")
        while True:
            try:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                stat_before = self.path.stat()
                existing = self._read_owner()
                if existing.hostname != self.owner.hostname:
                    raise LockError(
                        "backup lock belongs to another host and cannot be verified safely: "
                        + self._description(existing)
                    )
                if self.process_checker(existing.pid):
                    raise LockError("backup already running: " + self._description(existing))
                try:
                    stat_after = self.path.stat()
                    if (stat_before.st_dev, stat_before.st_ino) != (stat_after.st_dev, stat_after.st_ino):
                        continue
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            except OSError as exc:
                raise LockError(f"could not acquire backup lock {self.path}: {exc}") from exc
            try:
                os.write(descriptor, payload)
                os.fsync(descriptor)
            except Exception:
                os.close(descriptor)
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                raise
            os.close(descriptor)
            self._acquired = True
            metadata = self.path.stat()
            self._inode = (metadata.st_dev, metadata.st_ino)
            return self

    def release(self) -> None:
        if not self._acquired:
            return
        with self._path_guard():
            self._release_guarded()

    def _release_guarded(self) -> None:
        try:
            existing = self._read_owner()
        except FileNotFoundError:
            self._acquired = False
            return
        if existing.token != self.owner.token:
            self._acquired = False
            raise LockError("refusing to release a backup lock owned by another process")
        metadata = self.path.stat()
        if self._inode != (metadata.st_dev, metadata.st_ino):
            self._acquired = False
            raise LockError("refusing to release a replaced backup lock")
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self._acquired = False

    def __enter__(self) -> "BackupLock":
        return self.acquire()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()
