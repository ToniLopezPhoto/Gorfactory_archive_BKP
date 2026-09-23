# Versioned execution and reconciliation

After CLI preflight, `gorbackup backup` atomically acquires
`<archive>/state/backup.lock`, creates a fresh immutable plan, applies the safety gates below,
persists the assessment, reserves a unique history directory, and only then runs
real rclone. After reconciling the real execution, it verifies every transferred
file cryptographically before publishing the manifest or promoting known-good.
Displaced files are directed to `history/<backup-run-id>/`; the source marker and
recent-file window remain excluded. Tests and CI use only synthetic temporary
trees and fake rclone reports.

## Single-run lock

The lock is held from before planning until execution, reconciliation, verification, manifest
publication, and known-good promotion have finished. It is released by a context
manager on success, a safety block, rclone/reconciliation failure, manifest
failure, or an unexpected exception. Thus two `backup` processes cannot reach a
real `rclone sync` concurrently. Backup then takes `.history-access.lock`
exclusively; prune uses the same lock order (`BackupLock` then history exclusive),
while restores use a shared history lock. `baseline --reconcile` remains outside
the scope of this lock in the current release.

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
but cannot advance known-good state. Failures persist execution, reconciliation,
and verification evidence and publish a failed diagnostic manifest, never a
success manifest. Existing `current/` and history content is left untouched for
recovery and investigation; this stage performs no destructive rollback.

## Post-transfer verification

`backup: success` means rclone completed, execution exactly matched the immutable
plan, and every `operation=transfer` row persisted for this real run was verified.
Only those actual new/replaced files are read. Planned-but-not-transferred,
`skipped_recent`, unchanged files, and the rest of the multi-terabyte catalogue
are not rehashed, so daily cost scales with transferred bytes rather than archive
size.

For the current mounted-filesystem deployment, verification checks existence and
size, then streams source and `current/` through SHA-256 in bounded chunks. Size
and mtime are never accepted as content proof. Results are stored per path in
SQLite and in `verification-<run-id>.json`; the backup manifest contains method,
verified file/byte totals, failure count, and the report path. A mismatch, missing
file, read error, or unverifiable path makes the run `failed`, returns non-zero,
and prevents known-good promotion.

Source metadata from the immutable plan is checked before hashing, and source
identity/size/mtime are checked again after reading. A source that changed after
transfer or during hashing is classified `source_changed`, not mislabeled as
destination corruption, but still cannot be promoted as verified. Destination
changes during hashing are likewise rejected. The backup lock remains held for
this entire decision.

Verified transfers write `sha256:<digest>` into `known_good_files`. Checksums for
unchanged protected files are retained when their size and mtime still match;
the prior metadata and checksum for `skipped_recent` remain intact.

## Manual selected-path verification

The read-only command compares explicitly selected source paths with
`archive/current`; directory arguments recurse through regular files:

```sh
gorbackup --config config/config.yaml verify project/photo.tif
gorbackup --config config/config.yaml verify project/session-2026
```

At least one relative path is mandatory, preventing an accidental full-catalogue
hash. Absolute paths, `..`, root escapes, and symlink escapes fail closed. Exit
status is zero only when all selected files match; mismatch, absence, unsafe path,
or read error is non-zero. This command does not update source, `current/`, the
ledger, or known-good state.

```sh
gorbackup --config config/config.yaml backup
```

## Rename optimization

Gorbackup probes both endpoints with `rclone backend features`. Optimization is
enabled only when both are local, they advertise a common content hash, and the
destination advertises `Move`. Each candidate is then proved independently by
stable SHA-256 reads of `source/new` and `current/old`. Only an exact match is
moved atomically inside `current`; every unavailable or inconclusive case keeps
the normal `archive old + transfer new` fallback.

On macOS the move uses `renameatx_np(..., RENAME_EXCL)` through directory file
descriptors opened with `O_NOFOLLOW`. Existing or concurrently-created targets
therefore cannot be replaced, and symlinked path components cannot redirect the
move outside `current/`. Platforms without a native atomic no-replace primitive
disable this optimization and retain the normal fallback. Source and old-file
fingerprints are revalidated immediately before the syscall; its post-state is
also checked. An ambiguous result fails the run and retains the rename journal.

Native `--track-renames` is intentionally not enabled: its global matching can
act outside the immutable candidate mapping, and its destination-side move does
not create a physical old-path copy in `--backup-dir`. An optimized action is
recorded as `rename`, with `path=new`, `related_path=old`, and classification
`optimized_move`. Transfers and renames each require exactly one SHA-256
verification result before success and known-good promotion.

An fsynced rename journal is written before destination moves. A crash before
durable ledger evidence leaves the journal in state and blocks the next backup
for operator reconciliation. Hash proof plus post-move verification adds local
I/O, but avoids retransferring large unchanged files from the SAM.
