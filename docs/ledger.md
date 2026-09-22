# Ledger model

The current schema version is **6**. It adds explicit `rename` execution
evidence (`path` is new and `related_path` is old), plus planned/executed rename
file and byte metrics. Migration from v5 preserves existing evidence.

The SQLite ledger separates six stages that must never be treated as
interchangeable:

1. **catalogue** — a complete metadata inventory of the source;
2. **plan** — immutable operations reported by a dry-run;
3. **execution** — successful path operations reported by the real rclone run;
4. **reconciliation** — the comparison between planned and executed operations;
5. **verification** — SHA-256 evidence for each actual transfer or rename;
6. **known-good state** — the catalogue snapshot promoted only after exact,
successful reconciliation and verification.

When an exact backup skips recent files, promotion builds the protected snapshot:
non-recent plan metadata replaces known-good normally, previous known-good
metadata is retained for an already-protected skipped path, and a newly created
skipped path is omitted. Consequently known-good never claims that an excluded
new version was copied.

## Metrics have one meaning

Every run exposes explicit counters. Catalogue inventory uses
`catalogue_file_count` and `catalogue_total_bytes`. Plans use
`planned_transfer_files/bytes` and `planned_archive_files/bytes`. Real execution
uses `executed_transfer_files/bytes` and `executed_archive_files/bytes`.

There is no generic `file_count` or `total_bytes`: those names previously mixed
inventory, intent, and outcome. Path evidence is retained in `plan_items`,
`safety_assessments`, `execution_items`, `reconciliation_items`, and
`verification_items`. The latter records method, status, verified bytes, both
digests, and diagnostic detail with an index on `(run_id, status)`.

## Transaction and publication rules

`gorbackup scan` stages the complete source walk and replaces `catalogue_files`
only in the transaction that marks the scan successful. A failed or interrupted
scan leaves the previous catalogue snapshot intact.

A plan stores its own complete catalogue snapshot and operation list. The real
run writes a separate `execution-<run-id>.json`; dry-run output is never reused
as execution evidence. Execution rows are persisted before verification so
`execution_items`, rather than plan intent, define the checksum scope. Only an
execution with status `success`, reconciliation `exact`, and no verification
failure can replace `known_good_files` and `known_good_state`. `blocked`,
`warning`, `failed`, incomplete (`running`), or unparseable runs retain their
evidence but cannot advance known-good state. A blocked run points to its immutable
plan and stores the complete original assessment, failed gates, override request,
override decision, measured values, thresholds, and human-readable message.

## Schema migration

Schema v4 adds path-level verification evidence. Because the project remains
pre-production, opening a v1, v2, or v3 database atomically replaces its state
tables with the v4 schema (`PRAGMA user_version = 4`). The next scan/plan rebuilds state;
no ambiguous legacy metric is guessed into a new semantic field.
