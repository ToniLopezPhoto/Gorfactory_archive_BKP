"""Configuration loading and validation without filesystem side effects."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


class ConfigError(ValueError):
    """Raised when a configuration file is missing or invalid."""


@dataclass(frozen=True)
class SourceConfig:
    path: Path


@dataclass(frozen=True)
class ArchiveConfig:
    root: Path
    current_dir: str
    history_dir: str


@dataclass(frozen=True)
class SafetyConfig:
    max_deletes_per_run: int
    max_delete_size_gb: float
    min_free_space_percent: float
    ignore_recent_minutes: int


@dataclass(frozen=True)
class RetentionConfig:
    auto_prune: bool


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    directory: Path = Path("logs")
    keep_days: int = 30


@dataclass(frozen=True)
class AppConfig:
    source: SourceConfig
    archive: ArchiveConfig
    safety: SafetyConfig
    retention: RetentionConfig
    logging: LoggingConfig


def _section(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = data.get(name)
    if not isinstance(value, Mapping):
        raise ConfigError(f"'{name}' must be a mapping")
    return value


def _required(section: Mapping[str, Any], section_name: str, key: str) -> Any:
    if key not in section:
        raise ConfigError(f"missing required setting: {section_name}.{key}")
    return section[key]


def _positive_number(value: Any, name: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"'{name}' must be a number")
    if value < 0 or (value == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "greater than zero"
        raise ConfigError(f"'{name}' must be {qualifier}")
    return float(value)


def load_config(path: Path) -> AppConfig:
    """Parse and validate *path* without accessing configured locations."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc

    if not isinstance(raw, Mapping):
        raise ConfigError("configuration root must be a mapping")

    source = _section(raw, "source")
    archive = _section(raw, "archive")
    safety = _section(raw, "safety")
    retention = _section(raw, "retention")
    logging = raw.get("logging", {})
    if not isinstance(logging, Mapping):
        raise ConfigError("'logging' must be a mapping")

    source_path = _required(source, "source", "path")
    archive_root = _required(archive, "archive", "root")
    current_dir = _required(archive, "archive", "current_dir")
    history_dir = _required(archive, "archive", "history_dir")
    for name, value in (
        ("source.path", source_path),
        ("archive.root", archive_root),
        ("archive.current_dir", current_dir),
        ("archive.history_dir", history_dir),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"'{name}' must be a non-empty string")

    max_deletes = _required(safety, "safety", "max_deletes_per_run")
    ignore_recent = _required(safety, "safety", "ignore_recent_minutes")
    if isinstance(max_deletes, bool) or not isinstance(max_deletes, int):
        raise ConfigError("'safety.max_deletes_per_run' must be an integer")
    if isinstance(ignore_recent, bool) or not isinstance(ignore_recent, int):
        raise ConfigError("'safety.ignore_recent_minutes' must be an integer")
    _positive_number(max_deletes, "safety.max_deletes_per_run", allow_zero=True)
    _positive_number(ignore_recent, "safety.ignore_recent_minutes", allow_zero=True)

    free_percent = _positive_number(
        _required(safety, "safety", "min_free_space_percent"),
        "safety.min_free_space_percent",
        allow_zero=True,
    )
    if free_percent > 100:
        raise ConfigError("'safety.min_free_space_percent' must not exceed 100")

    auto_prune = _required(retention, "retention", "auto_prune")
    if not isinstance(auto_prune, bool):
        raise ConfigError("'retention.auto_prune' must be true or false")

    level = logging.get("level", "INFO")
    directory = logging.get("directory", "logs")
    keep_days = logging.get("keep_days", 30)
    if not isinstance(level, str) or level.upper() not in {
        "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"
    }:
        raise ConfigError("'logging.level' is not a supported log level")
    if not isinstance(directory, str) or not directory.strip():
        raise ConfigError("'logging.directory' must be a non-empty string")
    if isinstance(keep_days, bool) or not isinstance(keep_days, int) or keep_days < 0:
        raise ConfigError("'logging.keep_days' must be a non-negative integer")

    return AppConfig(
        source=SourceConfig(Path(source_path)),
        archive=ArchiveConfig(Path(archive_root), current_dir, history_dir),
        safety=SafetyConfig(
            max_deletes_per_run=max_deletes,
            max_delete_size_gb=_positive_number(
                _required(safety, "safety", "max_delete_size_gb"),
                "safety.max_delete_size_gb",
                allow_zero=True,
            ),
            min_free_space_percent=free_percent,
            ignore_recent_minutes=ignore_recent,
        ),
        retention=RetentionConfig(auto_prune=auto_prune),
        logging=LoggingConfig(level.upper(), Path(directory), keep_days),
    )
