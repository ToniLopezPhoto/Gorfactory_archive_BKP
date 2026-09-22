# History and safe recovery

`gorbackup history` and `gorbackup restore` provide a non-destructive recovery
workflow. They never write to the SAM source, and restore does not write to
`current/` or remove anything from `history/`.

## Find a version

```sh
gorbackup --config config/config.yaml history "Campaign/photo.tif"
```

The path must be relative to the catalogue. The command lists the copy in
`current/`, when present, followed by archived versions from newest to oldest.
Each archived row includes its backup run ID, run date and status, byte size,
known checksum (when one belongs to that exact version), reason, and physical
availability. `recorded but missing` means the ledger has evidence for a
version whose historical file is no longer present; that version cannot be
restored.

History discovery is read-only. It uses the SQLite ledger as its primary index
and checks the corresponding files on disk; it does not scan history as a
substitute for missing ledger evidence.

## Restore a selected version

Choose the run ID shown by `history`, then run:

```sh
gorbackup --config config/config.yaml restore "Campaign/photo.tif" --run <run_id>
```

The default output is:

```text
<archive>/recovery/<restore_id>/Campaign/photo.tif
```

The command streams a copy to a diagnostic temporary file, verifies size and
SHA-256, and only then publishes the final recovery file atomically. A known
SHA-256 belonging to that historical version is also checked. If no historical
checksum was persisted, gorbackup calculates SHA-256 for both the historical
source and restored copy. The restore is successful only when they match.

An existing final file is never overwritten. A failed copy or verification
leaves no final file; its temporary file may remain next to the intended output
for diagnosis.

An explicit external staging root is supported:

```sh
gorbackup --config config/config.yaml restore "Campaign/photo.tif" \
  --run <run_id> --destination /absolute/safe/staging
```

Explicit destinations must be absolute and outside the archive. Destinations
inside the SAM source, `current/`, `history/`, `state/`, or `manifests/` are
rejected. Direct restore to the SAM or current mirror is intentionally not
supported.

Every started copy is recorded separately in `restore_runs` with running,
success, or failed status. Restore does not alter backup-run metrics or
known-good state. The selected file remains unchanged in `history/` after
recovery.

Restore does not take the backup lock: it only reads history belonging to a
backup run whose final status is `success` or `warning`, and it writes to a
separate staging area. Runs that are `running`, `blocked`, or `failed` are not
eligible, which prevents recovery from a history directory still being
written. `history` may display their recorded evidence and status for
diagnosis, but remains read-only.
