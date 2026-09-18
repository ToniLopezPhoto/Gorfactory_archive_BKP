# Contributing

`main` is the only integration branch. Start every feature or bugfix branch
from the latest `main` and open a pull request back to `main`.

```sh
git switch main
git pull --ff-only
git switch -c codex/issue-<number>-<short-name>
```

Before opening a pull request, run:

```sh
python -m pytest
python -m compileall -q src tests
```

Pull requests to `main` must pass the complete GitHub Actions Python matrix
before merge. Do not merge directly or retarget implementation work to another
integration branch to bypass CI.

Use `Closes #N` or `Fixes #N` only when every acceptance criterion is complete.
For partial work, reference the issue without an auto-closing keyword and list
the remaining gaps explicitly.

Changes that could affect real archive data require synthetic fixture tests.
Never use the production SAM or archive as a development or CI test target, and
never enable production scheduling from an implementation pull request.
