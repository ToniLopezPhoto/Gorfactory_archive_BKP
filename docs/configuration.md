# Configuration reference

Copy `config/example.yaml` to the ignored `config/config.yaml` before running an
operational command. Configuration is loaded and validated without checking,
creating, or modifying the paths it contains.

## Sections

- `source.path`: source directory to archive.
- `archive.root`: destination root. `current_dir` and `history_dir` are directory
  names below that root.
- `safety.max_deletes_per_run`: maximum number of deletions allowed in one run.
- `safety.max_delete_size_gb`: maximum combined deletion size in GiB.
- `safety.min_free_space_percent`: required free space from 0 through 100.
- `safety.ignore_recent_minutes`: age below which source changes are ignored.
- `retention.auto_prune`: whether future retention logic may remove old history;
  disabled in the example.
- `logging.level`: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`.
- `logging.directory`: local log directory.
- `logging.keep_days`: number of days of logs to retain.

The implementation never supplies organization-specific source or archive paths.
Local configuration, logs, state databases, and manifests are excluded by
`.gitignore`.
