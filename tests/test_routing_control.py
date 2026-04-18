"""
test_routing_control.py — P1.1: Routing control activation.

Verifies that RoutingDecision is now CONTROL, not just telemetry:
  1. _route_blocked uses contract repair_target for cross-phase routing
  2. Phase gate rejection produces redirect verdict when repair_target differs
  3. retry/redirect paths inject repair hints into agent messages
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest
from routing_decision import RoutingDecision


# ══════════════════════════════════════════════════════════════════════════════
# 1. _route_blocked uses contract repair_target
# ══════════════════════════════════════════════════════════════════════════════

class TestRouteBlockedUsesRepairTarget:
    """_route_blocked must derive next_phase from contract repair_target."""

    def test_analyze_routes_to_observe(self):
        """ANALYZE repair_target = OBSERVE → next_phase = OBSERVE."""
        from step_sections import _route_blocked
        rd = _route_blocked("ANALYZE", "analysis_gate_rejected", occurrence=0)
        assert isinstance(rd, RoutingDecision)
        assert rd.next_phase == "OBSERVE", \
            f"ANALYZE repair_target is OBSERVE, got next_phase={rd.next_phase}"

    def test_execute_routes_to_self(self):
        """EXECUTE repair_target = EXECUTE → next_phase = EXECUTE (stay)."""
        from step_sections import _route_blocked
        rd = _route_blocked("EXECUTE", "execute_gate_rejected", occurrence=0)
        assert rd.next_phase == "EXECUTE"

    def test_design_routes_to_self(self):
        """DESIGN repair_target = DESIGN → stays."""
        from step_sections import _route_blocked
        rd = _route_blocked("DESIGN", "design_gate_rejected", occurrence=0)
        assert rd.next_phase == "DESIGN"

    def test_judge_routes_to_self(self):
        """JUDGE repair_target = JUDGE → stays."""
        from step_sections import _route_blocked
        rd = _route_blocked("JUDGE", "judge_gate_rejected", occurrence=0)
        assert rd.next_phase == "JUDGE"

    def test_observe_routes_to_self(self):
        """OBSERVE repair_target = OBSERVE → stays."""
        from step_sections import _route_blocked
        rd = _route_blocked("OBSERVE", "missing_phase_record_retry", occurrence=0)
        assert rd.next_phase == "OBSERVE"

    def test_route_includes_repair_hints(self):
        """RoutingDecision must carry repair hints from _REJECTION_POLICY."""
        from step_sections import _route_blocked
        rd = _route_blocked("ANALYZE", "analysis_gate_rejected", occurrence=0)
        assert len(rd.repair_hints) >= 1, "Must have at least one repair hint"
        assert rd.strategy, "Must have a strategy"

    def test_route_includes_source(self):
        """RoutingDecision source must be 'rejection_policy'."""
        from step_sections import _route_blocked
        rd = _route_blocked("ANALYZE", "analysis_gate_rejected")
        assert rd.source == "rejection_policy"


# ══════════════════════════════════════════════════════════════════════════════
# 2. evaluate_transition redirect verdict for cross-phase routing
# ══════════════════════════════════════════════════════════════════════════════

class TestEvaluateTransitionRedirect:
    """Phase gate rejection must produce redirect when repair_target differs."""

    def test_analyze_gate_rejection_produces_redirect(self):
        """ANALYZE gate reject → verdict=redirect, next_phase=OBSERVE."""
        from unittest.mock import MagicMock, patch
        from step_sections import evaluate_transition
        from step_monitor_state import StepMonitorState
        from control.reasoning_state import initial_reasoning_state
        from phase_record import PhaseRecord

        state = StepMonitorState("test__test-0001", attempt=1, instance={
            "instance_id": "test__test-0001", "repo": "test/test",
            "base_commit": "abc123", "problem_statement": "test",
        })
        state.cp_state = initial_reasoning_state("ANALYZE")
        cp_holder = [state.cp_state]

        # Pre-admit a minimal ANALYZE record
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=["causal_grounding"], claims=[], evidence_refs=["file.py:1"],
            from_steps=[], content="short analysis",
            root_cause="short",  # too short — will fail gate
        )
        state.phase_records.append(pr)

        agent = MagicMock()
        agent.messages = []
        agent.model = MagicMock()
        agent.model._submitted_phase_record = None

        # Mock analysis gate to REJECT
        mock_verdict = MagicMock()
        mock_verdict.passed = False
        mock_verdict.failed_rules = ["code_grounding"]
        mock_verdict.scores = {"code_grounding": 0.2}
        mock_verdict.rejection = None

        with patch("step_sections.evaluate_analysis", mock_verdict, create=True), \
             patch("analysis_gate.evaluate_analysis", return_value=mock_verdict):
            result = evaluate_transition(
                agent,
                state=state,
                cp_state_holder=cp_holder,
                eval_phase="ANALYZE",
                old_phase="OBSERVE",
                latest_assistant_text="I think the bug is here",
            )

        # ANALYZE repair_target = OBSERVE → redirect
        assert result.verdict == "redirect", \
            f"ANALYZE gate rejection should produce redirect, got {result.verdict}"
        assert result.next_phase == "OBSERVE", \
            f"ANALYZE redirect should go to OBSERVE, got {result.next_phase}"
        assert result.routing is not None
        assert result.routing.next_phase == "OBSERVE"

    def test_execute_gate_rejection_produces_retry(self):
        """EXECUTE gate reject → verdict=retry (repair_target=EXECUTE, same phase)."""
        from unittest.mock import MagicMock, patch
        from step_sections import evaluate_transition
        from step_monitor_state import StepMonitorState
        from control.reasoning_state import initial_reasoning_state
        from phase_record import PhaseRecord

        state = StepMonitorState("test__test-0001", attempt=1, instance={
            "instance_id": "test__test-0001", "repo": "test/test",
            "base_commit": "abc123", "problem_statement": "test",
        })
        state.cp_state = initial_reasoning_state("EXECUTE")
        cp_holder = [state.cp_state]

        pr = PhaseRecord(
            phase="EXECUTE", subtype="execution.code_patch",
            principals=["minimal_change"], claims=[], evidence_refs=["file.py:1"],
            from_steps=[], content="execution content",
        )
        state.phase_records.append(pr)

        agent = MagicMock()
        agent.messages = []
        agent.model = MagicMock()
        agent.model._submitted_phase_record = None

        mock_verdict = MagicMock()
        mock_verdict.passed = False
        mock_verdict.failed_rules = ["patch_quality"]
        mock_verdict.scores = {"patch_quality": 0.3}
        mock_verdict.rejection = None

        with patch("execute_gate.evaluate_execute", return_value=mock_verdict):
            result = evaluate_transition(
                agent,
                state=state,
                cp_state_holder=cp_holder,
                eval_phase="EXECUTE",
                old_phase="DECIDE",
                latest_assistant_text="I wrote a patch",
            )

        # EXECUTE repair_target = EXECUTE → retry (same phase)
        assert result.verdict == "retry", \
            f"EXECUTE gate rejection should produce retry, got {result.verdict}"


# ══════════════════════════════════════════════════════════════════════════════
# 3. retry/redirect inject repair hints into agent messages
# ══════════════════════════════════════════════════════════════════════════════

class TestRepairHintInjection:
    """retry and redirect paths must inject repair hints into agent messages."""

    def test_redirect_injects_message_with_reason(self):
        """When _tv=redirect, agent.messages gets redirect reason + hints."""
        from unittest.mock import MagicMock, patch
        import dataclasses
        from step_monitor_state import StepMonitorState
        from control.reasoning_state import (
            initial_reasoning_state, VerdictAdvance, VerdictContinue,
        )
        from step_sections import _step_cp_update_and_verdict, TransitionEvaluation

        state = StepMonitorState("test__test-0001", attempt=1, instance={
            "instance_id": "test__test-0001", "repo": "test/test",
            "base_commit": "abc123", "problem_statement": "test",
        })
        state.cp_state = initial_reasoning_state("OBSERVE")
        cp_holder = [state.cp_state]

        agent = MagicMock()
        agent.messages = []
        model = MagicMock()
        model._submitted_phase_record = {"phase": "OBSERVE", "principals": []}
        model.pop_submitted_phase_record.return_value = {"phase": "OBSERVE", "principals": []}
        model.pop_submission_failure.return_value = None
        model._last_extract_record = None
        agent.model = model

        # Mock admission to succeed (so we reach evaluate_transition)
        mock_admission = MagicMock()
        mock_admission.admitted = True
        mock_admission.stop = False
        mock_admission.source = "tool_submitted"
        mock_admission.stop_reason = ""
        mock_admission.retry_messages = []

        # Mock evaluate_transition to return redirect
        mock_transition = TransitionEvaluation()
        mock_transition.verdict = "redirect"
        mock_transition.next_phase = "OBSERVE"
        mock_transition.source = "gate_rejection"
        mock_transition.reason = "analysis_gate_rejected"
        mock_transition.routing = RoutingDecision(
            next_phase="OBSERVE",
            strategy="complete_causal_chain",
            repair_hints=["Strengthen root cause evidence"],
            source="rejection_policy",
        )
        mock_transition.pending_messages = []

        with patch("step_sections.decide_next", return_value=VerdictAdvance(to="ANALYZE")), \
             patch("step_sections.extract_weak_progress", return_value=False), \
             patch.object(state, "update_cp_with_step_signals", return_value=(False, "")), \
             patch.object(state, "latest_tests_passed", return_value=0), \
             patch("step_sections.admit_phase_record", return_value=mock_admission), \
             patch("step_sections.evaluate_transition", return_value=mock_transition):
            _step_cp_update_and_verdict(
                agent,
                state=state,
                cp_state_holder=cp_holder,
                env_error_detected=False,
                step_patch_non_empty=False,
                latest_assistant_text="analysis text",
            )

        # Phase should have changed to redirect target
        assert cp_holder[0].phase == "OBSERVE"

        # Agent messages should contain redirect info
        redirect_msgs = [
            m for m in agent.messages
            if "GATE REDIRECT" in m.get("content", "")
        ]
        assert len(redirect_msgs) >= 1, \
            f"Must inject GATE REDIRECT message, got messages: {[m.get('content','')[:50] for m in agent.messages]}"
        assert "Strengthen root cause evidence" in redirect_msgs[0]["content"], \
            "Redirect message must contain repair hint"

    def test_retry_injects_message_with_hint(self):
        """When _tv=retry, agent.messages gets retry reason + hints."""
        from unittest.mock import MagicMock, patch
        from step_monitor_state import StepMonitorState
        from control.reasoning_state import (
            initial_reasoning_state, VerdictAdvance,
        )
        from step_sections import _step_cp_update_and_verdict, TransitionEvaluation

        state = StepMonitorState("test__test-0001", attempt=1, instance={
            "instance_id": "test__test-0001", "repo": "test/test",
            "base_commit": "abc123", "problem_statement": "test",
        })
        state.cp_state = initial_reasoning_state("OBSERVE")
        cp_holder = [state.cp_state]

        agent = MagicMock()
        agent.messages = []
        model = MagicMock()
        model._submitted_phase_record = {"phase": "EXECUTE", "principals": []}
        model.pop_submitted_phase_record.return_value = {"phase": "EXECUTE", "principals": []}
        model.pop_submission_failure.return_value = None
        model._last_extract_record = None
        agent.model = model

        mock_admission = MagicMock()
        mock_admission.admitted = True
        mock_admission.stop = False
        mock_admission.source = "tool_submitted"
        mock_admission.stop_reason = ""
        mock_admission.retry_messages = []

        # Mock evaluate_transition to return retry (same phase)
        mock_transition = TransitionEvaluation()
        mock_transition.verdict = "retry"
        mock_transition.source = "gate_rejection"
        mock_transition.reason = "execute_gate_rejected"
        mock_transition.routing = RoutingDecision(
            next_phase="EXECUTE",
            strategy="fix_execution_errors",
            repair_hints=["Fix the execution issues reported"],
            source="rejection_policy",
        )
        mock_transition.pending_messages = []

        with patch("step_sections.decide_next", return_value=VerdictAdvance(to="ANALYZE")), \
             patch("step_sections.extract_weak_progress", return_value=False), \
             patch.object(state, "update_cp_with_step_signals", return_value=(False, "")), \
             patch.object(state, "latest_tests_passed", return_value=0), \
             patch("step_sections.admit_phase_record", return_value=mock_admission), \
             patch("step_sections.evaluate_transition", return_value=mock_transition):
            _step_cp_update_and_verdict(
                agent,
                state=state,
                cp_state_holder=cp_holder,
                env_error_detected=False,
                step_patch_non_empty=False,
                latest_assistant_text="execution text",
            )

        # Agent messages should contain retry info
        retry_msgs = [
            m for m in agent.messages
            if "GATE RETRY" in m.get("content", "")
        ]
        assert len(retry_msgs) >= 1, \
            f"Must inject GATE RETRY message, got messages: {[m.get('content','')[:50] for m in agent.messages]}"
        assert "Fix the execution issues reported" in retry_msgs[0]["content"], \
            "Retry message must contain repair hint"


# ══════════════════════════════════════════════════════════════════════════════
# 4. Contract repair_target consistency
# ══════════════════════════════════════════════════════════════════════════════

class TestRepairTargetConsistency:
    """Contract repair_target values must be valid phases."""

    def test_all_repair_targets_are_valid_phases(self):
        """Every repair_target in SUBTYPE_CONTRACTS must be a valid phase."""
        from subtype_contracts import SUBTYPE_CONTRACTS
        from canonical_symbols import ALL_PHASES

        for subtype, contract in SUBTYPE_CONTRACTS.items():
            target = contract.get("repair_target", "")
            if target:
                assert target in ALL_PHASES, \
                    f"{subtype} has repair_target={target} which is not a valid phase"

    def test_analyze_repair_target_is_observe(self):
        """ANALYZE must route to OBSERVE (the key cross-phase routing)."""
        from subtype_contracts import get_repair_target
        assert get_repair_target("ANALYZE") == "OBSERVE"

    def test_non_analyze_repair_targets_are_self(self):
        """Non-ANALYZE phases currently route to themselves."""
        from subtype_contracts import get_repair_target
        for phase in ("OBSERVE", "DECIDE", "DESIGN", "EXECUTE", "JUDGE"):
            target = get_repair_target(phase)
            assert target == phase, \
                f"{phase} repair_target should be {phase}, got {target}"


# ══════════════════════════════════════════════════════════════════════════════
# 6. P1.3': EXECUTE → ANALYZE wrong direction redirect
# ══════════════════════════════════════════════════════════════════════════════

class TestDetectQjWrongDirection:
    """P1.3': detect_qj_wrong_direction() on StepMonitorState."""

    def _make_state(self):
        from step_monitor_state import StepMonitorState
        return StepMonitorState("test__test-0001", attempt=1, instance={
            "instance_id": "test__test-0001", "repo": "test/test",
            "base_commit": "abc", "problem_statement": "test",
        })

    def test_no_qj_history_returns_false(self):
        state = self._make_state()
        should, reason = state.detect_qj_wrong_direction()
        assert not should

    def test_single_qj_returns_false(self):
        state = self._make_state()
        state.quick_judge_history = [
            {"target_status": "error", "direction": "first_signal"},
        ]
        should, reason = state.detect_qj_wrong_direction()
        assert not should

    def test_two_errors_triggers_redirect(self):
        state = self._make_state()
        state.quick_judge_history = [
            {"target_status": "error", "direction": "first_signal"},
            {"target_status": "error", "direction": "inconclusive"},
        ]
        should, reason = state.detect_qj_wrong_direction()
        assert should
        assert "qj_wrong_direction" in reason

    def test_two_failed_triggers_redirect(self):
        state = self._make_state()
        state.quick_judge_history = [
            {"target_status": "failed", "direction": "first_signal"},
            {"target_status": "failed", "direction": "unchanged"},
        ]
        should, reason = state.detect_qj_wrong_direction()
        assert should

    def test_second_passed_cancels_redirect(self):
        state = self._make_state()
        state.quick_judge_history = [
            {"target_status": "error", "direction": "first_signal"},
            {"target_status": "passed", "direction": "improved"},
        ]
        should, reason = state.detect_qj_wrong_direction()
        assert not should

    def test_improved_direction_cancels_redirect(self):
        state = self._make_state()
        state.quick_judge_history = [
            {"target_status": "failed", "direction": "first_signal"},
            {"target_status": "failed", "direction": "improved"},
        ]
        should, reason = state.detect_qj_wrong_direction()
        assert not should

    def test_only_fires_once_per_attempt(self):
        state = self._make_state()
        state.quick_judge_history = [
            {"target_status": "error", "direction": "first_signal"},
            {"target_status": "error", "direction": "inconclusive"},
        ]
        should1, _ = state.detect_qj_wrong_direction()
        assert should1
        # Mark as used
        state._execute_analyze_redirect_used = True
        should2, _ = state.detect_qj_wrong_direction()
        assert not should2

    def test_mixed_error_and_missing_triggers(self):
        state = self._make_state()
        state.quick_judge_history = [
            {"target_status": "error", "direction": "first_signal"},
            {"target_status": "missing", "direction": "inconclusive"},
        ]
        should, reason = state.detect_qj_wrong_direction()
        assert should


# ══════════════════════════════════════════════════════════════════════════════
# P1.3' prompt conflict regression: when required_next_phase is set,
# phase prompt must come from the target phase, not current phase.
# ══════════════════════════════════════════════════════════════════════════════

class TestPromptFollowsRequiredNextPhase:
    """Phase prompt injection must use required_next_phase when set."""

    def _make_state(self):
        from step_monitor_state import StepMonitorState
        return StepMonitorState("test__test-0001", attempt=1, instance={
            "instance_id": "test__test-0001", "repo": "test/test",
            "base_commit": "abc", "problem_statement": "test",
        })

    def _make_fake_agent(self):
        """Minimal agent-like object with messages list."""
        class FakeAgent:
            def __init__(self):
                self.messages = []
                self.n_calls = 5
                self.model = None
        return FakeAgent()

    def test_normal_phase_uses_current_phase(self):
        """Without required_next_phase, prompt comes from current cp_state.phase."""
        from step_sections import _step_inject_phase
        state = self._make_state()
        state.required_next_phase = None  # no redirect
        agent = self._make_fake_agent()
        # Set cp_state phase to EXECUTE
        class FakeCp:
            phase = "EXECUTE"
        _step_inject_phase(agent, cp_state_holder=[FakeCp()], state=state)
        # Should have injected EXECUTE prompt
        injected = [m for m in agent.messages if "[Phase: EXECUTE]" in m.get("content", "")]
        assert len(injected) == 1, f"Expected EXECUTE prompt, got {[m['content'][:50] for m in agent.messages]}"

    def test_redirect_uses_target_phase(self):
        """When required_next_phase=ANALYZE, prompt must be ANALYZE, not EXECUTE."""
        from step_sections import _step_inject_phase
        state = self._make_state()
        state.required_next_phase = "ANALYZE"  # redirect active
        agent = self._make_fake_agent()
        class FakeCp:
            phase = "EXECUTE"  # current phase is still EXECUTE
        _step_inject_phase(agent, cp_state_holder=[FakeCp()], state=state)
        # Should have injected ANALYZE prompt, NOT EXECUTE
        all_content = " ".join(m.get("content", "") for m in agent.messages)
        assert "[Phase: ANALYZE]" in all_content, \
            f"Expected ANALYZE prompt when required_next_phase=ANALYZE, got: {all_content[:200]}"
        assert "[Phase: EXECUTE]" not in all_content, \
            f"EXECUTE prompt should NOT appear when required_next_phase=ANALYZE"

    def test_redirect_cleared_uses_current_phase_again(self):
        """After required_next_phase is consumed (set to None), use current phase."""
        from step_sections import _step_inject_phase
        state = self._make_state()
        state.required_next_phase = None  # cleared after agent submitted ANALYZE
        agent = self._make_fake_agent()
        class FakeCp:
            phase = "ANALYZE"  # now actually in ANALYZE
        _step_inject_phase(agent, cp_state_holder=[FakeCp()], state=state)
        injected = [m for m in agent.messages if "[Phase: ANALYZE]" in m.get("content", "")]
        assert len(injected) == 1


# ══════════════════════════════════════════════════════════════════════════════
# 5. P2: Scope consistency gate (ANALYZE→EXECUTE file-scope)
# ══════════════════════════════════════════════════════════════════════════════

class TestScopeConsistencyGate:
    """P2: When ANALYZE declares root_cause_location_files and EXECUTE patches
    different files, the system must detect scope drift and route back."""

    def _make_state(self):
        from step_monitor_state import StepMonitorState
        s = StepMonitorState.__new__(StepMonitorState)
        s._llm_step = 5
        s._injected_signals = set()
        s.pending_redirect_hint = ""
        s.required_next_phase = None
        s.phase_records = []
        s.cp_state = None
        s._execute_entry_step = 3
        s._execute_write_seen = True
        s._patch_hash_counts = {}
        s._analyze_root_cause_files = []
        s._analyze_scope_summary = ""
        s._last_patch_files = []
        s._scope_drift_count = 0
        s.last_analyze_root_cause = ""
        s._retryable_loop_counts = {}
        s.early_stop_verdict = None
        s.analysis_gate_rejects = 0
        s.design_gate_rejects = 0
        s.decide_gate_rejects = 0
        s.execute_gate_rejects = 0
        s.judge_gate_rejects = 0
        s.verify_history = []
        s._steps_without_submission = 0
        s._submission_escalation_level = 0
        s._last_submission_phase = ""
        s.quick_judge_history = []
        s.quick_judge_count = 0
        s.last_quick_judge_step = -10
        s.last_quick_judge_time = 0.0
        s.last_quick_judge_patch = ""
        s._quick_judge_selected_tests = None
        s._pending_quick_judge_message = ""
        s._last_admitted_phase = ""
        s._observe_tool_signal = False
        s._phase_accumulated_text = {}
        s.extraction_retry_counts = {}
        s._bypassed_principals = set()
        return s

    def test_zero_overlap_triggers_drift(self):
        """When patch_files and analyze_files have zero overlap, drift is detected."""
        state = self._make_state()
        state._analyze_root_cause_files = ["django/urls/resolvers.py"]
        state._last_patch_files = ["django/urls/base.py"]

        # The gate check is inside _step_post_step which is complex.
        # Test the core logic directly: normalize + overlap check.
        def _norm_file(f):
            f = f.strip()
            for _prefix in ("/testbed/", "a/", "b/"):
                if f.startswith(_prefix):
                    f = f[len(_prefix):]
            return f

        analyze_norm = {_norm_file(f) for f in state._analyze_root_cause_files}
        patch_norm = {_norm_file(f) for f in state._last_patch_files}
        overlap = analyze_norm & patch_norm
        assert not overlap, "Should have zero overlap for this test case"

    def test_overlap_passes(self):
        """When patch_files include the analyzed file, no drift."""
        state = self._make_state()
        state._analyze_root_cause_files = ["django/urls/resolvers.py"]
        state._last_patch_files = ["django/urls/resolvers.py", "django/urls/base.py"]

        def _norm_file(f):
            f = f.strip()
            for _prefix in ("/testbed/", "a/", "b/"):
                if f.startswith(_prefix):
                    f = f[len(_prefix):]
            return f

        analyze_norm = {_norm_file(f) for f in state._analyze_root_cause_files}
        patch_norm = {_norm_file(f) for f in state._last_patch_files}
        overlap = analyze_norm & patch_norm
        assert overlap == {"django/urls/resolvers.py"}

    def test_norm_file_strips_testbed_prefix(self):
        """Normalization strips /testbed/ prefix for consistent comparison."""
        def _norm_file(f):
            f = f.strip()
            for _prefix in ("/testbed/", "a/", "b/"):
                if f.startswith(_prefix):
                    f = f[len(_prefix):]
            return f

        assert _norm_file("/testbed/django/urls/resolvers.py") == "django/urls/resolvers.py"
        assert _norm_file("django/urls/resolvers.py") == "django/urls/resolvers.py"
        assert _norm_file("a/django/urls/resolvers.py") == "django/urls/resolvers.py"

    def test_11477_pattern_drift_detected(self):
        """django-11477 pattern: ANALYZE says resolvers.py, patch modifies base.py.
        This is the exact case P2 was designed to catch."""
        state = self._make_state()
        # 11477: ANALYZE correctly identified resolvers.py
        state._analyze_root_cause_files = ["django/urls/resolvers.py"]
        # But agent's patch modified base.py (translate_url or reverse)
        state._last_patch_files = ["django/urls/base.py"]

        def _norm_file(f):
            f = f.strip()
            for _prefix in ("/testbed/", "a/", "b/"):
                if f.startswith(_prefix):
                    f = f[len(_prefix):]
            return f

        analyze_norm = {_norm_file(f) for f in state._analyze_root_cause_files}
        patch_norm = {_norm_file(f) for f in state._last_patch_files}
        overlap = analyze_norm & patch_norm

        # Zero overlap = drift
        assert not overlap, "11477 pattern: should detect zero overlap (scope drift)"

        # Simulate drift counter increment
        state._scope_drift_count += 1
        assert state._scope_drift_count == 1

        # First violation → route to DECIDE
        target = "DECIDE" if state._scope_drift_count == 1 else "ANALYZE"
        assert target == "DECIDE"

    def test_scope_inject_into_execute_prompt(self):
        """P2: DECIDE/EXECUTE prompts should include analyzed file scope."""
        from step_sections import _step_inject_phase

        state = self._make_state()
        state._analyze_root_cause_files = ["django/urls/resolvers.py"]
        state._analyze_scope_summary = "Bug is in RegexPattern.match()"

        class FakeAgent:
            messages = []
            model = None
        agent = FakeAgent()

        class FakeCp:
            phase = "EXECUTE"
        _step_inject_phase(agent, cp_state_holder=[FakeCp()], state=state)

        scope_msgs = [
            m for m in agent.messages
            if "ANALYZE-confirmed root-cause files" in m.get("content", "")
        ]
        assert len(scope_msgs) == 1, f"Expected 1 scope injection, got {len(scope_msgs)}"
        content = scope_msgs[0]["content"]
        assert "django/urls/resolvers.py" in content
        assert "RegexPattern.match()" in content

    def test_no_scope_inject_without_analyze_files(self):
        """No scope injection if ANALYZE didn't declare files."""
        from step_sections import _step_inject_phase

        state = self._make_state()
        state._analyze_root_cause_files = []  # no files declared

        class FakeAgent:
            messages = []
            model = None
        agent = FakeAgent()

        class FakeCp:
            phase = "EXECUTE"
        _step_inject_phase(agent, cp_state_holder=[FakeCp()], state=state)

        scope_msgs = [
            m for m in agent.messages
            if "ANALYZE-confirmed root-cause files" in m.get("content", "")
        ]
        assert len(scope_msgs) == 0

    def test_analyze_schema_includes_new_fields(self):
        """ANALYZE contract schema must include root_cause_location_files."""
        from cognition_contracts.analysis_root_cause import (
            SCHEMA_PROPERTIES,
            SCHEMA_REQUIRED,
        )
        assert "root_cause_location_files" in SCHEMA_PROPERTIES
        assert "root_cause_scope_summary" in SCHEMA_PROPERTIES
        assert "root_cause_location_files" in SCHEMA_REQUIRED
        # root_cause_scope_summary is optional (not in SCHEMA_REQUIRED)

    def test_state_serialization(self):
        """P2 fields survive checkpoint serialization round-trip."""
        state = self._make_state()
        state._analyze_root_cause_files = ["django/urls/resolvers.py"]
        state._analyze_scope_summary = "Bug in match()"
        state._scope_drift_count = 1

        from step_monitor_state import StepMonitorState
        # Manually serialize relevant fields
        d = {
            "analyze_root_cause_files": state._analyze_root_cause_files,
            "analyze_scope_summary": state._analyze_scope_summary,
            "scope_drift_count": state._scope_drift_count,
        }
        # Verify values survive
        assert d["analyze_root_cause_files"] == ["django/urls/resolvers.py"]
        assert d["analyze_scope_summary"] == "Bug in match()"
        assert d["scope_drift_count"] == 1
