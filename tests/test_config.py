from pathlib import Path

import pytest

from gorbackup.config import ConfigError, load_config


VALID_CONFIG = """
source:
  path: /source
  mount_path: /source-mount
  marker_file: .source-id
  marker_id: expected-source
archive:
  root: /archive
  current_dir: current
  history_dir: history
  marker_file: .archive-id
  marker_id: expected-archive
safety:
  max_deletes_per_run: 10
  max_delete_size_gb: 2.5
  min_free_space_percent: 15
  ignore_recent_minutes: 5
  min_source_size_gb: 1
  min_source_size_ratio: 0.8
retention:
  auto_prune: false
"""


def test_load_config_does_not_require_configured_paths(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(VALID_CONFIG, encoding="utf-8")

    config = load_config(path)

    assert config.source.path == Path("/source")
    assert config.source.mount_path == Path("/source-mount")
    assert config.archive.root == Path("/archive")
    assert config.safety.max_delete_size_gb == 2.5
    assert config.retention.auto_prune is False


def test_invalid_source_ratio_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIG.replace("min_source_size_ratio: 0.8", "min_source_size_ratio: 1.1"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="must not exceed 1"):
        load_config(path)


def test_missing_file_has_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="configuration file not found"):
        load_config(tmp_path / "missing.yaml")


def test_invalid_threshold_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIG.replace("min_free_space_percent: 15", "min_free_space_percent: 101"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="must not exceed 100"):
        load_config(path)
