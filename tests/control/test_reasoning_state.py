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
    NO_PROGRESS_THRESHOLD, PHASE_STEP_BUDGET, reset_phase_steps,
)
from control.swe_signal_adapter import extract_step_signals, extract_verify_signals, extract_weak_progress


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
        # EXECUTE is excluded: 改动5 changed EXECUTE stagnation to VerdictRedirect(DECIDE)
        # See test_execute_stagnation_redirects_to_decide below.
    ])
    def test_stagnation_advance_table(self, phase, expected_to):
        s = initial_reasoning_state(phase)
        for _ in range(NO_PROGRESS_THRESHOLD):
            s = update_reasoning_state(s, no_progress_signals())
        verdict = decide_next(s)
        assert isinstance(verdict, VerdictAdvance)
        assert verdict.to == expected_to

    def test_execute_stagnation_redirects_to_decide(self):
        """改动5: EXECUTE stagnation → VerdictRedirect(DECIDE), not VerdictAdvance(JUDGE).
        Agent has no patch yet — send back to DECIDE to rethink, not forward to JUDGE."""
        s = initial_reasoning_state("EXECUTE")
        for _ in range(NO_PROGRESS_THRESHOLD):
            s = update_reasoning_state(s, no_progress_signals())
        verdict = decide_next(s)
        assert isinstance(verdict, VerdictRedirect)
        assert verdict.to == "DECIDE"
        assert verdict.reason == "execute_no_progress"

    # phase gates
    def test_observe_hypothesis_narrowing_advances(self):
        s = initial_reasoning_state("OBSERVE")
        signals = normalize_signals({"hypothesis_narrowing": 1})
        s = update_reasoning_state(s, signals)
        verdict = decide_next(s)
        assert isinstance(verdict, VerdictAdvance)
        assert verdict.to == "ANALYZE"

    def test_analyze_actionability_advances_to_execute(self):
        """CORR2: actionability = patch_non_empty (pre-execution). Advances ANALYZE→DECIDE (P2)."""
        s = initial_reasoning_state("ANALYZE")
        signals = normalize_signals({"actionability": 1})
        s = update_reasoning_state(s, signals)
        verdict = decide_next(s)
        assert isinstance(verdict, VerdictAdvance)
        assert verdict.to == "DECIDE"

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
        partial, pee, _ = extract_step_signals(
            tests_passed_count=5,
            tests_passed_prev=5,
            env_error_detected=False,
            patch_non_empty=False,
        )
        assert partial == {}
        assert pee is False  # no boundary event

    def test_step_evidence_gain_when_tests_increase(self):
        partial, pee, _ = extract_step_signals(
            tests_passed_count=6,
            tests_passed_prev=5,
            env_error_detected=False,
            patch_non_empty=False,
        )
        assert partial.get("evidence_gain") == 1
        assert partial.get("hypothesis_narrowing") == 1

    def test_step_actionability_when_patch_non_empty(self):
        """CORR2: actionability = patch_non_empty, not verify result."""
        partial, pee, _ = extract_step_signals(
            tests_passed_count=5,
            tests_passed_prev=5,
            env_error_detected=False,
            patch_non_empty=True,
        )
        assert partial.get("actionability") == 1
        assert "task_success" not in partial  # CORR1: task_success NOT in step signals

    def test_step_env_noise(self):
        partial, pee, _ = extract_step_signals(
            tests_passed_count=5,
            tests_passed_prev=5,
            env_error_detected=True,
            patch_non_empty=False,
        )
        assert partial.get("env_noise") is True

    def test_C5_files_written_but_no_test_change_no_evidence(self):
        """C5: patch_non_empty alone does NOT set evidence_gain."""
        partial, pee, _ = extract_step_signals(
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
            partial, _, _r = extract_step_signals(
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
        step_partial, _, _r = extract_step_signals(
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
            step_partial, _, _r = extract_step_signals(
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
        step_partial, _, _r = extract_step_signals(
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
            step_partial, _, _r = extract_step_signals(
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
            step_partial, _, _r = extract_step_signals(
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
        step_partial, _, _r = extract_step_signals(
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


class TestB2CpStateHolder:
    """B2: cross-attempt cp_state persistence via holder list."""

    def test_holder_updated_by_step_signals(self):
        """Step signals update holder[0], caller sees updated state."""
        holder = [initial_reasoning_state("OBSERVE")]
        step_partial, _, _r = extract_step_signals(
            tests_passed_count=2, tests_passed_prev=0,
            env_error_detected=False, patch_non_empty=True,
        )
        holder[0] = update_reasoning_state(holder[0], normalize_signals(step_partial))
        assert holder[0].step_index == 1
        assert holder[0].no_progress_steps == 0  # test count increased → progress

    def test_holder_accumulates_across_steps(self):
        """Multiple step updates accumulate no_progress correctly."""
        holder = [initial_reasoning_state("OBSERVE")]
        # 3 no-progress steps
        for _ in range(3):
            step_partial, _, _r = extract_step_signals(
                tests_passed_count=-1, tests_passed_prev=-1,
                env_error_detected=False, patch_non_empty=False,
            )
            holder[0] = update_reasoning_state(holder[0], normalize_signals(step_partial))
        assert holder[0].step_index == 3
        assert holder[0].no_progress_steps == 3

    def test_verify_signal_applied_after_steps_preserves_step_index(self):
        """Verify signal at attempt boundary does not reset step_index from steps."""
        holder = [initial_reasoning_state("OBSERVE")]
        # Simulate 5 steps
        for _ in range(5):
            step_partial, _, _r = extract_step_signals(
                tests_passed_count=-1, tests_passed_prev=-1,
                env_error_detected=False, patch_non_empty=False,
            )
            holder[0] = update_reasoning_state(holder[0], normalize_signals(step_partial))
        assert holder[0].step_index == 5
        # Apply verify signal
        verify_partial = extract_verify_signals(controlled_verify_passed=True)
        holder[0] = update_reasoning_state(holder[0], normalize_signals(verify_partial))
        assert holder[0].step_index == 6   # incremented once more
        assert holder[0].task_success is True
        assert isinstance(decide_next(holder[0]), VerdictStop)

    def test_env_error_in_step_triggers_redirect(self):
        """env_error_detected=True in a step sets env_noise → VerdictRedirect."""
        holder = [initial_reasoning_state("OBSERVE")]
        step_partial, _, _r = extract_step_signals(
            tests_passed_count=-1, tests_passed_prev=-1,
            env_error_detected=True, patch_non_empty=False,
        )
        holder[0] = update_reasoning_state(holder[0], normalize_signals(step_partial))
        verdict = decide_next(holder[0])
        assert isinstance(verdict, VerdictRedirect)


# ── B3 Stagnation Gating (verify-window level) ────────────────────────────────

class TestB3StagnationGating:
    """
    B3.2 — stagnation is a verify-window concept, not a step concept.
    update_stagnation=False: step-level calls; no_progress_steps frozen.
    update_stagnation=True (default): verify-boundary calls; I1/I2 apply.
    """

    def test_step_level_does_not_advance_no_progress(self):
        """update_stagnation=False: no_progress_steps stays at prev value."""
        s = state_at("OBSERVE", no_progress=1)
        s2 = update_reasoning_state(s, no_progress_signals(), update_stagnation=False)
        assert s2.no_progress_steps == 1  # frozen — not incremented

    def test_step_level_does_not_reset_no_progress(self):
        """update_stagnation=False: even evidence_gain does NOT reset stagnation."""
        s = state_at("OBSERVE", no_progress=3)
        signals = normalize_signals({"evidence_gain": 1})
        s2 = update_reasoning_state(s, signals, update_stagnation=False)
        assert s2.no_progress_steps == 3  # frozen — not reset either

    def test_verify_window_increments_no_progress(self):
        """update_stagnation=True (default): I2 applies — no-signal increments."""
        s = state_at("OBSERVE", no_progress=1)
        s2 = update_reasoning_state(s, no_progress_signals(), update_stagnation=True)
        assert s2.no_progress_steps == 2

    def test_verify_window_resets_on_evidence(self):
        """update_stagnation=True (default): I1 applies — evidence_gain resets."""
        s = state_at("OBSERVE", no_progress=3)
        signals = normalize_signals({"evidence_gain": 1})
        s2 = update_reasoning_state(s, signals, update_stagnation=True)
        assert s2.no_progress_steps == 0

    def test_many_step_calls_then_one_verify_call(self):
        """
        Core B3.2 scenario:
        N step calls (update_stagnation=False) → no_progress stays 0
        1 verify call (update_stagnation=True, no evidence) → no_progress = 1
        After NO_PROGRESS_THRESHOLD verify calls without progress → stagnation fires.
        """
        s = initial_reasoning_state("OBSERVE")

        # Simulate 20 agent steps with no test progress — step-level calls
        for _ in range(20):
            step_partial, _, _r = extract_step_signals(
                tests_passed_count=5, tests_passed_prev=5,
                env_error_detected=False, patch_non_empty=True,
            )
            s = update_reasoning_state(s, normalize_signals(step_partial),
                                       update_stagnation=False)

        # After 20 steps, no_progress should still be 0 (frozen at step level)
        assert s.no_progress_steps == 0
        assert s.step_index == 20

        # Now apply 1 verify window with no test progress
        verify_partial = extract_verify_signals(controlled_verify_passed=False)
        # verify signals only set task_success; we also feed step signals for the window
        window_partial, _, _r = extract_step_signals(
            tests_passed_count=5, tests_passed_prev=5,
            env_error_detected=False, patch_non_empty=True,
        )
        s = update_reasoning_state(s, normalize_signals(window_partial),
                                   update_stagnation=True)
        assert s.no_progress_steps == 1  # first verify window without progress

        # One more verify window without progress (window_partial already unpacked above)
        s = update_reasoning_state(s, normalize_signals(window_partial),
                                   update_stagnation=True)
        assert s.no_progress_steps == 2

        # Keep adding verify windows until threshold
        for _ in range(NO_PROGRESS_THRESHOLD - 2):
            s = update_reasoning_state(s, normalize_signals(window_partial),
                                       update_stagnation=True)
        assert s.no_progress_steps == NO_PROGRESS_THRESHOLD

        # Now stagnation should fire
        verdict = decide_next(s)
        assert isinstance(verdict, VerdictAdvance)

    def test_step_calls_do_not_trigger_stagnation_verdict(self):
        """
        With step calls under the phase budget (update_stagnation=False), verdict is CONTINUE
        because no_progress_steps never advances.
        Note: phase_steps still increments, so iterations must stay under PHASE_STEP_BUDGET.
        """
        s = initial_reasoning_state("OBSERVE")
        # Stay under OBSERVE budget (10) — 5 steps is safe
        for _ in range(5):
            s = update_reasoning_state(s, no_progress_signals(), update_stagnation=False)
        assert s.no_progress_steps == 0
        verdict = decide_next(s)
        assert isinstance(verdict, VerdictContinue)

    def test_step_index_still_increments_with_false(self):
        """I4 still holds: step_index monotone even when update_stagnation=False."""
        s = initial_reasoning_state("OBSERVE")
        for i in range(1, 6):
            s = update_reasoning_state(s, no_progress_signals(), update_stagnation=False)
            assert s.step_index == i

    def test_default_parameter_is_true(self):
        """Calling without update_stagnation arg behaves like update_stagnation=True."""
        s = state_at("OBSERVE", no_progress=1)
        s_default = update_reasoning_state(s, no_progress_signals())        # no arg
        s_explicit = update_reasoning_state(s, no_progress_signals(), update_stagnation=True)
        assert s_default.no_progress_steps == s_explicit.no_progress_steps


# ── B3.3 Weak Progress ────────────────────────────────────────────────────────

class TestB3WeakProgress:
    """
    B3.3 — extract_weak_progress() is a log-only diagnostic.
    Does NOT affect stagnation counter. Signals: patch_non_empty OR env_error OR tests>=0.
    """

    def test_patch_non_empty_is_weak_signal(self):
        assert extract_weak_progress(
            env_error_detected=False,
            patch_non_empty=True,
            latest_tests_passed=0,
        ) is True

    def test_env_error_is_weak_signal(self):
        assert extract_weak_progress(
            env_error_detected=True,
            patch_non_empty=False,
            latest_tests_passed=-1,
        ) is True

    def test_tests_passed_zero_is_weak_signal(self):
        """latest_tests_passed >= 0 means test data available — weak signal."""
        assert extract_weak_progress(
            env_error_detected=False,
            patch_non_empty=False,
            latest_tests_passed=0,
        ) is True

    def test_all_false_negative_tests_is_not_weak(self):
        """No patch, no env error, no test data (< 0) → no weak signal."""
        assert extract_weak_progress(
            env_error_detected=False,
            patch_non_empty=False,
            latest_tests_passed=-1,
        ) is False

    def test_all_signals_present_is_weak(self):
        assert extract_weak_progress(
            env_error_detected=True,
            patch_non_empty=True,
            latest_tests_passed=5,
        ) is True

    def test_weak_progress_does_not_affect_stagnation(self):
        """
        Weak progress is log-only. The stagnation counter is NOT influenced
        by patch_non_empty alone — only by evidence_gain/hypothesis_narrowing at verify boundary.
        """
        s = initial_reasoning_state("OBSERVE")
        # step with patch but no test change
        step_partial, _, _r = extract_step_signals(
            tests_passed_count=3, tests_passed_prev=3,
            env_error_detected=False, patch_non_empty=True,
        )
        # step-level call (no stagnation) — weak_progress would be True
        s = update_reasoning_state(s, normalize_signals(step_partial),
                                   update_stagnation=False)
        weak = extract_weak_progress(
            env_error_detected=False,
            patch_non_empty=True,
            latest_tests_passed=3,
        )
        assert weak is True
        assert s.no_progress_steps == 0  # not reset because update_stagnation=False


# ── B5 Semantic Event Gating ──────────────────────────────────────────────────

class TestB5SemanticEventGating:
    """
    B5 — progress_evaluable_event gates stagnation updates.

    progress_evaluable_event=True only on semantic boundary events:
      1. inner verify returned new result (verify_history grew)
      2. env failure detected
      3. patch first write (False → True, not subsequent edits)

    Regular read/think/write steps → progress_evaluable_event=False → no_progress frozen.
    """

    # ── progress_evaluable_event logic ────────────────────────────────────────

    def test_pee_false_on_normal_step(self):
        """No boundary event → progress_evaluable_event=False."""
        _, pee, _r = extract_step_signals(
            tests_passed_count=5, tests_passed_prev=5,
            env_error_detected=False, patch_non_empty=True,
            patch_was_non_empty_prev=True,  # already had patch, not first write
            verify_history_len=2, verify_history_len_prev=2,
        )
        assert pee is False

    def test_pee_true_on_inner_verify_new(self):
        """New inner-verify result → progress_evaluable_event=True."""
        _, pee, _r = extract_step_signals(
            tests_passed_count=5, tests_passed_prev=5,
            env_error_detected=False, patch_non_empty=True,
            patch_was_non_empty_prev=True,
            verify_history_len=3, verify_history_len_prev=2,  # grew
        )
        assert pee is True

    def test_pee_true_on_env_error(self):
        """Env failure → progress_evaluable_event=True."""
        _, pee, _r = extract_step_signals(
            tests_passed_count=5, tests_passed_prev=5,
            env_error_detected=True, patch_non_empty=False,
            patch_was_non_empty_prev=False,
            verify_history_len=0, verify_history_len_prev=0,
        )
        assert pee is True

    def test_pee_true_on_patch_first_write(self):
        """patch False→True transition → progress_evaluable_event=True."""
        _, pee, _r = extract_step_signals(
            tests_passed_count=5, tests_passed_prev=5,
            env_error_detected=False, patch_non_empty=True,   # patch now exists
            patch_was_non_empty_prev=False,                   # first write
            verify_history_len=0, verify_history_len_prev=0,
        )
        assert pee is True

    def test_pee_false_on_patch_subsequent_edit(self):
        """Subsequent patch edits do NOT set progress_evaluable_event."""
        _, pee, _r = extract_step_signals(
            tests_passed_count=5, tests_passed_prev=5,
            env_error_detected=False, patch_non_empty=True,
            patch_was_non_empty_prev=True,  # already had patch — not first write
            verify_history_len=0, verify_history_len_prev=0,
        )
        assert pee is False

    def test_pee_false_when_patch_stays_empty(self):
        """Patch stays empty (no write at all) → not a boundary event."""
        _, pee, _r = extract_step_signals(
            tests_passed_count=5, tests_passed_prev=5,
            env_error_detected=False, patch_non_empty=False,
            patch_was_non_empty_prev=False,
            verify_history_len=0, verify_history_len_prev=0,
        )
        assert pee is False

    # ── stagnation gating integration ─────────────────────────────────────────

    def test_no_progress_frozen_on_non_boundary_steps(self):
        """
        Many steps with patch edits (not first write, no inner-verify, no env error)
        → progress_evaluable_event=False each step → no_progress stays 0.
        """
        s = initial_reasoning_state("OBSERVE")
        # First write (makes pee=True once)
        _, pee, _r = extract_step_signals(
            tests_passed_count=5, tests_passed_prev=5,
            env_error_detected=False, patch_non_empty=True,
            patch_was_non_empty_prev=False,  # first write
            verify_history_len=0, verify_history_len_prev=0,
        )
        step_partial_first, _, _r = extract_step_signals(
            tests_passed_count=5, tests_passed_prev=5,
            env_error_detected=False, patch_non_empty=True,
            patch_was_non_empty_prev=False,
        )
        s = update_reasoning_state(s, normalize_signals(step_partial_first),
                                   update_stagnation=pee)  # True for first write

        assert s.no_progress_steps == 1  # boundary event → stagnation advanced

        # Subsequent edits (pee=False) — 30 steps
        for _ in range(30):
            step_partial, pee, _ = extract_step_signals(
                tests_passed_count=5, tests_passed_prev=5,
                env_error_detected=False, patch_non_empty=True,
                patch_was_non_empty_prev=True,  # not first write
                verify_history_len=0, verify_history_len_prev=0,
            )
            s = update_reasoning_state(s, normalize_signals(step_partial),
                                       update_stagnation=pee)

        # no_progress frozen at 1 — 30 non-boundary steps didn't advance it
        assert s.no_progress_steps == 1

    def test_no_progress_advances_on_inner_verify(self):
        """
        Inner-verify result arrives → pee=True → stagnation can advance.
        """
        s = initial_reasoning_state("OBSERVE")

        # 10 non-boundary steps (pee=False each)
        for _ in range(10):
            step_partial, pee, _ = extract_step_signals(
                tests_passed_count=5, tests_passed_prev=5,
                env_error_detected=False, patch_non_empty=True,
                patch_was_non_empty_prev=True,
                verify_history_len=0, verify_history_len_prev=0,
            )
            s = update_reasoning_state(s, normalize_signals(step_partial),
                                       update_stagnation=pee)

        assert s.no_progress_steps == 0  # frozen

        # Inner verify fires (pee=True)
        step_partial, pee, _ = extract_step_signals(
            tests_passed_count=5, tests_passed_prev=5,
            env_error_detected=False, patch_non_empty=True,
            patch_was_non_empty_prev=True,
            verify_history_len=1, verify_history_len_prev=0,  # new inner-verify
        )
        assert pee is True
        s = update_reasoning_state(s, normalize_signals(step_partial),
                                   update_stagnation=pee)

        assert s.no_progress_steps == 1  # advanced on boundary event

    def test_pee_all_three_conditions_simultaneously(self):
        """All three boundary conditions at once → pee=True (any is sufficient)."""
        _, pee, _r = extract_step_signals(
            tests_passed_count=5, tests_passed_prev=5,
            env_error_detected=True,
            patch_non_empty=True,
            patch_was_non_empty_prev=False,
            verify_history_len=2, verify_history_len_prev=1,
        )
        assert pee is True

    def test_pee_default_args_no_boundary(self):
        """Default optional args: no prev state → only env_error or patch_first_write can set pee."""
        # With all defaults (no prev data), patch_first_write can't trigger (patch_non_empty=False)
        _, pee, _r = extract_step_signals(
            tests_passed_count=5, tests_passed_prev=5,
            env_error_detected=False, patch_non_empty=False,
        )
        assert pee is False


# ── Phase Budget Tests ────────────────────────────────────────────────────────

class TestPhaseBudget:

    def test_phase_steps_increments(self):
        """phase_steps increments by 1 on each update_reasoning_state call."""
        s = initial_reasoning_state("OBSERVE")
        assert s.phase_steps == 0
        s = update_reasoning_state(s, no_progress_signals())
        assert s.phase_steps == 1
        s = update_reasoning_state(s, no_progress_signals())
        assert s.phase_steps == 2

    def test_budget_triggers_advance(self):
        """When phase_steps >= budget, decide_next returns VerdictAdvance."""
        budget = PHASE_STEP_BUDGET["OBSERVE"]
        s = ReasoningState(phase="OBSERVE", phase_steps=budget)
        verdict = decide_next(s)
        assert isinstance(verdict, VerdictAdvance)
        assert verdict.to == "ANALYZE"

    def test_under_budget_continues(self):
        """When phase_steps < budget, decide_next returns VerdictContinue."""
        budget = PHASE_STEP_BUDGET["OBSERVE"]
        s = ReasoningState(phase="OBSERVE", phase_steps=budget - 1)
        verdict = decide_next(s)
        assert isinstance(verdict, VerdictContinue)

    def test_judge_budget_stops(self):
        """JUDGE has no next phase — budget exhausted returns VerdictStop."""
        budget = PHASE_STEP_BUDGET["JUDGE"]
        s = ReasoningState(phase="JUDGE", phase_steps=budget)
        verdict = decide_next(s)
        assert isinstance(verdict, VerdictStop)
        assert verdict.reason == "phase_budget_exhausted"

    def test_execute_has_largest_budget(self):
        """EXECUTE budget is the largest (25) to allow patch iteration."""
        assert PHASE_STEP_BUDGET["EXECUTE"] >= 20
        assert PHASE_STEP_BUDGET["EXECUTE"] > PHASE_STEP_BUDGET["OBSERVE"]

    def test_reset_phase_steps(self):
        """reset_phase_steps sets phase_steps to 0."""
        s = ReasoningState(phase="OBSERVE", phase_steps=15)
        s2 = reset_phase_steps(s)
        assert s2.phase_steps == 0
        assert s2.phase == "OBSERVE"  # phase unchanged

    def test_budget_priority_below_task_success(self):
        """task_success (priority 1) takes precedence over budget (priority 2.75)."""
        budget = PHASE_STEP_BUDGET["OBSERVE"]
        s = ReasoningState(phase="OBSERVE", phase_steps=budget, task_success=True)
        verdict = decide_next(s)
        assert isinstance(verdict, VerdictStop)
        assert verdict.reason == "task_success"

    def test_budget_priority_below_env_noise(self):
        """env_noise (priority 2) takes precedence over budget (priority 2.75)."""
        budget = PHASE_STEP_BUDGET["OBSERVE"]
        s = ReasoningState(phase="OBSERVE", phase_steps=budget, env_noise=True)
        verdict = decide_next(s)
        assert isinstance(verdict, VerdictRedirect)
        assert verdict.reason == "env_noise detected"
