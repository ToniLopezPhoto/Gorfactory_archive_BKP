"""Guard the administrative payload for issue #26 against accidental drift."""

from scripts.main_ci_gate import REQUIRED_CHECKS, check_protection, protection_payload


def test_payload_requires_exact_ci_matrix_and_pr_without_reviewers():
    payload = protection_payload()
    assert payload["required_status_checks"] == {
        "strict": True, "contexts": list(REQUIRED_CHECKS),
    }
    assert len(REQUIRED_CHECKS) == 7
    assert payload["enforce_admins"] is True
    assert payload["required_pull_request_reviews"]["required_approving_review_count"] == 0
    assert payload["restrictions"] is None


def test_readback_rejects_partial_or_bypassable_configuration():
    branch = {"name": "main", "protected": True}
    protection = {
        "required_status_checks": {"strict": True, "contexts": list(REQUIRED_CHECKS)},
        "enforce_admins": {"enabled": True},
        "required_pull_request_reviews": {"required_approving_review_count": 0},
    }
    assert check_protection(branch, protection) == []
    protection["required_status_checks"]["contexts"].pop()
    protection["enforce_admins"]["enabled"] = False
    errors = check_protection(branch, protection)
    assert any("required checks differ" in error for error in errors)
    assert any("administrator enforcement" in error for error in errors)
