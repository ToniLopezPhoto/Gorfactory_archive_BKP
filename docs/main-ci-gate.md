# Main CI merge gate (#26)

CI running on a pull request is not itself a merge gate. An administrator must
apply branch protection to `main`, read it back, and demonstrate the behavior
with a temporary pull request before closing #26.

The repository contains a standard-library-only helper. It makes no changes
until `apply` is explicitly requested. Keep the token out of the repository.
Use a token with repository **Administration: write** permission to apply the
rule, and **Administration: read** to verify it.

```bash
python scripts/main_ci_gate.py show
export GITHUB_TOKEN='your-admin-token'
python scripts/main_ci_gate.py apply
python scripts/main_ci_gate.py verify
```

The helper applies branch protection that requires a pull request, zero human
approvals, strict up-to-date status checks and administrator enforcement. It
requires exactly these seven CI jobs:

```text
Linux / Python 3.9
Linux / Python 3.10
Linux / Python 3.11
Linux / Python 3.12
Linux / Python 3.13
Linux / Python 3.14
macOS production smoke
```

`apply` performs a PUT, then reads `main` and its protection back. It fails if
GitHub reports an unprotected branch, a missing or extra required check,
non-strict checks, disabled administrator enforcement, or a missing PR rule.
The readback proves configuration only; it does not prove merge blocking.

## Temporary gate proof

1. Create a temporary branch from the current `main` and add only a harmless
   test note. Push it and open a PR against `main`. Do not enable auto-merge.
2. While the seven required jobs are pending, record the PR URL, head SHA,
   check states and GitHub's blocked merge message. Do not merge.
3. On that temporary branch only, deliberately make one existing CI test fail.
   Push and record the failed job and blocked merge state. Do not merge.
4. Remove the deliberate failure and push. Wait for all seven required jobs to
   pass. Record the same PR's enabled merge state. Closing the proof PR is fine;
   merging it is not necessary.
5. Add the PR URL and the three observations to #26. Mark its two acceptance
   criteria complete and close it only after the configuration readback and
   behavior proof both succeed. Then update #1. Do not start #4 in this task.

The temporary failing commit must never be merged into `main`. The helper does
not change workflow jobs, enable auto-merge, modify backup code, or access
production storage.
