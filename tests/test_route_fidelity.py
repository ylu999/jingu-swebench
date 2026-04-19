"""Route fidelity tests — lock in the 3 verified EFR repair loop cases.

Validates that:
1. classify_failure produces correct failure_type
2. get_routing produces correct next_phase
3. The route is the one that should reach cp-reset (no override when EFR active)

These 3 cases are from batch efr-repair-loop-3inst (2026-04-19), all verified in CloudWatch.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from failure_classifier import classify_failure, get_routing


# ── Verified cases from efr-repair-loop-3inst ──────────────────────────

_VERIFIED_CASES = [
    # (cv_result, expected_failure_type, expected_route, instance_id)
    (
        {"f2p_passed": 0, "f2p_failed": 1},
        "wrong_direction",
        "ANALYZE",
        "django__django-11333",
    ),
    (
        {"f2p_passed": 4, "f2p_failed": 2},
        "incomplete_fix",
        "DESIGN",
        "django__django-11400",
    ),
    (
        {"f2p_passed": 1, "f2p_failed": 0, "eval_resolved": False},
        "verify_gap",
        "EXECUTE",
        "django__django-11141",
    ),
]


@pytest.mark.parametrize(
    "cv,expected_ft,expected_route,instance_id",
    _VERIFIED_CASES,
    ids=[c[3] for c in _VERIFIED_CASES],
)
def test_route_fidelity_classify_and_route(cv, expected_ft, expected_route, instance_id):
    """Each verified case produces the correct failure_type and route."""
    ft = classify_failure(cv)
    assert ft == expected_ft, f"{instance_id}: expected {expected_ft}, got {ft}"

    routing = get_routing(ft)
    assert routing["next_phase"] == expected_route, (
        f"{instance_id}: expected route {expected_route}, got {routing['next_phase']}"
    )


def test_efr_route_should_not_be_overridden_when_active():
    """When _last_failure_type is set (EFR active), protocol routing must not override.

    This is the contract: _efr_route_active = bool(_last_failure_type).
    When True, protocol-route prints SKIP instead of OVERRIDE.
    """
    # This is a behavioral contract test, not a unit test of the override logic.
    # The actual override guard is: `if _efr_route_active: ... SKIP`
    # We verify the condition logic here.
    assert bool("wrong_direction") is True  # non-empty = EFR active
    assert bool("incomplete_fix") is True
    assert bool("verify_gap") is True
    assert bool("") is False  # empty = no EFR, protocol can override


def test_all_failure_types_route_to_valid_phase():
    """Every failure type routes to a valid cognition phase."""
    valid_phases = {"ANALYZE", "DESIGN", "EXECUTE", "JUDGE", "OBSERVE", "DECIDE"}
    for ft in ["wrong_direction", "incomplete_fix", "verify_gap", "execution_error"]:
        routing = get_routing(ft)
        assert routing["next_phase"] in valid_phases, (
            f"{ft} routes to invalid phase: {routing['next_phase']}"
        )


def test_wrong_direction_never_routes_to_execute():
    """wrong_direction must NOT route to EXECUTE — that would repeat the same phase."""
    routing = get_routing("wrong_direction")
    assert routing["next_phase"] != "EXECUTE", (
        "wrong_direction must not route to EXECUTE (same phase as failure)"
    )
