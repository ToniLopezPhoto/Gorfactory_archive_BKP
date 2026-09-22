from pathlib import Path

import pytest

from gorbackup.verification import (
    ExpectedSource,
    VerificationError,
    expand_manual_paths,
    hash_file,
    verify_file,
    verify_selected,
    verify_transfers,
)


def roots(tmp_path: Path):
    source = tmp_path / "source"
    destination = tmp_path / "current"
    source.mkdir()
    destination.mkdir()
    return source, destination


def test_identical_transfer_is_verified_with_sha256(tmp_path: Path) -> None:
    source, destination = roots(tmp_path)
    (source / "photo.tif").write_bytes(b"image-data")
    (destination / "photo.tif").write_bytes(b"image-data")
    stat = (source / "photo.tif").stat()

    result = verify_transfers(
        source, destination,
        [{"operation": "transfer", "path": "photo.tif"}],
        expected_sources={"photo.tif": ExpectedSource(stat.st_size, stat.st_mtime_ns)},
    )

    assert result.verified_files == 1
    assert result.verified_bytes == 10
    assert result.items[0].status == "verified"
    assert result.items[0].source_checksum == result.items[0].destination_checksum


def test_same_size_corruption_is_checksum_mismatch(tmp_path: Path) -> None:
    source, destination = roots(tmp_path)
    (source / "photo.raw").write_bytes(b"abcdef")
    (destination / "photo.raw").write_bytes(b"abcXef")

    item = verify_file(source, destination, "photo.raw")

    assert item.status == "mismatch"
    assert item.detail == "checksum mismatch"
    assert item.source_checksum != item.destination_checksum


def test_missing_destination_is_error(tmp_path: Path) -> None:
    source, destination = roots(tmp_path)
    (source / "missing.psd").write_bytes(b"data")

    item = verify_file(source, destination, "missing.psd")

    assert item.status == "error"
    assert "cannot resolve" in item.detail


def test_source_changed_since_plan_is_specific_status(tmp_path: Path) -> None:
    source, destination = roots(tmp_path)
    path = source / "changed.tif"
    path.write_bytes(b"before")
    before = path.stat()
    (destination / "changed.tif").write_bytes(b"before")
    path.write_bytes(b"after-change")

    item = verify_file(
        source, destination, "changed.tif",
        expected=ExpectedSource(before.st_size, before.st_mtime_ns),
    )

    assert item.status == "source_changed"
    assert "immutable plan" in item.detail


def test_source_change_during_hash_is_not_corruption(tmp_path: Path) -> None:
    source, destination = roots(tmp_path)
    source_path = source / "race.tif"
    source_path.write_bytes(b"same")
    (destination / "race.tif").write_bytes(b"same")

    def racing_hasher(path: Path):
        digest = hash_file(path)
        if path == source_path:
            path.write_bytes(b"changed")
        return digest

    item = verify_file(source, destination, "race.tif", hasher=racing_hasher)

    assert item.status == "source_changed"
    assert "while it was being hashed" in item.detail


def test_hash_file_reads_in_bounded_chunks(tmp_path: Path) -> None:
    path = tmp_path / "large.raw"
    path.write_bytes(b"x" * 25)
    reads = []

    class TrackingFile:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.wrapped.close()

        def read(self, size):
            reads.append(size)
            return self.wrapped.read(size)

    digest, count = hash_file(
        path, chunk_size=7,
        opener=lambda value, mode: TrackingFile(open(value, mode)),
    )

    assert len(digest) == 64
    assert count == 25
    assert reads == [7, 7, 7, 7, 7]


def test_multiple_and_mixed_transfers_have_correct_metrics(tmp_path: Path) -> None:
    source, destination = roots(tmp_path)
    for name, source_data, destination_data in (
        ("one.tif", b"one", b"one"),
        ("two.tif", b"two!", b"tw0!"),
    ):
        (source / name).write_bytes(source_data)
        (destination / name).write_bytes(destination_data)

    result = verify_transfers(source, destination, [
        {"operation": "transfer", "path": "one.tif"},
        {"operation": "archive", "path": "ignored.tif"},
        {"operation": "transfer", "path": "two.tif"},
    ])

    assert [item.status for item in result.items] == ["verified", "mismatch"]
    assert result.verified_files == 1
    assert result.verified_bytes == 3
    assert result.failures == 1


def test_manual_directory_is_recursive_and_reports_mismatch(tmp_path: Path) -> None:
    source, destination = roots(tmp_path)
    (source / "folder").mkdir()
    (destination / "folder").mkdir()
    (source / "folder" / "ok.tif").write_bytes(b"ok")
    (destination / "folder" / "ok.tif").write_bytes(b"ok")
    (source / "folder" / "bad.tif").write_bytes(b"bad")
    (destination / "folder" / "bad.tif").write_bytes(b"BAD")

    result = verify_selected(source, destination, ["folder"])

    assert {item.path for item in result.items} == {
        "folder/ok.tif", "folder/bad.tif"
    }
    assert result.failures == 1


@pytest.mark.parametrize("value", ["..", "../escape", "/absolute", "a/../../b"])
def test_manual_traversal_and_absolute_paths_are_rejected(
    tmp_path: Path, value: str
) -> None:
    source, _ = roots(tmp_path)
    with pytest.raises(VerificationError, match="unsafe relative path"):
        tuple(expand_manual_paths(source, [value]))


def test_manual_missing_path_is_an_error(tmp_path: Path) -> None:
    source, destination = roots(tmp_path)
    with pytest.raises(VerificationError, match="cannot resolve"):
        verify_selected(source, destination, ["missing.tif"])


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    source, destination = roots(tmp_path)
    outside = tmp_path / "outside.tif"
    outside.write_bytes(b"outside")
    try:
        (source / "escape.tif").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")

    item = verify_file(source, destination, "escape.tif")

    assert item.status == "error"
    assert "escapes configured root" in item.detail
