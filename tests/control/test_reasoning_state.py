"""
test_reasoning_state.py — Unit tests for control/reasoning_state.py

Tests mirror the TypeScript invariants (I1-I5) from signal_integrator.test.ts,
plus the 5 architectural corrections from the design review:
  - [CORR1] step vs verify signal separation (task_success only from verify)
  - [CORR2] actionability = pre-execution (patch_non_empty), not post-verify
  - [CORR3] REDIRECT is unconditional override
  - [CORR4] no step-level early stop (only attempt boundary)
  - [C5]    false progress: files_written without test count change ≠ progress
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

import pytest
from control.reasoning_state import (
    CognitionSignals, ReasoningState, DEFAULT_SIGNALS,
    initial_reasoning_state, normalize_signals, update_reasoning_state, decide_next,
    VerdictAdvance, VerdictRedirect, VerdictStop, VerdictContinue,
    NO_PROGRESS_THRESHOLD,
)
from control.swe_signal_adapter import extract_step_signals, extract_verify_signals


# ── Helpers ───────────────────────────────────────────────────────────────────

def no_progress_signals() -> CognitionSignals:
    """All-zero signals — no progress at all."""
    return CognitionSignals()


def state_at(phase: str, no_progress: int = 0, step: int = 0) -> ReasoningState:
    return ReasoningState(phase=phase, no_progress_steps=no_progress, step_index=step)


# ── normalize_signals ─────────────────────────────────────────────────────────

class TestNormalizeSignals:
    def test_empty_partial_returns_defaults(self):
        result = normalize_signals({})
        assert result == DEFAULT_SIGNALS

    def test_partial_fields_preserved(self):
        result = normalize_signals({"evidence_gain": 1, "task_success": True})
        assert result.evidence_gain == 1
        assert result.task_success is True
        # absent fields get defaults
        assert result.hypothesis_narrowing == 0
        assert result.uncertainty == 1.0
        assert result.env_noise is False

    def test_absent_fields_get_conservative_defaults(self):
        result = normalize_signals({"actionability": 1})
        assert result.uncertainty == 1.0    # assume fully uncertain
        assert result.task_success is False # assume not solved

    def test_idempotent(self):
        """A6: normalize_signals(normalize_signals(p).asdict()) == normalize_signals(p)"""
        partial = {"evidence_gain": 1, "env_noise": True}
        r1 = normalize_signals(partial)
        import dataclasses
        r2 = normalize_signals(dataclasses.asdict(r1))
        assert r1 == r2


# ── initial_reasoning_state ───────────────────────────────────────────────────

class TestInitialReasoningState:
    def test_phase_preserved(self):
        for phase in ["UNDERSTAND", "OBSERVE", "ANALYZE", "DECIDE", "EXECUTE", "JUDGE"]:
            s = initial_reasoning_state(phase)
            assert s.phase == phase

    def test_step_index_zero(self):
        s = initial_reasoning_state("OBSERVE")
        assert s.step_index == 0

    def test_no_progress_zero(self):
        s = initial_reasoning_state("OBSERVE")
        assert s.no_progress_steps == 0

    def test_conservative_defaults(self):
        s = initial_reasoning_state("OBSERVE")
        assert s.uncertainty == 1.0
        assert s.task_success is False
        assert s.evidence_gain == 0


# ── update_reasoning_state — invariants ───────────────────────────────────────

class TestUpdateReasoningStateInvariants:

    # I4: step_index monotone increment
    def test_I4_step_index_increments(self):
        s = initial_reasoning_state("OBSERVE")
        for i in range(1, 6):
            s = update_reasoning_state(s, no_progress_signals())
            assert s.step_index == i

    # I5: phase preserved across all phases + task_success
    @pytest.mark.parametrize("phase", ["UNDERSTAND", "OBSERVE", "ANALYZE", "DECIDE", "EXECUTE", "JUDGE"])
    def test_I5_phase_preserved(self, phase):
        s = initial_reasoning_state(phase)
        signals = normalize_signals({"evidence_gain": 1})
        s2 = update_reasoning_state(s, signals)
        assert s2.phase == phase

    def test_I5_phase_preserved_with_task_success(self):
        """task_success signal must NOT cause phase change."""
        s = initial_reasoning_state("EXECUTE")
        signals = normalize_signals({"task_success": True})
        s2 = update_reasoning_state(s, signals)
        assert s2.phase == "EXECUTE"

    # I1: evidence_gain resets no_progress_steps
    def test_I1_evidence_gain_resets_stagnation(self):
        s = state_at("OBSERVE", no_progress=3)
        signals = normalize_signals({"evidence_gain": 1})
        s2 = update_reasoning_state(s, signals)
        assert s2.no_progress_steps == 0

    # I1: hypothesis_narrowing also resets
    def test_I1_hypothesis_narrowing_resets_stagnation(self):
        s = state_at("OBSERVE", no_progress=3)
        signals = normalize_signals({"hypothesis_narrowing": 1})
        s2 = update_reasoning_state(s, signals)
        assert s2.no_progress_steps == 0

    # I2: no-signal increments
    def test_I2_no_signal_increments(self):
        s = state_at("OBSERVE", no_progress=1)
        s2 = update_reasoning_state(s, no_progress_signals())
        assert s2.no_progress_steps == 2

    def test_I2_accumulates_to_threshold(self):
        s = initial_reasoning_state("OBSERVE")
        for _ in range(NO_PROGRESS_THRESHOLD):
            s = update_reasoning_state(s, no_progress_signals())
        assert s.no_progress_steps == NO_PROGRESS_THRESHOLD

    # I3: observation fields overwrite
    def test_I3_fields_overwrite_not_accumulate(self):
        s = state_at("OBSERVE")
        s = update_reasoning_state(s, normalize_signals({"evidence_gain": 1}))
        assert s.evidence_gain == 1
        # next step: no evidence_gain in signals → overwrites back to 0
        s = update_reasoning_state(s, no_progress_signals())
        assert s.evidence_gain == 0

    # Pure function: prev not mutated
    def test_pure_function_no_mutation(self):
        s = state_at("OBSERVE", no_progress=1)
        _ = update_reasoning_state(s, no_progress_signals())
        assert s.no_progress_steps == 1  # unchanged

    def test_pure_function_produces_new_object(self):
        s = initial_reasoning_state("OBSERVE")
        s2 = update_reasoning_state(s, no_progress_signals())
        assert s is not s2

    # C5: false progress — files written but tests unchanged ≠ progress
    def test_C5_false_progress_does_not_reset_stagnation(self):
        """
        actionability=1 (patch non-empty) without test count change should NOT
        reset no_progress_steps. Only evidence_gain/hypothesis_narrowing resets.
        """
        s = state_at("OBSERVE", no_progress=2)
        # actionability alone (patch written, no test progress)
        signals = normalize_signals({"actionability": 1})
        s2 = update_reasoning_state(s, signals)
        assert s2.no_progress_steps == 3  # incremented, not reset


# ── decide_next ───────────────────────────────────────────────────────────────

class TestDecideNext:

    # task_success → STOP regardless of phase
    @pytest.mark.parametrize("phase", ["UNDERSTAND", "OBSERVE", "ANALYZE", "DECIDE", "EXECUTE", "JUDGE"])
    def test_task_success_stops_all_phases(self, phase):
        s = state_at(phase)
        signals = normalize_signals({"task_success": True})
        s = update_reasoning_state(s, signals)
        verdict = decide_next(s)
        assert isinstance(verdict, VerdictStop)
        assert verdict.reason == "task_success"

    # env_noise → REDIRECT unconditionally (CORR3)
    def test_env_noise_redirects(self):
        s = state_at("OBSERVE")
        signals = normalize_signals({"env_noise": True})
        s = update_reasoning_state(s, signals)
        verdict = decide_next(s)
        assert isinstance(verdict, VerdictRedirect)
        assert verdict.to == "ANALYZE"

    def test_env_noise_overrides_stagnation(self):
        """env_noise takes priority 2, stagnation is 3 — redirect wins."""
        s = state_at("OBSERVE", no_progress=NO_PROGRESS_THRESHOLD)
        signals = normalize_signals({"env_noise": True})
        s = update_reasoning_state(s, signals)
        verdict = decide_next(s)
        assert isinstance(verdict, VerdictRedirect)

    # stagnation → ADVANCE or STOP(no_signal)
    def test_stagnation_observe_advances_to_analyze(self):
        s = initial_reasoning_state("OBSERVE")
        for _ in range(NO_PROGRESS_THRESHOLD):
            s = update_reasoning_state(s, no_progress_signals())
        verdict = decide_next(s)
        assert isinstance(verdict, VerdictAdvance)
        assert verdict.to == "ANALYZE"

    def test_stagnation_judge_stops(self):
        s = initial_reasoning_state("JUDGE")
        for _ in range(NO_PROGRESS_THRESHOLD):
            s = update_reasoning_state(s, no_progress_signals())
        verdict = decide_next(s)
        assert isinstance(verdict, VerdictStop)
        assert verdict.reason == "no_signal"

    @pytest.mark.parametrize("phase,expected_to", [
        ("UNDERSTAND", "OBSERVE"),
        ("OBSERVE",    "ANALYZE"),
        ("ANALYZE",    "DECIDE"),
        ("DECIDE",     "EXECUTE"),
        ("EXECUTE",    "JUDGE"),
    ])
    def test_stagnation_advance_table(self, phase, expected_to):
        s = initial_reasoning_state(phase)
        for _ in range(NO_PROGRESS_THRESHOLD):
            s = update_reasoning_state(s, no_progress_signals())
        verdict = decide_next(s)
        assert isinstance(verdict, VerdictAdvance)
        assert verdict.to == expected_to

    # phase gates
    def test_observe_hypothesis_narrowing_advances(self):
        s = initial_reasoning_state("OBSERVE")
        signals = normalize_signals({"hypothesis_narrowing": 1})
        s = update_reasoning_state(s, signals)
        verdict = decide_next(s)
        assert isinstance(verdict, VerdictAdvance)
        assert verdict.to == "ANALYZE"

    def test_analyze_actionability_advances_to_execute(self):
        """CORR2: actionability = patch_non_empty (pre-execution). Advances ANALYZE→EXECUTE."""
        s = initial_reasoning_state("ANALYZE")
        signals = normalize_signals({"actionability": 1})
        s = update_reasoning_state(s, signals)
        verdict = decide_next(s)
        assert isinstance(verdict, VerdictAdvance)
        assert verdict.to == "EXECUTE"

    def test_continue_when_no_signal(self):
        # no_progress=0, one step → no_progress_steps=1, still below threshold (2)
        s = state_at("OBSERVE", no_progress=0)
        s = update_reasoning_state(s, no_progress_signals())
        assert s.no_progress_steps == 1  # below NO_PROGRESS_THRESHOLD=2
        verdict = decide_next(s)
        assert isinstance(verdict, VerdictContinue)

    def test_evidence_gain_without_narrowing_does_not_advance_observe(self):
        """evidence_gain alone doesn't trigger the OBSERVE gate (needs narrowing)."""
        s = initial_reasoning_state("OBSERVE")
        signals = normalize_signals({"evidence_gain": 1})
        s = update_reasoning_state(s, signals)
        verdict = decide_next(s)
        assert isinstance(verdict, VerdictContinue)


# ── swe_signal_adapter ────────────────────────────────────────────────────────

class TestSweSignalAdapter:

    # extract_step_signals

    def test_step_no_signals_when_no_progress(self):
        partial = extract_step_signals(
            tests_passed_count=5,
            tests_passed_prev=5,
            env_error_detected=False,
            patch_non_empty=False,
        )
        assert partial == {}

    def test_step_evidence_gain_when_tests_increase(self):
        partial = extract_step_signals(
            tests_passed_count=6,
            tests_passed_prev=5,
            env_error_detected=False,
            patch_non_empty=False,
        )
        assert partial.get("evidence_gain") == 1
        assert partial.get("hypothesis_narrowing") == 1

    def test_step_actionability_when_patch_non_empty(self):
        """CORR2: actionability = patch_non_empty, not verify result."""
        partial = extract_step_signals(
            tests_passed_count=5,
            tests_passed_prev=5,
            env_error_detected=False,
            patch_non_empty=True,
        )
        assert partial.get("actionability") == 1
        assert "task_success" not in partial  # CORR1: task_success NOT in step signals

    def test_step_env_noise(self):
        partial = extract_step_signals(
            tests_passed_count=5,
            tests_passed_prev=5,
            env_error_detected=True,
            patch_non_empty=False,
        )
        assert partial.get("env_noise") is True

    def test_C5_files_written_but_no_test_change_no_evidence(self):
        """C5: patch_non_empty alone does NOT set evidence_gain."""
        partial = extract_step_signals(
            tests_passed_count=5,
            tests_passed_prev=5,
            env_error_detected=False,
            patch_non_empty=True,       # files written
        )
        assert "evidence_gain" not in partial
        assert "hypothesis_narrowing" not in partial
        # only actionability is set
        assert partial.get("actionability") == 1

    def test_step_no_task_success(self):
        """CORR1: extract_step_signals must never set task_success."""
        for cv_passed in [True, False]:
            partial = extract_step_signals(
                tests_passed_count=10,
                tests_passed_prev=5,
                env_error_detected=False,
                patch_non_empty=True,
            )
            assert "task_success" not in partial

    # extract_verify_signals

    def test_verify_passed_sets_task_success(self):
        partial = extract_verify_signals(controlled_verify_passed=True)
        assert partial == {"task_success": True}

    def test_verify_failed_sets_task_success_false(self):
        partial = extract_verify_signals(controlled_verify_passed=False)
        assert partial == {"task_success": False}

    def test_verify_only_sets_task_success(self):
        """CORR1: verify signals must not contain evidence_gain or other step signals."""
        partial = extract_verify_signals(controlled_verify_passed=True)
        assert set(partial.keys()) == {"task_success"}


# ── End-to-end integration scenarios ─────────────────────────────────────────

class TestIntegrationScenarios:
    """
    Full chain: extract_step_signals → normalize_signals → update_reasoning_state → decide_next
    Then: extract_verify_signals → normalize_signals → update_reasoning_state (separate call)
    """

    def test_C1_verify_pass_stops(self):
        """C1: controlled_verify passes → task_success → VerdictStop."""
        # Use EXECUTE phase (no phase gates active there)
        s = initial_reasoning_state("EXECUTE")
        # step: patch written, tests improved
        step_partial = extract_step_signals(
            tests_passed_count=8, tests_passed_prev=5,
            env_error_detected=False, patch_non_empty=True,
        )
        s = update_reasoning_state(s, normalize_signals(step_partial))
        # EXECUTE has no phase gate for hypothesis_narrowing/actionability → CONTINUE
        assert isinstance(decide_next(s), VerdictContinue)  # not yet

        # verify: passes — separate update call (CORR1)
        verify_partial = extract_verify_signals(controlled_verify_passed=True)
        s = update_reasoning_state(s, normalize_signals(verify_partial))
        verdict = decide_next(s)
        assert isinstance(verdict, VerdictStop)
        assert verdict.reason == "task_success"

    def test_C2_stagnation_truncated(self):
        """C2: 2 steps without test progress → VerdictAdvance."""
        s = initial_reasoning_state("OBSERVE")
        for _ in range(NO_PROGRESS_THRESHOLD):
            step_partial = extract_step_signals(
                tests_passed_count=5, tests_passed_prev=5,  # no test change
                env_error_detected=False, patch_non_empty=False,
            )
            s = update_reasoning_state(s, normalize_signals(step_partial))
        verdict = decide_next(s)
        assert isinstance(verdict, VerdictAdvance)
        assert verdict.to == "ANALYZE"

    def test_C3_env_noise_redirect(self):
        """C3: env error → VerdictRedirect (unconditional)."""
        s = initial_reasoning_state("OBSERVE")
        step_partial = extract_step_signals(
            tests_passed_count=5, tests_passed_prev=5,
            env_error_detected=True, patch_non_empty=False,
        )
        s = update_reasoning_state(s, normalize_signals(step_partial))
        verdict = decide_next(s)
        assert isinstance(verdict, VerdictRedirect)
        assert verdict.to == "ANALYZE"

    def test_C4_normal_progress_does_not_interfere(self):
        """C4: test count increasing every step → stagnation never triggers."""
        s = initial_reasoning_state("OBSERVE")
        base = 0
        for i in range(5):
            step_partial = extract_step_signals(
                tests_passed_count=base + 1, tests_passed_prev=base,
                env_error_detected=False, patch_non_empty=True,
            )
            s = update_reasoning_state(s, normalize_signals(step_partial))
            base += 1
            assert s.no_progress_steps == 0
        verdict = decide_next(s)
        # hypothesis_narrowing > 0 in OBSERVE → ADVANCE
        assert isinstance(verdict, VerdictAdvance)

    def test_C5_false_progress_patch_only(self):
        """C5: writing patch without test improvement → stagnation increments."""
        s = initial_reasoning_state("OBSERVE")
        for _ in range(NO_PROGRESS_THRESHOLD):
            step_partial = extract_step_signals(
                tests_passed_count=5, tests_passed_prev=5,  # no test change
                env_error_detected=False, patch_non_empty=True,  # patch written
            )
            s = update_reasoning_state(s, normalize_signals(step_partial))
        assert s.no_progress_steps == NO_PROGRESS_THRESHOLD
        verdict = decide_next(s)
        # stagnation triggers despite patch being written
        assert isinstance(verdict, VerdictAdvance)

    def test_signal_separation_step_then_verify(self):
        """CORR1: step signals and verify signals are applied as separate update calls."""
        s = initial_reasoning_state("EXECUTE")
        # step
        step_partial = extract_step_signals(
            tests_passed_count=3, tests_passed_prev=1,
            env_error_detected=False, patch_non_empty=True,
        )
        s = update_reasoning_state(s, normalize_signals(step_partial))
        assert s.task_success is False  # not yet set by step

        # verify — separate call
        verify_partial = extract_verify_signals(controlled_verify_passed=True)
        s = update_reasoning_state(s, normalize_signals(verify_partial))
        assert s.task_success is True   # now set
        assert isinstance(decide_next(s), VerdictStop)
