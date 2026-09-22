import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from gorbackup.dependencies import RcloneInfo
from gorbackup.planner import PlanItem
from gorbackup.renames import (
    BackendCapabilities, RenameOptimizationUnavailable, RenameStateAmbiguous,
    execute_local_rename, probe_rename_capabilities, prove_identity,
    rename_no_replace,
)


def fake_no_replace(source_fd, source_name, destination_fd, destination_name):
    try:
        os.stat(destination_name, dir_fd=destination_fd, follow_symlinks=False)
    except FileNotFoundError:
        os.rename(source_name, destination_name, src_dir_fd=source_fd, dst_dir_fd=destination_fd)
    else:
        raise RenameOptimizationUnavailable("destination already exists")


def proven_pair(tmp_path: Path, new: str = "new.tif"):
    source, current = tmp_path / "source", tmp_path / "current"
    source.mkdir(); current.mkdir()
    source_path = source / new
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"pixels")
    (current / "old.tif").write_bytes(b"pixels")
    item = PlanItem("rename_move_candidate", new, 6, "candidate", "old.tif", 6)
    proven = prove_identity(source, current, item)
    assert proven is not None
    return source, current, proven


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
    execute_local_rename(current, proven, primitive=fake_no_replace)
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


def test_existing_destination_is_never_overwritten(tmp_path: Path) -> None:
    _, current, proven = proven_pair(tmp_path)
    (current / "new.tif").write_bytes(b"other process")
    with pytest.raises(RenameOptimizationUnavailable, match="destination already exists"):
        execute_local_rename(current, proven, primitive=fake_no_replace)
    assert (current / "new.tif").read_bytes() == b"other process"
    assert (current / "old.tif").read_bytes() == b"pixels"


def test_concurrent_destination_conflict_preserves_both_files(tmp_path: Path) -> None:
    _, current, proven = proven_pair(tmp_path)
    def racing_primitive(source_fd, source_name, destination_fd, destination_name):
        with open(destination_name, "wb", opener=lambda path, flags: os.open(path, flags, dir_fd=destination_fd)) as handle:
            handle.write(b"racer")
        raise RenameOptimizationUnavailable("destination appeared concurrently")
    with pytest.raises(RenameOptimizationUnavailable, match="concurrently"):
        execute_local_rename(current, proven, primitive=racing_primitive)
    assert (current / "new.tif").read_bytes() == b"racer"
    assert (current / "old.tif").read_bytes() == b"pixels"


def test_old_symlink_escape_is_rejected(tmp_path: Path) -> None:
    source, current = tmp_path / "source", tmp_path / "current"
    outside = tmp_path / "outside"
    source.mkdir(); current.mkdir(); outside.mkdir()
    (source / "new.tif").write_bytes(b"pixels")
    (outside / "photo.tif").write_bytes(b"pixels")
    (current / "link").symlink_to(outside, target_is_directory=True)
    item = PlanItem("rename_move_candidate", "new.tif", 6, "candidate", "link/photo.tif", 6)
    proven = prove_identity(source, current, item)
    assert proven is not None
    with pytest.raises(RenameOptimizationUnavailable):
        execute_local_rename(current, proven, primitive=fake_no_replace)
    assert (outside / "photo.tif").read_bytes() == b"pixels"


def test_new_parent_symlink_escape_is_rejected(tmp_path: Path) -> None:
    _, current, proven = proven_pair(tmp_path, "dest/photo.tif")
    outside = tmp_path / "outside"
    outside.mkdir()
    (current / "dest").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RenameOptimizationUnavailable):
        execute_local_rename(current, proven, primitive=fake_no_replace)
    assert not (outside / "photo.tif").exists()
    assert (current / "old.tif").exists()


def test_nested_safe_directory_is_created(tmp_path: Path) -> None:
    _, current, proven = proven_pair(tmp_path, "one/two/photo.tif")
    execute_local_rename(current, proven, primitive=fake_no_replace)
    assert (current / "one/two/photo.tif").read_bytes() == b"pixels"


def test_old_replaced_after_proof_is_rejected(tmp_path: Path) -> None:
    _, current, proven = proven_pair(tmp_path)
    (current / "old.tif").unlink()
    (current / "old.tif").write_bytes(b"unproved")
    with pytest.raises(RenameOptimizationUnavailable, match="proved regular file"):
        execute_local_rename(current, proven, primitive=fake_no_replace)
    assert not (current / "new.tif").exists()
    assert (current / "old.tif").read_bytes() == b"unproved"


def test_source_changed_after_proof_is_rejected(tmp_path: Path) -> None:
    source, current, proven = proven_pair(tmp_path)
    (source / "new.tif").write_bytes(b"changed")
    with pytest.raises(RenameOptimizationUnavailable, match="catalogue file changed"):
        execute_local_rename(current, proven, primitive=fake_no_replace)
    assert (current / "old.tif").exists()


def test_unsupported_primitive_never_falls_back_to_os_rename(tmp_path: Path) -> None:
    _, current, proven = proven_pair(tmp_path)
    def unsupported(*args):
        raise RenameOptimizationUnavailable("unsupported")
    with pytest.raises(RenameOptimizationUnavailable, match="unsupported"):
        execute_local_rename(current, proven, primitive=unsupported)
    assert (current / "old.tif").exists()
    assert not (current / "new.tif").exists()


def test_ambiguous_primitive_failure_is_not_treated_as_fallback(tmp_path: Path) -> None:
    _, current, proven = proven_pair(tmp_path)
    def ambiguous(source_fd, source_name, destination_fd, destination_name):
        os.unlink(source_name, dir_fd=source_fd)
        with open(destination_name, "wb", opener=lambda path, flags: os.open(path, flags, dir_fd=destination_fd)) as handle:
            handle.write(b"unknown")
        raise RuntimeError("unknown syscall outcome")
    with pytest.raises(RenameStateAmbiguous):
        execute_local_rename(current, proven, primitive=ambiguous)


def test_post_state_recognizes_success_despite_primitive_error(tmp_path: Path) -> None:
    _, current, proven = proven_pair(tmp_path)
    def moved_then_error(source_fd, source_name, destination_fd, destination_name):
        os.rename(source_name, destination_name, src_dir_fd=source_fd, dst_dir_fd=destination_fd)
        raise RuntimeError("lost syscall response")
    execute_local_rename(current, proven, primitive=moved_then_error)
    assert not (current / "old.tif").exists()
    assert (current / "new.tif").read_bytes() == b"pixels"


@pytest.mark.skipif(sys.platform != "darwin", reason="renameatx_np is macOS-specific")
def test_real_macos_rename_no_replace(tmp_path: Path) -> None:
    source, destination = tmp_path / "old", tmp_path / "new"
    source.write_bytes(b"proved")
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        rename_no_replace(directory_fd, source.name, directory_fd, destination.name)
        assert destination.read_bytes() == b"proved"
        source.write_bytes(b"second")
        with pytest.raises(RenameOptimizationUnavailable, match="destination already exists"):
            rename_no_replace(directory_fd, source.name, directory_fd, destination.name)
        assert destination.read_bytes() == b"proved"
        assert source.read_bytes() == b"second"
    finally:
        os.close(directory_fd)


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
