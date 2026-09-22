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

The planner compares its before/after inventories path by path. A stable file
that changes, disappears, or appears without a demonstrably recent timestamp
still fails the immutable plan. A path already inside the fixed grace cutoff may
change or disappear, and a demonstrably recent path may appear; these cases are
represented once as `skipped_recent` using the latest available metadata. The
path remains explicitly excluded from live execution even when it disappeared
during planning. This exception is deliberately narrow: a previously stable file
that begins changing during the dry-run fails closed rather than being reclassified
as recent.

```sh
gorbackup --config config/config.yaml plan
```

## Rename candidates

`rename_move_candidate` means only that one source-only and one current-only file
share size and nanosecond mtime. Those fields reduce the search space; they never
prove identity. Candidates use separate `planned_rename_files` and
`planned_rename_bytes` metrics and are not counted as planned transfer bytes.
Ambiguous groups remain ordinary additions/deletions. Recent paths are excluded
before candidate detection and can never be optimized.
