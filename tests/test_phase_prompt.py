"""
test_phase_prompt.py — Unit tests for scripts/phase_prompt.py (p189)

Verifies:
- OBSERVE phase returns correct prefix containing "[Phase: OBSERVE]"
- EXECUTE phase returns correct prefix containing "[Phase: EXECUTE]"
- Unknown phase returns empty string (safe fallback)
- All 6 known phases (UNDERSTAND/OBSERVE/ANALYZE/DECIDE/EXECUTE/JUDGE) have non-empty guidance
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest
from phase_prompt import build_phase_prefix, PHASE_GUIDANCE


# ── Tests: build_phase_prefix ─────────────────────────────────────────────────

def test_phase_prefix_observe():
    """OBSERVE phase returns prefix with [Phase: OBSERVE] header."""
    result = build_phase_prefix("OBSERVE")
    assert result.startswith("[Phase: OBSERVE]"), f"Expected '[Phase: OBSERVE]' prefix, got: {result!r}"
    assert "evidence" in result.lower(), "OBSERVE guidance should mention evidence gathering"
    assert result.endswith("\n\n"), "Phase prefix should end with double newline"


def test_phase_prefix_execute():
    """EXECUTE phase returns prefix with [Phase: EXECUTE] header."""
    result = build_phase_prefix("EXECUTE")
    assert result.startswith("[Phase: EXECUTE]"), f"Expected '[Phase: EXECUTE]' prefix, got: {result!r}"
    assert "fix" in result.lower() or "patch" in result.lower(), "EXECUTE guidance should mention fix/patch"
    assert result.endswith("\n\n"), "Phase prefix should end with double newline"


def test_phase_prefix_unknown():
    """Unknown phase returns empty string — safe fallback, no injection."""
    result = build_phase_prefix("UNKNOWN_PHASE")
    assert result == "", f"Unknown phase should return empty string, got: {result!r}"

    result_empty = build_phase_prefix("")
    assert result_empty == "", f"Empty phase string should return empty string, got: {result_empty!r}"


def test_all_known_phases_have_guidance():
    """All 6 phases in Phase Literal have non-empty guidance."""
    known_phases = ["UNDERSTAND", "OBSERVE", "ANALYZE", "DECIDE", "EXECUTE", "JUDGE"]
    for phase in known_phases:
        result = build_phase_prefix(phase)
        assert result != "", f"Phase {phase!r} should have non-empty guidance"
        assert f"[Phase: {phase}]" in result, f"Phase {phase!r} prefix must contain '[Phase: {phase}]'"


def test_phase_guidance_dict_completeness():
    """PHASE_GUIDANCE dict contains all canonical phases."""
    from canonical_symbols import ALL_PHASES
    expected_phases = set(ALL_PHASES)
    assert set(PHASE_GUIDANCE.keys()) == expected_phases, (
        f"PHASE_GUIDANCE keys mismatch. Expected: {expected_phases}, got: {set(PHASE_GUIDANCE.keys())}"
    )


def test_phase_prefix_no_double_bracket():
    """Phase prefix format is '[Phase: X] guidance' — no extra brackets in guidance."""
    for phase in PHASE_GUIDANCE:
        result = build_phase_prefix(phase)
        # Should start with [Phase: X] exactly once
        assert result.count(f"[Phase: {phase}]") == 1, (
            f"Phase {phase!r} prefix should contain '[Phase: {phase}]' exactly once"
        )
