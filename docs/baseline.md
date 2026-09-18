# Baseline adoption

The baseline workflow turns the existing manual first dump into trusted state
without blindly copying the full catalogue again.

## 1. Verify without changes

Connect the expected SAM and archive volumes, confirm their configured identity
markers, then run:

```sh
gorbackup --config config/config.yaml baseline
```

The command runs all preflight checks and then uses `rclone check` against the
configured `current/` directory. It excludes the source identity marker and does
not modify the source or destination.

The path-level JSON report distinguishes:

- files missing from the destination;
- files found only on the destination;
- files present on both sides but different;
- read or hash errors.

Destination-only files are retained and reported for manual review. They do not
prevent adoption when every source file is present and matches.

## 2. Reconcile when required

If files are missing or mismatched, inspect the report first. To copy only the
required source content and verify again:

```sh
gorbackup --config config/config.yaml baseline --reconcile
```

Reconciliation uses `rclone copy --check-first --metadata`. `copy` updates missing
or changed files but does not delete destination extras. The workflow never calls
`rclone sync`, `delete`, or `purge`.

## 3. Successful state

Only a successful final comparison writes the known-good manifest. It records:

- source file count and total bytes;
- source and archive marker identities;
- archive free bytes and percentage after reconciliation;
- path-level verification results, including retained extras;
- whether reconciliation was required;
- timestamps and a separate baseline run record.

State files are written atomically below `state.directory` and are ignored by Git.
Future backup preflights load the manifest summary and abort if the source becomes
implausibly smaller than this baseline.
