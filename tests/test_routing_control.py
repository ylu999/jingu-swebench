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
