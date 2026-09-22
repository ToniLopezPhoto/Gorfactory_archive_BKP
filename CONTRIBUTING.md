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

The `main` branch protection rule must require these stable check names:

- `Linux / Python 3.9`
- `Linux / Python 3.10`
- `Linux / Python 3.11`
- `Linux / Python 3.12`
- `Linux / Python 3.13`
- `Linux / Python 3.14`
- `macOS production smoke`

Repository administrators can update only the required-status-check portion of
an existing protection rule (without replacing its review/force-push settings):

```sh
gh api --method PATCH \
  repos/ToniLopezPhoto/Gorfactory_archive_BKP/branches/main/protection/required_status_checks \
  --input required-checks.json
```

`required-checks.json` must contain:

```json
{
  "strict": true,
  "contexts": [
    "Linux / Python 3.9",
    "Linux / Python 3.10",
    "Linux / Python 3.11",
    "Linux / Python 3.12",
    "Linux / Python 3.13",
    "Linux / Python 3.14",
    "macOS production smoke"
  ]
}
```

Verify the applied rule with:

```sh
gh api repos/ToniLopezPhoto/Gorfactory_archive_BKP/branches/main/protection/required_status_checks
```

Until that response reports `strict: true` and every context above, the merge
gate remains administratively incomplete even if the workflow itself is green.

Use `Closes #N` or `Fixes #N` only when every acceptance criterion is complete.
For partial work, reference the issue without an auto-closing keyword and list
the remaining gaps explicitly.

Changes that could affect real archive data require synthetic fixture tests.
Never use the production SAM or archive as a development or CI test target, and
never enable production scheduling from an implementation pull request.
