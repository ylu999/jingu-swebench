"""
test_cognition_gate.py — Tests for p187 cognition gate (JUDGE phase entry).

Tests cover:
  - check_cognition_at_judge: pass path (valid declaration)
  - check_cognition_at_judge: fail path (signal contradiction)
  - check_cognition_at_judge: empty declaration always passes (opt-in gate)
  - Integration: gate injects pending_redirect_hint on fail
  - Integration: gate does not set pending_redirect_hint on pass
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

import pytest
from unittest.mock import MagicMock, patch

from cognition_check import check_cognition_at_judge, check_cognition, format_cognition_feedback


# ── check_cognition_at_judge: direct unit tests ────────────────────────────────

class TestCheckCognitionAtJudge:
    def test_pass_valid_declaration(self):
        """Valid declaration + compatible signals → pass, empty feedback."""
        decl = {"type": "execution", "principals": ["minimal_change"]}
        signals = ["is_single_line_fix"]
        ok, feedback = check_cognition_at_judge(decl, signals)
        assert ok is True
        assert feedback == ""

    def test_fail_signal_contradiction(self):
        """diagnosis + is_normalization → fail, feedback non-empty."""
        decl = {"type": "diagnosis", "principals": ["causality"]}
        signals = ["is_normalization"]
        ok, feedback = check_cognition_at_judge(decl, signals)
        assert ok is False
        assert "[cognition]" in feedback
        assert "diagnosis" in feedback or "normalization" in feedback

    def test_fail_principal_contradiction(self):
        """execution + causality principal → fail (causality is diagnosis concern)."""
        decl = {"type": "execution", "principals": ["causality"]}
        signals = []
        ok, feedback = check_cognition_at_judge(decl, signals)
        assert ok is False
        assert feedback != ""

    def test_empty_declaration_always_passes(self):
        """Empty declaration → opt-in gate → always pass."""
        ok, feedback = check_cognition_at_judge({}, [])
        assert ok is True
        assert feedback == ""

    def test_empty_declaration_with_signals_passes(self):
        """No FIX_TYPE → pass even if signals would contradict."""
        ok, feedback = check_cognition_at_judge({}, ["is_normalization", "is_comment_only"])
        assert ok is True

    def test_execution_comment_only_fails(self):
        """execution + is_comment_only → fail (no code mutation)."""
        decl = {"type": "execution", "principals": ["minimal_change"]}
        signals = ["is_comment_only"]
        ok, feedback = check_cognition_at_judge(decl, signals)
        assert ok is False
        assert feedback != ""


# ── Integration: gate logic matches pending_redirect_hint injection ─────────────

class TestCognitionGateIntegration:
    """
    Test the gate logic that would run in _verifying_run:
    on fail → pending_redirect_hint is set with [COGNITION_FAIL] prefix
    on pass → pending_redirect_hint remains empty
    """

    def _simulate_gate(self, declaration, patch_signals):
        """
        Simulate the gate block from _verifying_run:
          if phase==JUDGE: run check_cognition_at_judge → set redirect hint on fail
        Returns (skip_verify: bool, hint: str)
        """
        # Simulate a minimal monitor state
        class FakeMonitorState:
            pending_redirect_hint: str = ""

        monitor = FakeMonitorState()
        skip_verify = False

        # Simulate cp_state_holder[0].phase == "JUDGE"
        from control.reasoning_state import ReasoningState
        cp_state = ReasoningState(phase="JUDGE")
        cp_state_holder = [cp_state]

        if cp_state_holder is not None and cp_state_holder[0].phase == "JUDGE":
            cg_pass, cg_feedback = check_cognition_at_judge(declaration, patch_signals)
            if not cg_pass:
                monitor.pending_redirect_hint = f"[COGNITION_FAIL] {cg_feedback}"
                skip_verify = True

        return skip_verify, monitor.pending_redirect_hint

    def test_judge_phase_pass_does_not_skip_verify(self):
        """
        JUDGE phase + cognition pass → controlled_verify is NOT skipped.
        pending_redirect_hint remains empty.
        """
        decl = {"type": "execution", "principals": ["minimal_change"]}
        signals = ["is_single_line_fix"]
        skip_verify, hint = self._simulate_gate(decl, signals)
        assert skip_verify is False
        assert hint == ""

    def test_judge_phase_fail_skips_verify_and_injects_hint(self):
        """
        JUDGE phase + cognition fail → controlled_verify IS skipped.
        pending_redirect_hint is set with [COGNITION_FAIL] prefix.
        """
        decl = {"type": "diagnosis", "principals": ["causality"]}
        signals = ["is_normalization"]
        skip_verify, hint = self._simulate_gate(decl, signals)
        assert skip_verify is True
        assert hint.startswith("[COGNITION_FAIL]")
        assert "[cognition]" in hint

    def test_judge_phase_empty_declaration_does_not_skip_verify(self):
        """
        JUDGE phase + empty declaration (no FIX_TYPE) → gate passes, verify not skipped.
        """
        skip_verify, hint = self._simulate_gate({}, [])
        assert skip_verify is False
        assert hint == ""

    def test_non_judge_phase_gate_does_not_fire(self):
        """
        Non-JUDGE phase → gate does not fire at all (no check, no hint).
        """
        from control.reasoning_state import ReasoningState
        cp_state = ReasoningState(phase="EXECUTE")
        cp_state_holder = [cp_state]

        class FakeMonitorState:
            pending_redirect_hint: str = ""

        monitor = FakeMonitorState()
        skip_verify = False

        # Reproduce gate condition: only fires on JUDGE
        if cp_state_holder is not None and cp_state_holder[0].phase == "JUDGE":
            cg_pass, cg_feedback = check_cognition_at_judge(
                {"type": "diagnosis", "principals": []}, ["is_normalization"]
            )
            if not cg_pass:
                monitor.pending_redirect_hint = f"[COGNITION_FAIL] {cg_feedback}"
                skip_verify = True

        # EXECUTE phase → gate did not fire
        assert skip_verify is False
        assert monitor.pending_redirect_hint == ""
