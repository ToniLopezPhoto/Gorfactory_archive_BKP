"""Allow ``python -m gorbackup`` to behave like the console script."""

from gorbackup.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
