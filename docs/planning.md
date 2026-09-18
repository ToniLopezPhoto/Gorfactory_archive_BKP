# Non-destructive backup planning

`gorbackup plan` calculates and persists the operations a future backup would
perform before any archive content is changed.

```sh
gorbackup --config config/config.yaml plan
```

## Safety contract

The command runs the intended `rclone sync` shape with all of these protections:

- `--dry-run` is always present;
- JSON Lines logging is enabled with `--use-json-log`;
- the stable `--combined` report is captured separately;
- the source identity marker is excluded;
- the recent-file grace window is passed through `--min-age`;
- the future version-history location is supplied through `--backup-dir`.

No execution path removes, moves, overwrites or creates catalogue/archive content.
Only state files beneath `archive/state/` are written.

## Plan categories

Every item has a relative path, reason and byte size and is classified as:

- `new_file`: source path missing from `current/`;
- `changed_file`: source and current versions differ;
- `delete_from_current`: path present only in `current/`;
- `rename_move_candidate`: a unique new/deleted pair with equal size and exact
  modification time;
- `skipped_recent`: source file inside the configured grace window;
- `error`: comparison, parsing, reading or hashing failure.

Rename/move is intentionally a candidate, not a promise. The match is conservative
and remains visible with both the new path and the related old path.

## Exact safety totals

The plan records per-category file counts and byte totals, plus two independent
aggregates:

- `transfer_bytes`: bytes that would enter `current/`;
- `leaving_current_bytes`: existing bytes that would leave `current/`, including
  replaced versions and deletion candidates.

Changed files retain separate new and old sizes, so these aggregates remain exact
when a replacement changes size.

## Persistence

The plan and all path-level items are committed to SQLite against their `run_id`
before any later execution can consume them. A matching JSON summary is written to
`archive/state/manifests/plan-<run_id>.json`, with `latest-plan.json` as a readable
pointer to the most recent generated plan. Plans containing errors are persisted
as failed and the CLI returns a non-zero status.
