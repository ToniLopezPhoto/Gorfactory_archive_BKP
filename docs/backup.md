# Versioned incremental backup

`gorbackup backup` always generates a fresh dry-run plan before it executes an
incremental `rclone sync`. If planning reports an error, execution is refused.

Before execution, the plan must also pass every configured safety gate:

- destination-only deletions (including rename candidates) must not exceed
  `safety.max_deletes_per_run`;
- their combined size must not exceed `safety.max_delete_size_gb`;
- planned transfers must fit while retaining
  `safety.min_free_space_percent` after the run.

The deletion limits are also supplied to the real rclone command through
`--max-delete` and `--max-delete-size`. This second layer aborts if the source or
destination changes after planning and the live sync crosses a limit.

For every successful run, files that would be overwritten or removed from
`current/` are moved, with their original relative paths, to:

```text
history/<backup-run-id>/
```

The run ID is unique and an existing history directory is never reused. The
source marker is excluded, the configured recent-file window is preserved, and
the source is only ever passed to rclone as the sync source.

The SQLite ledger records both the planning and backup runs. A JSON manifest is
written to `state/manifests/backup-<run-id>.json` and mirrored as
`latest-backup.json`; it links to the exact plan and records transferred and
archived counts and bytes plus the accepted safety assessment.

```sh
gorbackup --config config/config.yaml backup
```

A non-zero rclone result or structured error log marks the backup run failed and
does not publish a success manifest. Files already moved by rclone remain in the
run's history directory for recovery and investigation.
