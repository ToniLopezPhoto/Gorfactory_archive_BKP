# Versioned execution and reconciliation

`gorbackup backup` creates a fresh immutable plan, applies deletion/capacity
safety gates, reserves a unique history directory, and then runs real rclone.
Displaced files are directed to `history/<backup-run-id>/`; the source marker and
recent-file window remain excluded. Tests and CI use only synthetic temporary
trees and fake rclone reports.

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

Warnings describe what actually happened and publish a degraded backup manifest,
but cannot advance known-good state. Failures persist execution and reconciliation
evidence and do not publish a success backup manifest. Existing history content is
left for recovery and investigation.

```sh
gorbackup --config config/config.yaml backup
```
