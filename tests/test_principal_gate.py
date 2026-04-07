"""
test_principal_gate.py — Unit tests for p188 phase-specific principal enforcement.

Verifies:
- ANALYZE phase: missing causal_grounding -> violation returned
- ANALYZE phase: with causal_grounding declared -> no violation
- EXECUTE phase: missing minimal_change -> violation returned
- OBSERVE phase: no enforcement (no required principals) -> no violation
- get_principal_feedback: returns non-empty string for known violations
- JUDGE phase: missing invariant_preservation -> violation returned
- check_principal_gate handles None/empty principals gracefully
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest
from principal_gate import (
    check_principal_gate,
    get_principal_feedback,
    PHASE_REQUIRED_PRINCIPALS,
    PHASE_VIOLATION_REDIRECT,
)


# ── Simple stub for PhaseRecord-like objects ──────────────────────────────────

class _FakePhaseRecord:
    """Minimal stub with .principals attribute — avoids importing full PhaseRecord."""
    def __init__(self, principals: list[str]):
        self.principals = principals
        self.phase = "TEST"


# ── Tests: check_principal_gate ───────────────────────────────────────────────

def test_analyze_missing_causal_grounding():
    """ANALYZE phase without causal_grounding declared -> violation."""
    record = _FakePhaseRecord(principals=["evidence_based"])
    violation = check_principal_gate(record, "ANALYZE")
    assert violation == "missing_causal_grounding", f"expected missing_causal_grounding, got {violation}"


def test_analyze_with_causal_grounding():
    """ANALYZE phase requires both causal_grounding and evidence_linkage (v2.0 contract)."""
    # causal_grounding alone is insufficient
    record = _FakePhaseRecord(principals=["causal_grounding", "evidence_based"])
    violation = check_principal_gate(record, "ANALYZE")
    assert violation is not None, "causal_grounding alone should fail (evidence_linkage also required)"
    # both required principals satisfies the gate
    record2 = _FakePhaseRecord(principals=["causal_grounding", "evidence_linkage"])
    violation2 = check_principal_gate(record2, "ANALYZE")
    assert violation2 is None, f"causal_grounding + evidence_linkage should pass, got {violation2}"


def test_execute_missing_minimal_change():
    """EXECUTE phase without minimal_change declared -> violation."""
    record = _FakePhaseRecord(principals=["evidence_based"])
    violation = check_principal_gate(record, "EXECUTE")
    assert violation == "missing_minimal_change", f"expected missing_minimal_change, got {violation}"


def test_execute_with_minimal_change():
    """EXECUTE phase with minimal_change declared -> no violation."""
    record = _FakePhaseRecord(principals=["minimal_change"])
    violation = check_principal_gate(record, "EXECUTE")
    assert violation is None, f"expected None, got {violation}"


def test_observe_no_enforcement():
    """OBSERVE phase has no required principals -> always no violation."""
    record = _FakePhaseRecord(principals=[])
    violation = check_principal_gate(record, "OBSERVE")
    assert violation is None, f"OBSERVE should never produce violation, got {violation}"


def test_observe_no_enforcement_with_principals():
    """OBSERVE phase ignores any principals declared — still no violation."""
    record = _FakePhaseRecord(principals=["causal_grounding"])
    violation = check_principal_gate(record, "OBSERVE")
    assert violation is None


def test_judge_missing_invariant_preservation():
    """JUDGE phase without invariant_preservation -> violation."""
    record = _FakePhaseRecord(principals=["causal_grounding"])
    violation = check_principal_gate(record, "JUDGE")
    assert violation == "missing_invariant_preservation"


def test_judge_with_invariant_preservation():
    """JUDGE phase with invariant_preservation declared -> no violation."""
    record = _FakePhaseRecord(principals=["invariant_preservation"])
    violation = check_principal_gate(record, "JUDGE")
    assert violation is None


def test_unknown_phase_no_enforcement():
    """Unknown phase has no required principals -> no violation."""
    record = _FakePhaseRecord(principals=[])
    violation = check_principal_gate(record, "UNKNOWN_PHASE")
    assert violation is None


def test_principals_case_insensitive():
    """Principal matching is case-insensitive."""
    record = _FakePhaseRecord(principals=["Causal_Grounding", "Evidence_Linkage"])
    violation = check_principal_gate(record, "ANALYZE")
    assert violation is None, f"case-insensitive match should work, got {violation}"


def test_phase_case_insensitive():
    """Phase name matching is case-insensitive."""
    record = _FakePhaseRecord(principals=["causal_grounding", "evidence_linkage"])
    violation = check_principal_gate(record, "analyze")  # lowercase
    assert violation is None


def test_empty_principals_analyze():
    """Empty principals list -> ANALYZE violation."""
    record = _FakePhaseRecord(principals=[])
    violation = check_principal_gate(record, "ANALYZE")
    assert violation == "missing_causal_grounding"


def test_none_principals_handled():
    """None principals attribute -> treated as empty list, no crash."""
    record = _FakePhaseRecord(principals=None)
    # Should not raise; should return violation since nothing is declared
    violation = check_principal_gate(record, "ANALYZE")
    assert violation == "missing_causal_grounding"


# ── Tests: get_principal_feedback ─────────────────────────────────────────────

def test_get_feedback_returns_string():
    """get_principal_feedback returns a non-empty string for any violation."""
    feedback = get_principal_feedback("missing_causal_grounding")
    assert isinstance(feedback, str)
    assert len(feedback) > 0


def test_get_feedback_causal_grounding():
    """Feedback for missing_causal_grounding mentions causal or PRINCIPALS."""
    feedback = get_principal_feedback("missing_causal_grounding")
    assert "causal" in feedback.lower() or "PRINCIPALS" in feedback


def test_get_feedback_minimal_change():
    """Feedback for missing_minimal_change mentions minimal or PRINCIPALS."""
    feedback = get_principal_feedback("missing_minimal_change")
    assert "minimal" in feedback.lower() or "PRINCIPALS" in feedback


def test_get_feedback_invariant_preservation():
    """Feedback for missing_invariant_preservation mentions invariant or PRINCIPALS."""
    feedback = get_principal_feedback("missing_invariant_preservation")
    assert "invariant" in feedback.lower() or "PRINCIPALS" in feedback


def test_get_feedback_unknown_violation():
    """Unknown violation code returns a generic fallback string, not empty."""
    feedback = get_principal_feedback("some_unknown_code")
    assert isinstance(feedback, str)
    assert len(feedback) > 0


# ── Tests: PHASE_REQUIRED_PRINCIPALS table ────────────────────────────────────

def test_phase_table_has_expected_phases():
    """PHASE_REQUIRED_PRINCIPALS has all four phases."""
    assert "OBSERVE" in PHASE_REQUIRED_PRINCIPALS
    assert "ANALYZE" in PHASE_REQUIRED_PRINCIPALS
    assert "EXECUTE" in PHASE_REQUIRED_PRINCIPALS
    assert "JUDGE" in PHASE_REQUIRED_PRINCIPALS


def test_observe_has_no_required_principals():
    """OBSERVE requires no principals (empty list)."""
    assert PHASE_REQUIRED_PRINCIPALS["OBSERVE"] == []


def test_phase_redirect_table():
    """PHASE_VIOLATION_REDIRECT has redirect targets for enforced phases."""
    assert PHASE_VIOLATION_REDIRECT["ANALYZE"] == "OBSERVE"
    assert PHASE_VIOLATION_REDIRECT["EXECUTE"] == "ANALYZE"
    assert PHASE_VIOLATION_REDIRECT["JUDGE"] == "EXECUTE"
