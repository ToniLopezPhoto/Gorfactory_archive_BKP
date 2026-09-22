import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from gorbackup.dependencies import RcloneInfo
from gorbackup.planner import PlanItem
from gorbackup.renames import (
    BackendCapabilities, execute_local_rename, probe_rename_capabilities,
    prove_identity,
)


def test_identity_requires_content_not_size_and_mtime(tmp_path: Path) -> None:
    source, current = tmp_path / "source", tmp_path / "current"
    source.mkdir(); current.mkdir()
    (source / "new.tif").write_bytes(b"AAAA")
    (current / "old.tif").write_bytes(b"BBBB")
    stamp = 1_700_000_000_000_000_000
    os.utime(source / "new.tif", ns=(stamp, stamp))
    os.utime(current / "old.tif", ns=(stamp, stamp))
    item = PlanItem("rename_move_candidate", "new.tif", 4, "candidate", "old.tif", 4)
    assert prove_identity(source, current, item) is None


def test_unicode_move_is_proven_and_atomic(tmp_path: Path) -> None:
    source, current = tmp_path / "source", tmp_path / "current"
    source.mkdir(); current.mkdir()
    new = "Campaña 2026/Foto José 001.tif"
    (source / "Campaña 2026").mkdir()
    (source / new).write_bytes(b"pixels")
    (current / "old.tif").write_bytes(b"pixels")
    item = PlanItem("rename_move_candidate", new, 6, "candidate", "old.tif", 6)
    proven = prove_identity(source, current, item)
    assert proven is not None
    execute_local_rename(current, proven)
    assert (current / new).read_bytes() == b"pixels"
    assert not (current / "old.tif").exists()


def test_capability_probe_requires_local_move_and_common_hash(tmp_path: Path) -> None:
    replies = iter([
        {"Name": "local", "Hashes": ["md5", "sha1"], "Features": {"Move": True, "Copy": True}},
        {"Name": "local", "Hashes": ["sha1"], "Features": {"Move": True, "Copy": True}},
    ])
    def runner(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, json.dumps(next(replies)), "")
    result = probe_rename_capabilities(RcloneInfo("rclone", (1, 70, 0)), tmp_path, tmp_path, runner=runner)
    assert result.can_optimize
    assert result.common_hashes == frozenset({"sha1"})


@pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone is not installed")
def test_real_rclone_track_renames_with_backup_dir(tmp_path: Path) -> None:
    """Empirically pin native behavior without touching configured volumes."""
    source, current, history = tmp_path / "source", tmp_path / "current", tmp_path / "history"
    source.mkdir(); current.mkdir(); history.mkdir()
    (source / "old.tif").write_bytes(b"small fixture")
    subprocess.run(["rclone", "sync", str(source), str(current)], check=True)
    (source / "old.tif").rename(source / "new.tif")
    completed = subprocess.run([
        "rclone", "sync", str(source), str(current), "--track-renames",
        "--track-renames-strategy", "hash", "--backup-dir", str(history),
        "--use-json-log", "--log-level", "INFO",
    ], check=False, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert (current / "new.tif").read_bytes() == b"small fixture"
    assert not (current / "old.tif").exists()
    entries = [json.loads(line) for line in completed.stderr.splitlines() if line.strip()]
    assert not any(str(entry.get("msg", "")).lower().startswith("copied") for entry in entries)
    # Native track-renames moves current/old to current/new; it does not create
    # a recoverable history/old duplicate.
    assert not (history / "old.tif").exists()
