# Versioned execution and reconciliation

After CLI preflight, `gorbackup backup` atomically acquires
`<archive>/state/backup.lock`, creates a fresh immutable plan, applies the safety gates below,
persists the assessment, reserves a unique history directory, and only then runs
real rclone.
Displaced files are directed to `history/<backup-run-id>/`; the source marker and
recent-file window remain excluded. Tests and CI use only synthetic temporary
trees and fake rclone reports.

## Single-run lock

The lock is held from before planning until execution, reconciliation, manifest
publication, and known-good promotion have finished. It is released by a context
manager on success, a safety block, rclone/reconciliation failure, manifest
failure, or an unexpected exception. Thus two `backup` processes cannot reach a
real `rclone sync` concurrently. Standalone read-only/preparatory commands are not
locked; future mutable restore or prune operations must reuse this mechanism when
they are implemented. `baseline --reconcile` remains outside the scope of this
lock in the current release.

The JSON lock records schema version, PID, hostname, acquisition timestamp,
attempt identifier, and a random ownership token. Creation uses exclusive
filesystem creation rather than an existence check. A contender on the same host
checks the PID without signalling it: an existing or permission-protected PID is
active regardless of lock age, while a nonexistent PID is stale and may be
recovered. A different hostname is unverifiable and therefore fails closed.
Corrupt locks also fail closed and must be investigated, never silently removed.
Release verifies the ownership token (and file identity), so an old process cannot
delete a replacement lock. Lock contention returns a non-zero operational error
with owner PID, hostname, and timestamp, and creates no ledger run because the
contender never obtained backup ownership.

## Recent-file grace window

`safety.ignore_recent_minutes` is applied only to regular source files, never to
directories. The plan explicitly emits each such file as `skipped_recent` and
removes it from actionable transfer/archive operations in addition to passing
the equivalent `--min-age` filter to rclone. An old sibling in the same directory
remains eligible. Skipped paths therefore neither transfer nor create false
reconciliation divergence, and become eligible on a later run after aging beyond
the cutoff.

A successful run may contain skipped recent files. Known-good promotion excludes
a new skipped path and retains the previous known-good metadata for a skipped
path that was already protected. It never promotes the recent source metadata as
backed up; known-good counts and bytes describe the merged, actually protected
snapshot.

## Safety gates

Every assessment records the measured value and configured threshold for four
separate anomaly classes:

- `delete_count` and `delete_bytes` limit paths displaced from `current/`;
- `catalogue_file_count_shrink` and `catalogue_bytes_shrink` compare the current
  source catalogue with the latest successfully reconciled `known_good_state`;
  the adopted baseline is used only until that state exists;
- `high_change_count`, `high_change_bytes`, and `high_change_ratio` detect mass
  additions, rewrites, and moves even when few or no paths are deleted;
- `archive_capacity` and `archive_free_percent` protect the destination.

The assessment includes current/reference counts and bytes, signed absolute
deltas, percentage deltas, transfer volume, deletion volume, and projected free
space. Limits are configured under `safety` rather than embedded in code.

If any gate fails, the backup run is persisted as `blocked` with its immutable
`plan_run_id`, full assessment, failed gates, thresholds, and readable messages.
No history directory is reserved, `current/` is untouched, the real rclone sync is
not invoked, and known-good state cannot advance.

## Manual override

An operator who has independently verified a legitimate mass change may use:

```sh
gorbackup --config config/config.yaml backup --override-safety
```

This flag is accepted only from an interactive terminal. The ledger retains the
original failed assessment and records the exact gates overridden. It applies
only to deletion, catalogue-shrink, and high-change-volume gates. Identity/path
preflight, structural plan validity, destination-capacity gates, execution errors,
and reconciliation failures remain blocking. Automation must never supply this
flag: unattended input/output is rejected even when the argument is present.

## Independent execution evidence

The real command emits a new JSON log. Successful copy and versioning events are
parsed into path-level `execution_items`; errors, unsafe paths, invalid sizes, and
unknown operation types are not silently counted. An independent
`execution-<run-id>.json` is written before the backup summary. The backup summary
reports executed counters from these events, never from dry-run totals.
Every recorded archive event is also checked against the real history tree;
missing, extra, or differently-sized history files fail reconciliation.

## Reconciliation policy

Each `(operation, path, bytes)` is compared with the immutable plan:

- exact agreement is `success` and may promote known-good state;
- a new/unexpected transfer, or a planned transfer/archive that no longer occurs,
  is `warning` (typical source addition, removal, or recent-window transition);
- a changed transfer classification is `warning`;
- byte mismatch, duplicate successful event, unplanned archive/delete, direct
  deletion instead of versioning, rclone error, or invalid/ambiguous evidence is
  `failed`.

The four run outcomes are intentionally distinct: `blocked` means no execution
was allowed; `warning` means execution completed with non-fatal divergence;
`failed` means execution or reconciliation failed; `success` means exact
reconciliation and is the only state eligible for known-good promotion.

Warnings describe what actually happened and publish a degraded backup manifest,
but cannot advance known-good state. Failures persist execution and reconciliation
evidence and do not publish a success backup manifest. Existing history content is
left for recovery and investigation.

```sh
gorbackup --config config/config.yaml backup
```
