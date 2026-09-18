# gorbackup

`gorbackup` is the command-line entry point for the Gorfactory archive backup
system. It includes configuration, dependency and preflight checks plus a safe
workflow for adopting an existing first dump. Scheduled backup operations remain
placeholders.

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
`rclone` version. `backup` additionally runs read-only safety checks before
reporting its placeholder status; it never writes to the source. The configured
identity markers and archive directories must be provisioned by an operator.
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

## Development

```sh
python -m pytest
```

Project directories are organized as follows:

- `src/gorbackup/`: application package and CLI
- `config/`: tracked example and ignored local configuration
- `scripts/`: maintenance and installation helpers
- `launchd/`: macOS scheduling definitions
- `tests/`: automated tests
- `docs/`: user and operator documentation
