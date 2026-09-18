# Metadata inventory and run ledger

`gorbackup scan` inventories the source catalogue without opening or hashing file
contents. For every regular file it records the relative path, byte size and
nanosecond modification time. A checksum remains optional and a previously known
checksum is retained while size and modification time remain unchanged.

## Run a scan

With the expected source and archive volumes mounted:

```sh
gorbackup --config config/config.yaml scan
```

The normal preflight checks run first. SQLite state is stored at:

```text
<archive.root>/<state.directory>/<state.ledger_file>
```

Readable summaries are written beneath `state.manifests_dir` as an immutable
per-run JSON file and `latest-scan.json`.

## Transactional safety

Each scan receives a unique run ID and is initially recorded as `running`. File
metadata is written to a run-specific staging table in batches. Only after the
entire source traversal succeeds does one SQLite transaction:

1. calculate added, modified and deleted paths;
2. replace the current inventory;
3. mark the run successful with final totals.

If traversal fails or the SAM disappears, staging is discarded, the run is marked
`failed`, and the previous known-good inventory remains unchanged. An interrupted
process leaves a distinguishable `running` record rather than publishing partial
state.

## Available queries

The ledger API can return:

- the latest successful run;
- current total file count and bytes;
- added and modified files for a run;
- deleted paths for a run;
- historical run summaries, including warnings, errors and transfer counters.

Run rows include source and destination identities plus counters for copied, moved
and archived bytes. Those transfer counters remain zero for metadata-only scans
and are ready for later backup workflows.
