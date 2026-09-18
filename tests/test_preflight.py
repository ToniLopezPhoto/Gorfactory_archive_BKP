from pathlib import Path
from types import SimpleNamespace

import pytest

from gorbackup.config import (
    AppConfig,
    ArchiveConfig,
    LoggingConfig,
    RetentionConfig,
    SafetyConfig,
    SourceConfig,
)
from gorbackup.preflight import PreflightError, SourceSummary, run_preflight


def make_config(tmp_path: Path) -> AppConfig:
    source_mount = tmp_path / "source-volume"
    source = source_mount / "catalogue"
    archive = tmp_path / "archive-volume"
    source.mkdir(parents=True)
    archive.mkdir()
    (archive / "current").mkdir()
    (archive / "history").mkdir()
    (source / ".source-id").write_text("expected-source\n", encoding="utf-8")
    (archive / ".archive-id").write_text("expected-archive\n", encoding="utf-8")
    (source / "photo.tif").write_bytes(b"photo-data")
    return AppConfig(
        source=SourceConfig(
            source, source_mount, ".source-id", "expected-source"
        ),
        archive=ArchiveConfig(
            archive, "current", "history", ".archive-id", "expected-archive"
        ),
        safety=SafetyConfig(10, 1.0, 15.0, 5, 0.0, 0.8),
        retention=RetentionConfig(False),
        logging=LoggingConfig(),
    )


def run_valid(config: AppConfig, **kwargs):
    return run_preflight(
        config,
        is_mount=lambda path: True,
        access=lambda path, mode: True,
        disk_usage=lambda path: SimpleNamespace(total=100, used=20, free=80),
        same_filesystem=lambda source, archive: False,
        **kwargs,
    )


def test_valid_preflight_is_read_only_and_returns_summary(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    original_paths = sorted(config.source.path.rglob("*"))

    result = run_valid(config)

    assert result.source == SourceSummary(file_count=1, total_size_bytes=10)
    assert result.destination_free_percent == 80.0
    assert sorted(config.source.path.rglob("*")) == original_paths


def test_unmounted_source_aborts(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    with pytest.raises(PreflightError, match="source mount is unavailable"):
        run_preflight(
            config,
            is_mount=lambda path: path == config.archive.root,
            access=lambda path, mode: True,
            disk_usage=lambda path: SimpleNamespace(total=100, used=20, free=80),
            same_filesystem=lambda source, archive: False,
        )


def test_wrong_archive_marker_aborts(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    (config.archive.root / config.archive.marker_file).write_text(
        "another-disk", encoding="utf-8"
    )

    with pytest.raises(PreflightError, match="archive identity mismatch"):
        run_valid(config)


def test_low_free_space_aborts(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    with pytest.raises(PreflightError, match="below the configured minimum"):
        run_preflight(
            config,
            is_mount=lambda path: True,
            access=lambda path, mode: True,
            disk_usage=lambda path: SimpleNamespace(total=100, used=90, free=10),
            same_filesystem=lambda source, archive: False,
        )


def test_empty_source_aborts(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    (config.source.path / "photo.tif").unlink()

    with pytest.raises(PreflightError, match="source contains no data files"):
        run_valid(config)


def test_source_smaller_than_baseline_aborts(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    with pytest.raises(PreflightError, match="last known-good manifest"):
        run_valid(config, baseline=SourceSummary(100, 10_000))


def test_same_filesystem_aborts(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    with pytest.raises(PreflightError, match="same path or filesystem"):
        run_preflight(
            config,
            is_mount=lambda path: True,
            access=lambda path, mode: True,
            disk_usage=lambda path: SimpleNamespace(total=100, used=20, free=80),
            same_filesystem=lambda source, archive: True,
        )


def test_missing_archive_structure_aborts(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    (config.archive.root / "history").rmdir()

    with pytest.raises(PreflightError, match="required archive directory is missing"):
        run_valid(config)
