# gorbackup

[![CI](https://github.com/ToniLopezPhoto/Gorfactory_archive_BKP/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/ToniLopezPhoto/Gorfactory_archive_BKP/actions/workflows/ci.yml)

`gorbackup` is the command-line entry point for the Gorfactory archive backup
system. It includes configuration, dependency and preflight checks, baseline
adoption, dry-run planning, and versioned incremental synchronization.

## Requirements

- Python 3.9 or newer
- `rclone` 1.65.0 or newer on `PATH`

## Setup from a fresh clone

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
cp config/example.yaml config/config.yaml
gorbackup --help
gorbackup --config config/config.yaml status
```

Edit `config/config.yaml` for the local machine. It is ignored by Git and must
not be committed. The example paths are illustrative only.

Every operational command validates the configuration and the installed
`rclone` version. `backup` additionally runs read-only safety checks and a fresh
plan, enforces deletion and projected-capacity limits, then repeats deletion
limits inside rclone before syncing; it never writes to the source. The
configured identity markers and archive directories must be provisioned by an operator.
See [docs/configuration.md](docs/configuration.md) for the available settings.

To verify the existing dump without changing either side:

```sh
gorbackup --config config/config.yaml baseline
```

If the report identifies missing or mismatched destination files, reconciliation
must be requested explicitly. It uses `rclone copy`, never deletes destination
extras, and verifies again before writing the baseline manifest:

```sh
gorbackup --config config/config.yaml baseline --reconcile
```

See [docs/baseline.md](docs/baseline.md) for the adoption workflow.

To inventory the catalogue using path, size and modification-time metadata only:

```sh
gorbackup --config config/config.yaml scan
```

The scan updates the SQLite ledger only after the complete catalogue has been
read successfully. See [docs/ledger.md](docs/ledger.md).

To generate and persist a machine-readable dry-run before any backup execution:

```sh
gorbackup --config config/config.yaml plan
```

The preview reports transfer and `current/` departure counts/bytes, rename
candidates, recent files and errors. See [docs/planning.md](docs/planning.md).

To execute the same non-destructive sync shape, preserving every displaced file
under a unique history directory:

```sh
gorbackup --config config/config.yaml backup
```

See [docs/backup.md](docs/backup.md) for execution and recovery semantics.

## Development

```sh
python -m pytest
```

Pull requests targeting `main` run compilation and the full suite on every
supported CPython version through GitHub Actions.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the required branch, PR, test, and
issue-closing workflow. `main` is the sole integration branch.

Project directories are organized as follows:

- `src/gorbackup/`: application package and CLI
- `config/`: tracked example and ignored local configuration
- `scripts/`: maintenance and installation helpers
- `launchd/`: macOS scheduling definitions
- `tests/`: automated tests
- `docs/`: user and operator documentation
