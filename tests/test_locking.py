import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gorbackup.locking import BackupLock, LockError, process_is_alive


NOW = datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc)


def lock(path: Path, *, pid: int, host: str = "host", alive=lambda pid: False,
         token: str = "token") -> BackupLock:
    return BackupLock(
        path, pid=pid, hostname=host, now=lambda: NOW,
        process_checker=alive, token_factory=lambda: token,
    )


def test_only_one_attempt_acquires_and_active_owner_is_diagnostic(tmp_path: Path) -> None:
    path = tmp_path / "backup.lock"
    first = lock(path, pid=101, alive=lambda pid: True, token="first")
    second = lock(path, pid=202, alive=lambda pid: True, token="second")
    first.acquire()
    with pytest.raises(LockError, match=r"pid=101.*hostname=host.*acquired_at="):
        second.acquire()
    first.release()


def test_dead_local_owner_is_recovered_and_sequential_runs_continue(tmp_path: Path) -> None:
    path = tmp_path / "backup.lock"
    stale = lock(path, pid=101, token="stale")
    stale.acquire()
    recovered = lock(path, pid=202, token="recovered")
    recovered.acquire()
    recovered.release()
    following = lock(path, pid=303, token="following")
    following.acquire()
    following.release()
    assert not path.exists()


def test_live_pid_is_never_recovered_even_when_timestamp_is_old(tmp_path: Path) -> None:
    path = tmp_path / "backup.lock"
    path.write_text(json.dumps({
        "schema_version": 1, "pid": 101, "hostname": "host",
        "acquired_at": "2000-01-01T00:00:00+00:00", "token": "old",
        "run_id": None,
    }), encoding="utf-8")
    with pytest.raises(LockError, match="already running"):
        lock(path, pid=202, alive=lambda pid: True).acquire()
    assert path.exists()


def test_remote_and_corrupt_locks_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "backup.lock"
    remote = lock(path, pid=101, host="remote", token="remote")
    remote.acquire()
    with pytest.raises(LockError, match="another host"):
        lock(path, pid=202, host="local").acquire()
    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(LockError, match="corrupt or invalid"):
        lock(path, pid=202).acquire()
    assert path.exists()


def test_ownership_token_prevents_deleting_replacement(tmp_path: Path) -> None:
    path = tmp_path / "backup.lock"
    owner = lock(path, pid=101, token="mine")
    owner.acquire()
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["token"] = "theirs"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(LockError, match="owned by another"):
        owner.release()
    assert path.exists()


@pytest.mark.parametrize(
    ("error", "expected"),
    [(ProcessLookupError(), False), (PermissionError(), True)],
)
def test_process_probe_handles_missing_and_inaccessible_pids(
    monkeypatch: pytest.MonkeyPatch, error: OSError, expected: bool
) -> None:
    def fail(pid: int, signal: int) -> None:
        raise error

    monkeypatch.setattr("gorbackup.locking.os.kill", fail)
    assert process_is_alive(123) is expected
