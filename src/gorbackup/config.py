"""Configuration loading and validation without filesystem side effects."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml


class ConfigError(ValueError):
    """Raised when a configuration file is missing or invalid."""


@dataclass(frozen=True)
class SourceConfig:
    path: Path
    mount_path: Path
    marker_file: str
    marker_id: str


@dataclass(frozen=True)
class ArchiveConfig:
    root: Path
    current_dir: str
    history_dir: str
    marker_file: str
    marker_id: str


@dataclass(frozen=True)
class SafetyConfig:
    max_deletes_per_run: int
    max_delete_size_gb: float
    min_free_space_percent: float
    ignore_recent_minutes: int
    min_source_size_gb: float
    min_source_size_ratio: float
    max_source_file_count_drop: int = 500
    max_source_file_count_drop_percent: float = 10.0
    max_source_bytes_drop_gb: float = 50.0
    max_source_bytes_drop_percent: float = 10.0
    max_changed_files_per_run: int = 2000
    max_changed_bytes_gb: float = 200.0
    max_changed_catalogue_percent: float = 25.0


@dataclass(frozen=True)
class RetentionConfig:
    auto_prune: bool
    keep_days: int = 365
    keep_min_runs: int = 30
    target_free_percent: Optional[float] = None


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    directory: Path = Path("logs")
    keep_days: int = 30


@dataclass(frozen=True)
class StateConfig:
    directory: Path = Path("state")
    manifests_dir: str = "manifests"
    ledger_file: str = "gorbackup.sqlite3"
    baseline_manifest: str = "baseline.json"
    baseline_report: str = "baseline-report.json"


@dataclass(frozen=True)
class RenameOptimizationConfig:
    enabled: bool = True
    require_hash: bool = True


@dataclass(frozen=True)
class AppConfig:
    source: SourceConfig
    archive: ArchiveConfig
    safety: SafetyConfig
    retention: RetentionConfig
    logging: LoggingConfig
    state: StateConfig = StateConfig()
    rename_optimization: RenameOptimizationConfig = RenameOptimizationConfig()

    @property
    def state_root(self) -> Path:
        """Return the state root, resolving relative paths below the archive."""
        if self.state.directory.is_absolute():
            return self.state.directory
        return self.archive.root / self.state.directory

    @property
    def manifests_root(self) -> Path:
        return self.state_root / self.state.manifests_dir

    @property
    def ledger_path(self) -> Path:
        return self.state_root / self.state.ledger_file


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
    state = raw.get("state", {})
    if not isinstance(state, Mapping):
        raise ConfigError("'state' must be a mapping")
    rename_optimization = raw.get("rename_optimization", {})
    if not isinstance(rename_optimization, Mapping):
        raise ConfigError("'rename_optimization' must be a mapping")
    rename_enabled = rename_optimization.get("enabled", True)
    rename_require_hash = rename_optimization.get("require_hash", True)
    if not isinstance(rename_enabled, bool):
        raise ConfigError("'rename_optimization.enabled' must be true or false")
    if rename_require_hash is not True:
        raise ConfigError("'rename_optimization.require_hash' must be true")

    source_path = _required(source, "source", "path")
    source_mount = _required(source, "source", "mount_path")
    source_marker = _required(source, "source", "marker_file")
    source_marker_id = _required(source, "source", "marker_id")
    archive_root = _required(archive, "archive", "root")
    current_dir = _required(archive, "archive", "current_dir")
    history_dir = _required(archive, "archive", "history_dir")
    for name, value in (
        ("source.path", source_path),
        ("source.mount_path", source_mount),
        ("source.marker_file", source_marker),
        ("source.marker_id", source_marker_id),
        ("archive.root", archive_root),
        ("archive.current_dir", current_dir),
        ("archive.history_dir", history_dir),
        ("archive.marker_file", _required(archive, "archive", "marker_file")),
        ("archive.marker_id", _required(archive, "archive", "marker_id")),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"'{name}' must be a non-empty string")
    for name, value in (
        ("source.marker_file", source_marker),
        ("archive.marker_file", archive["marker_file"]),
    ):
        if Path(value).name != value:
            raise ConfigError(f"'{name}' must be a filename, not a path")

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
    source_ratio = _positive_number(
        _required(safety, "safety", "min_source_size_ratio"),
        "safety.min_source_size_ratio",
    )
    if source_ratio > 1:
        raise ConfigError("'safety.min_source_size_ratio' must not exceed 1")

    integer_thresholds = {
        "max_source_file_count_drop": safety.get("max_source_file_count_drop", 500),
        "max_changed_files_per_run": safety.get("max_changed_files_per_run", 2000),
    }
    for key, value in integer_thresholds.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"'safety.{key}' must be an integer")
        _positive_number(value, f"safety.{key}", allow_zero=True)
    percent_thresholds = {
        "max_source_file_count_drop_percent": safety.get("max_source_file_count_drop_percent", 10),
        "max_source_bytes_drop_percent": safety.get("max_source_bytes_drop_percent", 10),
        "max_changed_catalogue_percent": safety.get("max_changed_catalogue_percent", 25),
    }
    for key, value in percent_thresholds.items():
        percent = _positive_number(value, f"safety.{key}", allow_zero=True)
        if percent > 100:
            raise ConfigError(f"'safety.{key}' must not exceed 100")

    auto_prune = _required(retention, "retention", "auto_prune")
    if not isinstance(auto_prune, bool):
        raise ConfigError("'retention.auto_prune' must be true or false")
    if auto_prune:
        raise ConfigError("automatic pruning is not enabled in this release; retention.auto_prune must be false")
    retention_keep_days = retention.get("keep_days", 365)
    retention_keep_min_runs = retention.get("keep_min_runs", 30)
    for name, value in (("keep_days", retention_keep_days), ("keep_min_runs", retention_keep_min_runs)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ConfigError(f"'retention.{name}' must be a non-negative integer")
    target_free_percent = retention.get("target_free_percent")
    if target_free_percent is not None:
        target_free_percent = _positive_number(
            target_free_percent, "retention.target_free_percent", allow_zero=True
        )
        if target_free_percent > 100:
            raise ConfigError("'retention.target_free_percent' must not exceed 100")

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

    state_directory = state.get("directory", "state")
    manifests_dir = state.get("manifests_dir", "manifests")
    ledger_file = state.get("ledger_file", "gorbackup.sqlite3")
    baseline_manifest = state.get("baseline_manifest", "baseline.json")
    baseline_report = state.get("baseline_report", "baseline-report.json")
    for name, value in (
        ("state.directory", state_directory),
        ("state.manifests_dir", manifests_dir),
        ("state.ledger_file", ledger_file),
        ("state.baseline_manifest", baseline_manifest),
        ("state.baseline_report", baseline_report),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"'{name}' must be a non-empty string")
    for name, value in (
        ("state.manifests_dir", manifests_dir),
        ("state.ledger_file", ledger_file),
        ("state.baseline_manifest", baseline_manifest),
        ("state.baseline_report", baseline_report),
    ):
        if Path(value).name != value:
            raise ConfigError(f"'{name}' must be a filename, not a path")

    return AppConfig(
        source=SourceConfig(
            Path(source_path), Path(source_mount), source_marker, source_marker_id
        ),
        archive=ArchiveConfig(
            Path(archive_root),
            current_dir,
            history_dir,
            archive["marker_file"],
            archive["marker_id"],
        ),
        safety=SafetyConfig(
            max_deletes_per_run=max_deletes,
            max_delete_size_gb=_positive_number(
                _required(safety, "safety", "max_delete_size_gb"),
                "safety.max_delete_size_gb",
                allow_zero=True,
            ),
            min_free_space_percent=free_percent,
            ignore_recent_minutes=ignore_recent,
            min_source_size_gb=_positive_number(
                _required(safety, "safety", "min_source_size_gb"),
                "safety.min_source_size_gb",
                allow_zero=True,
            ),
            min_source_size_ratio=source_ratio,
            max_source_file_count_drop=integer_thresholds["max_source_file_count_drop"],
            max_source_file_count_drop_percent=float(percent_thresholds["max_source_file_count_drop_percent"]),
            max_source_bytes_drop_gb=_positive_number(
                safety.get("max_source_bytes_drop_gb", 50),
                "safety.max_source_bytes_drop_gb", allow_zero=True,
            ),
            max_source_bytes_drop_percent=float(percent_thresholds["max_source_bytes_drop_percent"]),
            max_changed_files_per_run=integer_thresholds["max_changed_files_per_run"],
            max_changed_bytes_gb=_positive_number(
                safety.get("max_changed_bytes_gb", 200),
                "safety.max_changed_bytes_gb", allow_zero=True,
            ),
            max_changed_catalogue_percent=float(percent_thresholds["max_changed_catalogue_percent"]),
        ),
        retention=RetentionConfig(
            auto_prune, retention_keep_days, retention_keep_min_runs,
            target_free_percent,
        ),
        logging=LoggingConfig(level.upper(), Path(directory), keep_days),
        state=StateConfig(
            Path(state_directory),
            manifests_dir,
            ledger_file,
            baseline_manifest,
            baseline_report,
        ),
        rename_optimization=RenameOptimizationConfig(
            enabled=rename_enabled, require_hash=rename_require_hash
        ),
    )
