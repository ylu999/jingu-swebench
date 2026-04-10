"""Tests for repair_prompts.py — p209 deliverable.

Verifies:
- All 4 failure types produce non-empty output
- Output contains the phase name
- Output contains evidence from cv_result
- NBR compliance: output is never empty string
"""
import pytest
from repair_prompts import build_repair_prompt
from failure_classifier import FAILURE_ROUTING_RULES


# Realistic cv_result fixtures
CV_WRONG_DIRECTION = {
    "verification_kind": "controlled_fail_to_pass",
    "f2p_passed": 0,
    "f2p_failed": 3,
    "eval_resolved": False,
    "output_tail": "FAILED tests/test_foo.py::test_bar - AssertionError: expected 1 got 2",
}

CV_INCOMPLETE_FIX = {
    "verification_kind": "controlled_fail_to_pass",
    "f2p_passed": 2,
    "f2p_failed": 1,
    "eval_resolved": False,
    "output_tail": "FAILED tests/test_baz.py::test_edge_case - ValueError",
}

CV_VERIFY_GAP = {
    "verification_kind": "controlled_fail_to_pass",
    "f2p_passed": 3,
    "f2p_failed": 0,
    "p2p_passed": 10,
    "p2p_failed": 2,
    "eval_resolved": False,
    "output_tail": "FAILED tests/test_existing.py::test_regression - broken by patch",
}

CV_EXECUTION_ERROR = {
    "verification_kind": "controlled_error",
    "f2p_passed": 0,
    "f2p_failed": 0,
    "eval_resolved": False,
    "output_tail": "SyntaxError: unexpected indent at line 42",
}

# Map failure types to fixtures
_FIXTURES = {
    "wrong_direction": CV_WRONG_DIRECTION,
    "incomplete_fix": CV_INCOMPLETE_FIX,
    "verify_gap": CV_VERIFY_GAP,
    "execution_error": CV_EXECUTION_ERROR,
}


@pytest.mark.parametrize("failure_type", list(FAILURE_ROUTING_RULES.keys()))
def test_all_types_produce_nonempty(failure_type: str):
    """NBR compliance: every failure type must produce non-empty prompt."""
    cv = _FIXTURES[failure_type]
    routing = FAILURE_ROUTING_RULES[failure_type]
    result = build_repair_prompt(failure_type, cv, routing)
    assert result.strip(), f"Empty prompt for {failure_type}"


@pytest.mark.parametrize("failure_type", list(FAILURE_ROUTING_RULES.keys()))
def test_output_contains_phase_name(failure_type: str):
    """Prompt must include the repair phase declaration."""
    cv = _FIXTURES[failure_type]
    routing = FAILURE_ROUTING_RULES[failure_type]
    result = build_repair_prompt(failure_type, cv, routing)
    phase = routing["next_phase"].upper()
    assert phase in result, f"Phase '{phase}' not found in prompt for {failure_type}"


@pytest.mark.parametrize("failure_type", list(FAILURE_ROUTING_RULES.keys()))
def test_output_contains_evidence(failure_type: str):
    """Prompt must include evidence from cv_result (f2p counts)."""
    cv = _FIXTURES[failure_type]
    routing = FAILURE_ROUTING_RULES[failure_type]
    result = build_repair_prompt(failure_type, cv, routing)
    # Must contain f2p counts
    assert "F2P results:" in result, f"Missing F2P evidence for {failure_type}"
    # Must contain test output from cv_result
    if cv.get("output_tail"):
        assert "Test output:" in result, f"Missing test output for {failure_type}"


@pytest.mark.parametrize("failure_type", list(FAILURE_ROUTING_RULES.keys()))
def test_output_contains_principals(failure_type: str):
    """Prompt must include required principals."""
    cv = _FIXTURES[failure_type]
    routing = FAILURE_ROUTING_RULES[failure_type]
    result = build_repair_prompt(failure_type, cv, routing)
    for p in routing["required_principals"]:
        assert p in result, f"Principal '{p}' not found in prompt for {failure_type}"


@pytest.mark.parametrize("failure_type", list(FAILURE_ROUTING_RULES.keys()))
def test_output_contains_repair_goal(failure_type: str):
    """Prompt must include the repair goal."""
    cv = _FIXTURES[failure_type]
    routing = FAILURE_ROUTING_RULES[failure_type]
    result = build_repair_prompt(failure_type, cv, routing)
    assert routing["repair_goal"] in result, f"Repair goal not found for {failure_type}"


def test_empty_cv_result_still_nonempty():
    """Even with empty cv_result, prompt must be non-empty (NBR)."""
    routing = FAILURE_ROUTING_RULES["wrong_direction"]
    result = build_repair_prompt("wrong_direction", {}, routing)
    assert result.strip(), "Empty prompt for empty cv_result"


def test_minimal_cv_result():
    """Minimal cv_result with just f2p counts."""
    cv = {"f2p_passed": 0, "f2p_failed": 5}
    routing = FAILURE_ROUTING_RULES["wrong_direction"]
    result = build_repair_prompt("wrong_direction", cv, routing)
    assert "0 passed" in result
    assert "5 failed" in result
