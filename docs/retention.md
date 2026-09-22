# Retention and safe history pruning

`current/` is the live archive mirror. `history/` contains displaced file versions,
recorded by completed backup executions. Retention applies only to ledger-recorded files
under `history/`; it never cleans arbitrary content and never touches `current/`, state,
recovery, source, or known-good metadata.

The conservative default policy is:

```yaml
retention:
  auto_prune: false
  keep_days: 365
  keep_min_runs: 30
  target_free_percent: null
```

Automatic pruning is unavailable in this release. A backup never invokes prune, and
`auto_prune: true` is rejected so a YAML change cannot silently enable deletion.

## Plan, review, execute

```console
gorbackup -c config/config.yaml prune --dry-run
gorbackup -c config/config.yaml prune --execute PRUNE_ID --yes
```

Dry-run writes `state/manifests/prune-plan-<prune_id>.json` plus schema-v7 ledger
evidence. Each item identifies its backup run, logical and physical history path, bytes,
timestamp, eligibility reason, size, nanosecond mtime, device, inode, and any exact
version checksum. Its summary reports affected runs, protected versions, reclaimable
bytes, and current/projected free space. Projection is an estimate because filesystem
accounting can vary.

`--yes` is mandatory for execution. Execution applies exactly the reviewed plan: it
matches the manifest to immutable ledger evidence and prevalidates every path and
fingerprint before the first deletion. Stale, manipulated, missing, non-regular, or
symlinked paths abort before deletion.

## Selection and protections

A version is eligible only when its ledger execution item is an archive operation, its
backup completed as `success` or `warning`, its age exceeds `keep_days`, and its run is
not among the newest `keep_min_runs`. Latest known-good and runs referenced by running or
failed restores are protected. Failed, blocked, running, and incomplete backup runs are
also protected. Future audit leases will plug into `protected_history_runs()`.

`target_free_percent` limits the oldest already-eligible versions to those estimated to
reach the target. It never bypasses age, minimum-run, or special protections.

## Concurrency and failures

Restore holds shared `state/.history-access.lock`; backup and prune hold it exclusively.
Prune uses fixed lock order `BackupLock` then `HistoryLock(EXCLUSIVE)`. Thus concurrent
restores may coexist, but no restore can race an unlink and backup cannot race prune.

Files are unlinked individually in deterministic order. Empty parents may be removed up
through their run directory, never above it. Unknown files remain untouched and prevent
directory cleanup. A mid-run failure marks the prune failed, preserves exact per-item
states, and writes `prune-execution-<id>.json`; no rollback is claimed.

History reports intentional removal as `pruned <timestamp> by prune <id>`. Unaccounted
absence remains `recorded but missing`. Restore of a pruned version fails specifically
with `historical version was deliberately pruned by <id>`.
