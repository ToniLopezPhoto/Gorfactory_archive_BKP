# Non-destructive backup planning

`gorbackup plan` inventories the catalogue and runs the intended `rclone sync`
with `--dry-run`, JSON logging, a separate combined report, the source marker
excluded, the recent-file window, and the future `--backup-dir`. It never changes
source or archive content.

The immutable per-run manifest and SQLite rows contain:

- `catalogue_file_count` and `catalogue_total_bytes`;
- `planned_transfer_files` and `planned_transfer_bytes`;
- `planned_archive_files` and `planned_archive_bytes`;
- path-level `new_file`, `changed_file`, `delete_from_current`,
  `rename_move_candidate`, `skipped_recent`, and `error` items.

Changed paths retain both incoming `size` and outgoing `leaving_size`. A rename is
only a conservative candidate based on unique size and modification-time equality.
Plans containing comparison, parse, or filesystem errors are persisted as failed
and cannot execute.

```sh
gorbackup --config config/config.yaml plan
```
