"""Tests for verify_gap → DESIGN reroute.

Replay validation: when verify_gap is detected (F2P pass, P2P regression),
the system routes to DESIGN phase (not EXECUTE), with correct prompt/principals.

Structural indicators tested:
  1. next_phase == DESIGN (not EXECUTE)
  2. repair_hint changed (redesign, not narrow)
  3. design_record prompt is fundamentally different from EXECUTE
  4. principals match DESIGN phase
  5. negative case: execution_error still routes to EXECUTE
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from failure_classifier import classify_failure, get_routing, FAILURE_ROUTING_RULES
from repair_prompts import build_repair_prompt


# ── 11490-like CV result: F2P all pass, P2P regression ──────────────────────

CV_11490_VERIFY_GAP = {
    "verification_kind": "controlled_fail_to_pass",
    "f2p_passed": 1,
    "f2p_failed": 0,
    "p2p_passed": 22,
    "p2p_failed": 1,
    "p2p_failing_names": [
        "test_combining_multiple_models (queries.test_qs_combinators.QuerySetSetOperationTests)"
    ],
    "eval_resolved": False,
    "output_tail": (
        "test_combining_multiple_models ... ERROR\n"
        "django.db.utils.OperationalError: no such column: queries_number.num"
    ),
}


# ── Replay A: routing decision ──────────────────────────────────────────────

def test_verify_gap_classifies_correctly():
    """11490-like CV result is classified as verify_gap."""
    ft = classify_failure(CV_11490_VERIFY_GAP)
    assert ft == "verify_gap"


def test_verify_gap_routes_to_design():
    """verify_gap routes to DESIGN, not EXECUTE."""
    routing = get_routing("verify_gap")
    assert routing["next_phase"] == "DESIGN"


def test_verify_gap_principals_are_design_phase():
    """verify_gap principals come from DESIGN phase, not EXECUTE."""
    routing = get_routing("verify_gap")
    # DESIGN principals should NOT include execution-specific ones
    assert "action_grounding" not in routing["required_principals"]
    assert "minimal_change" not in routing["required_principals"]


# ── Replay B: prompt content ────────────────────────────────────────────────

def test_verify_gap_prompt_contains_redesign_language():
    """Repair prompt for verify_gap must tell agent to REDESIGN, not narrow."""
    routing = get_routing("verify_gap")
    prompt = build_repair_prompt("verify_gap", CV_11490_VERIFY_GAP, routing)

    # Must contain redesign language
    assert "REDESIGN" in prompt or "redesign" in prompt
    # Must NOT contain "narrow" or "just make it more precise"
    assert "just make it more precise" not in prompt
    # Must declare DESIGN phase
    assert "[REPAIR PHASE: DESIGN]" in prompt


def test_verify_gap_prompt_contains_regression_evidence():
    """Prompt must include P2P regression count and test output."""
    routing = get_routing("verify_gap")
    prompt = build_repair_prompt("verify_gap", CV_11490_VERIFY_GAP, routing)

    assert "P2P regressions: 1" in prompt
    assert "DIAGNOSIS:" in prompt
    assert "test_combining_multiple_models" in prompt or "ERROR" in prompt


def test_verify_gap_prompt_warns_against_incremental_fix():
    """Prompt must explicitly warn that incremental patching doesn't work."""
    routing = get_routing("verify_gap")
    prompt = build_repair_prompt("verify_gap", CV_11490_VERIFY_GAP, routing)

    # Must warn against the pattern that failed in 11490 A2
    assert "same approach" in prompt.lower() or "already tried" in prompt.lower()


# ── Replay: _next_attempt_start_phase simulation ────────────────────────────

def test_routing_consumed_gives_design_start_phase():
    """Simulates jingu_agent.py line 3073: _next_attempt_start_phase."""
    ft = classify_failure(CV_11490_VERIFY_GAP)
    routing = get_routing(ft)
    _next_attempt_start_phase = routing["next_phase"].upper()
    assert _next_attempt_start_phase == "DESIGN"


# ── Negative case: execution_error still routes to EXECUTE ──────────────────

CV_EXECUTION_ERROR = {
    "verification_kind": "controlled_error",
    "f2p_passed": 0,
    "f2p_failed": 0,
    "eval_resolved": False,
    "output_tail": "SyntaxError: unexpected indent at line 42",
}


def test_execution_error_still_routes_to_execute():
    """execution_error must NOT be rerouted — stays at EXECUTE."""
    routing = get_routing("execution_error")
    assert routing["next_phase"] == "EXECUTE"


def test_execution_error_prompt_does_not_mention_redesign():
    """execution_error prompt should not tell agent to redesign."""
    routing = get_routing("execution_error")
    prompt = build_repair_prompt("execution_error", CV_EXECUTION_ERROR, routing)
    assert "REDESIGN" not in prompt
    assert "[REPAIR PHASE: EXECUTE]" in prompt


# ── Negative case: wrong_direction routes to ANALYZE (not DESIGN) ───────────

def test_wrong_direction_routes_to_analyze():
    """wrong_direction goes to ANALYZE, not DESIGN."""
    routing = get_routing("wrong_direction")
    assert routing["next_phase"] == "ANALYZE"


# ── Negative case: incomplete_fix still routes to DESIGN ────────────────────

def test_incomplete_fix_routes_to_design():
    """incomplete_fix also routes to DESIGN (pre-existing, confirm stable)."""
    routing = get_routing("incomplete_fix")
    assert routing["next_phase"] == "DESIGN"


# ── Structural indicator: all 4 failure types route to distinct phases ──────

def test_routing_phase_distribution():
    """Verify the routing map: ANALYZE, DESIGN, DESIGN, EXECUTE."""
    phases = {ft: r["next_phase"] for ft, r in FAILURE_ROUTING_RULES.items()}
    assert phases == {
        "wrong_direction": "ANALYZE",
        "incomplete_fix": "DESIGN",
        "verify_gap": "DESIGN",
        "execution_error": "EXECUTE",
    }
