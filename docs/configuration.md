# Configuration reference

Copy `config/example.yaml` to the ignored `config/config.yaml` before running an
operational command. Configuration is loaded and validated without checking,
creating, or modifying the paths it contains.

## Sections

- `source.path`: source directory to archive.
- `source.mount_path`: mount point of the SAM volume containing `source.path`.
- `source.marker_file` and `source.marker_id`: marker filename at `source.path`
  and its exact expected text. The marker must be provisioned by an operator;
  `gorbackup` only reads it.
- `archive.root`: mounted destination root. `current_dir` and `history_dir` are
  required directory names below that root.
- `archive.marker_file` and `archive.marker_id`: separate marker filename at the
  archive root and its exact expected text.
- `safety.max_deletes_per_run`: maximum number of deletions allowed in one run.
- `safety.max_delete_size_gb`: maximum combined deletion size in GiB.
- `safety.min_free_space_percent`: required free space from 0 through 100.
- `safety.ignore_recent_minutes`: age below which source changes are ignored.
- `safety.min_source_size_gb`: absolute minimum source size in GiB. This catches
  an empty or implausibly small source before a baseline manifest is available.
- `safety.min_source_size_ratio`: minimum fraction (greater than 0 through 1) of both the file
  count and byte size used by baseline-aware scan/plan preflight.
- `safety.max_source_file_count_drop` and
  `safety.max_source_file_count_drop_percent`: absolute and percentage limits for
  file-count shrink against latest known-good (or initial baseline as fallback).
- `safety.max_source_bytes_drop_gb` and
  `safety.max_source_bytes_drop_percent`: equivalent source byte-shrink limits.
- `safety.max_changed_files_per_run`: maximum planned new, modified, or moved files.
- `safety.max_changed_bytes_gb`: maximum bytes in those planned changes.
- `safety.max_changed_catalogue_percent`: maximum changed-file proportion relative
  to the reference catalogue.
- `retention.auto_prune`: whether future retention logic may remove old history;
  disabled in the example.
- `logging.level`: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`.
- `logging.directory`: local log directory.
- `logging.keep_days`: number of days of logs to retain.
- `state.directory`: state directory below `archive.root`. Absolute paths are
  accepted only when they still resolve inside the archive root.
- `state.manifests_dir`: human-readable manifest summary directory below state.
- `state.ledger_file`: SQLite current-state and run-history filename.
- `state.baseline_manifest`: known-good baseline summary filename.
- `state.baseline_report`: latest path-level comparison report filename.

The implementation never supplies organization-specific source or archive paths.
Local configuration, logs, state databases, and manifests are excluded by
`.gitignore`.

## Preflight behavior

Before `backup` or `baseline`, `gorbackup` verifies both mounts and identity
markers, source readability, archive writability and required directories,
separate filesystems, free space, and absolute source plausibility. After the
immutable backup plan exists, safety compares its catalogue totals primarily with
the ledger's latest known-good state and uses the adopted baseline only if no
successful backup has yet been promoted. All checks are read-only with respect to
the source. Critical preflight failures stop before planning; safety-gate failures
are recorded as blocked attempts before any data operation can start.
