"""Apply and verify the seven-check branch protection for main (issue #26).

Requires a GitHub token with repository Administration: write permission for
``apply`` and Administration: read permission for ``verify``. No gh CLI needed.
"""

import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REPOSITORY = "ToniLopezPhoto/Gorfactory_archive_BKP"
BRANCH = "main"
REQUIRED_CHECKS = (
    "Linux / Python 3.9",
    "Linux / Python 3.10",
    "Linux / Python 3.11",
    "Linux / Python 3.12",
    "Linux / Python 3.13",
    "Linux / Python 3.14",
    "macOS production smoke",
)
BASE = f"https://api.github.com/repos/{REPOSITORY}/branches/{BRANCH}"
PROTECTION = BASE + "/protection"


def protection_payload():
    """Keep this payload reviewable before applying it to GitHub."""
    return {
        "required_status_checks": {
            "strict": True,
            "contexts": list(REQUIRED_CHECKS),
        },
        "enforce_admins": True,
        # A pull request is required, but this personal repo needs no approver.
        "required_pull_request_reviews": {
            "required_approving_review_count": 0,
        },
        "restrictions": None,
    }


def request_json(url, token, method="GET", payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(url, data=data, method=method, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        **({"Content-Type": "application/json"} if data is not None else {}),
    })
    with urlopen(request, timeout=20) as response:
        return json.load(response)


def check_protection(branch, protection):
    """Return precise mismatches; a successful PUT alone proves nothing."""
    errors = []
    if branch.get("name") != BRANCH or branch.get("protected") is not True:
        errors.append("main is not reported as protected")
    checks = protection.get("required_status_checks") or {}
    if checks.get("strict") is not True:
        errors.append("required checks are not strict/up-to-date")
    actual = set(checks.get("contexts") or ())
    actual.update(item.get("context") for item in checks.get("checks") or ())
    actual.discard(None)
    if actual != set(REQUIRED_CHECKS):
        errors.append("required checks differ: " + json.dumps({
            "missing": sorted(set(REQUIRED_CHECKS) - actual),
            "extra": sorted(actual - set(REQUIRED_CHECKS)),
        }))
    if (protection.get("enforce_admins") or {}).get("enabled") is not True:
        errors.append("administrator enforcement is disabled")
    reviews = protection.get("required_pull_request_reviews")
    if not isinstance(reviews, dict):
        errors.append("pull requests are not required")
    elif reviews.get("required_approving_review_count") != 0:
        errors.append("human review count is not zero")
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("show", "apply", "verify"))
    args = parser.parse_args(argv)
    if args.action == "show":
        print(json.dumps(protection_payload(), indent=2))
        return 0
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        parser.error("GITHUB_TOKEN is required (Administration: write for apply)")
    try:
        branch = request_json(BASE, token)
        if args.action == "apply":
            # PUT replaces the complete branch protection rule. Do not erase
            # settings an administrator may have added since this script was
            # reviewed.
            if branch.get("protected"):
                existing = request_json(PROTECTION, token)
                if check_protection(branch, existing):
                    print("main is already protected; refusing to replace its rule",
                          file=sys.stderr)
                    return 1
            else:
                request_json(PROTECTION, token, "PUT", protection_payload())
                branch = request_json(BASE, token)
        protection = request_json(PROTECTION, token)
    except HTTPError as exc:
        # Never print request headers or the token.
        print(f"GitHub API HTTP {exc.code} at {exc.url}: {exc.reason}", file=sys.stderr)
        return 2
    except (URLError, OSError, ValueError) as exc:
        print(f"GitHub API request failed: {exc}", file=sys.stderr)
        return 2
    errors = check_protection(branch, protection)
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print("main protection verified: strict PR gate, administrators included")
    for check in REQUIRED_CHECKS:
        print(f"required: {check}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
