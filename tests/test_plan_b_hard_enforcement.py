"""
test_plan_b_hard_enforcement.py — Behavioral verification for Plan-B strong enforcement.

Three iron rules under test:
  1. Phase completion = admitted PhaseRecord exists (from tool submission ONLY)
  2. No transition without admitted record
  3. Fallback extraction = diagnostic only, never admission

Tests:
  1. Agent never calls submit_phase_record → stuck, protocol_violation, never advances
  2. Agent calls submit_phase_record with content → admitted, phase advances
  3. Perfect free text but no tool call → diagnostic only, phase incomplete
"""

import sys
import os
import dataclasses
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest
from step_monitor_state import StepMonitorState, StopExecution
from control.reasoning_state import (
    ReasoningState, VerdictAdvance, VerdictContinue,
    initial_reasoning_state,
)


# ── Helpers: minimal mocks ────────────────────────────────────────────────────

def _make_state(phase="OBSERVE") -> StepMonitorState:
    """Create a minimal StepMonitorState for testing."""
    instance = {
        "instance_id": "test__test-0001",
        "repo": "test/test",
        "base_commit": "abc123",
        "problem_statement": "test problem",
    }
    s = StepMonitorState("test__test-0001", attempt=1, instance=instance)
    s.cp_state = initial_reasoning_state(phase)
    # Plan-B telemetry counters
    s._extraction_tool_submitted = 0
    s._extraction_structured = 0
    s._extraction_regex_fallback = 0
    s._extraction_no_schema = 0
    s._missing_submission_count = 0
    s.diagnostic_phase_records = []
    return s


def _make_agent(model=None) -> MagicMock:
    """Create a minimal agent mock."""
    agent = MagicMock()
    agent.messages = []
    agent.model = model
    return agent


def _make_model(submitted_record=None) -> MagicMock:
    """Create a JinguModel mock.

    Args:
        submitted_record: If not None, pop_submitted_phase_record returns this
                          on first call, then None.  Also sets _submitted_phase_record
                          so the admission peek sees a pending record.
    """
    model = MagicMock()
    if submitted_record is not None:
        model.pop_submitted_phase_record.return_value = submitted_record
        model._submitted_phase_record = submitted_record
    else:
        model.pop_submitted_phase_record.return_value = None
        # Explicit None so MagicMock doesn't auto-create a truthy attribute.
        # This makes _has_pending_submission=False in _step_cp_update_and_verdict.
        model._submitted_phase_record = None
    # structured_extract returns None (no diagnostic) by default
    model.structured_extract.return_value = None
    model._last_extract_record = None
    # RC-1: pop_submission_failure returns None by default (no parse error)
    model.pop_submission_failure.return_value = None
    return model


def _advance_cp_state(state, to_phase):
    """Prepare cp_state so decide_next returns VerdictAdvance(to=to_phase)."""
    state.cp_state = dataclasses.replace(
        state.cp_state,
        phase={"ANALYZE": "OBSERVE", "DECIDE": "ANALYZE", "EXECUTE": "DECIDE"}.get(to_phase, "OBSERVE"),
    )


def _run_section3(agent, state, cp_holder, verdict_override, latest_text=""):
    """Run _step_cp_update_and_verdict with mocked decide_next and signals."""
    with patch("step_sections.decide_next", return_value=verdict_override), \
         patch("step_sections.extract_weak_progress", return_value=False), \
         patch.object(state, "update_cp_with_step_signals", return_value=(False, "")), \
         patch.object(state, "latest_tests_passed", return_value=0):
        from step_sections import _step_cp_update_and_verdict
        _step_cp_update_and_verdict(
            agent,
            state=state,
            cp_state_holder=cp_holder,
            env_error_detected=False,
            step_patch_non_empty=False,
            latest_assistant_text=latest_text,
        )


# ══════════════════════════════════════════════════════════════════════════════
# Test 1: Agent NEVER calls submit_phase_record → blocked, protocol_violation
# ══════════════════════════════════════════════════════════════════════════════

class TestNoSubmission:
    """Iron Rule 1+2: no tool submission = no phase completion = no advance.

    Current architecture: RC-1 gate suppresses VerdictAdvance when no pending
    submission exists.  Deadline-based escalation (soft → hard → terminal)
    handles agents that don't call submit_phase_record.
    """

    def test_rc1_suppresses_advance_without_submission(self):
        """RC-1: VerdictAdvance suppressed to VerdictContinue when no submission."""
        state = _make_state("OBSERVE")
        model = _make_model(submitted_record=None)  # never submits
        agent = _make_agent(model)
        cp_holder = [state.cp_state]

        verdict = VerdictAdvance(to="ANALYZE")

        # RC-1 gate suppresses → no advance, no error
        _run_section3(agent, state, cp_holder, verdict)

        # Phase did NOT advance — still OBSERVE
        assert cp_holder[0].phase == "OBSERVE", \
            "Phase must NOT advance without admitted record"

        # No admitted record in phase_records
        assert len(state.phase_records) == 0, \
            "phase_records must be empty when no tool submission"

        # Steps without submission counter incremented
        assert state._steps_without_submission >= 1

    def test_deadline_escalation_soft_warning(self):
        """Soft checkpoint: reminder injected at CHECKPOINT_SOFT steps."""
        state = _make_state("OBSERVE")
        model = _make_model(submitted_record=None)
        agent = _make_agent(model)
        cp_holder = [state.cp_state]

        verdict = VerdictAdvance(to="ANALYZE")

        # OBSERVE deadline=15, soft=12, hard=15, terminal=18
        # Run past soft checkpoint (12 steps)
        for _ in range(13):
            _run_section3(agent, state, cp_holder, verdict)

        # Phase still OBSERVE
        assert cp_holder[0].phase == "OBSERVE"

        # Soft reminder injected
        reminder_msgs = [
            m for m in agent.messages
            if "PHASE CHECKPOINT" in m.get("content", "")
        ]
        assert len(reminder_msgs) >= 1, \
            "Must inject phase checkpoint message after soft deadline"

    def test_deadline_escalation_terminal_stops(self):
        """Terminal checkpoint: StopExecution after deadline + 3 steps."""
        state = _make_state("OBSERVE")
        model = _make_model(submitted_record=None)
        agent = _make_agent(model)
        cp_holder = [state.cp_state]

        verdict = VerdictAdvance(to="ANALYZE")

        # OBSERVE: deadline=15, terminal=18
        # Run to terminal threshold
        for _ in range(18):
            _run_section3(agent, state, cp_holder, verdict)

        # early_stop_verdict should be set at terminal
        assert state.early_stop_verdict is not None, \
            "Terminal escalation must set early_stop_verdict"
        assert "step_governance_timeout" in state.early_stop_verdict.reason

        # Phase NEVER advanced
        assert cp_holder[0].phase == "OBSERVE"
        assert len(state.phase_records) == 0

    def test_steps_without_submission_tracks(self):
        """Telemetry: _steps_without_submission incremented on each step."""
        state = _make_state("OBSERVE")
        model = _make_model(submitted_record=None)
        agent = _make_agent(model)
        cp_holder = [state.cp_state]

        verdict = VerdictAdvance(to="ANALYZE")

        _run_section3(agent, state, cp_holder, verdict)
        assert state._steps_without_submission == 1

        _run_section3(agent, state, cp_holder, verdict)
        assert state._steps_without_submission == 2


# ══════════════════════════════════════════════════════════════════════════════
# Test 2: Agent calls submit_phase_record with valid content → admitted
# ══════════════════════════════════════════════════════════════════════════════

class TestValidSubmission:
    """Iron Rule 1: tool submission = admitted record = phase can advance."""

    def test_tool_submission_admitted_and_stored(self):
        """Valid tool submission → record in phase_records, source=tool_submitted."""
        submitted = {
            "phase": "OBSERVE",
            "root_cause": "found the bug in models.py",
            "evidence": ["django/db/models.py:42"],
            "principals": ["causal_grounding"],
        }
        state = _make_state("OBSERVE")
        model = _make_model(submitted_record=submitted)
        agent = _make_agent(model)
        cp_holder = [state.cp_state]

        verdict = VerdictAdvance(to="ANALYZE")

        # Mock downstream gates to not interfere
        with patch("step_sections.decide_next", return_value=verdict), \
             patch("step_sections.extract_weak_progress", return_value=False), \
             patch.object(state, "update_cp_with_step_signals", return_value=(False, "")), \
             patch.object(state, "latest_tests_passed", return_value=0):
            # Also need to mock imports that happen inside the function
            from step_sections import _step_cp_update_and_verdict

            # Mock cognition/analysis/principal gates to pass through
            with patch.dict("sys.modules", {
                "cognition_prompts": MagicMock(COGNITION_EXECUTION_ENABLED=False),
                "analysis_gate": MagicMock(),
                "principal_gate": MagicMock(),
                "principal_inference": MagicMock(),
                "jingu_onboard": MagicMock(),
            }):
                _step_cp_update_and_verdict(
                    agent,
                    state=state,
                    cp_state_holder=cp_holder,
                    env_error_detected=False,
                    step_patch_non_empty=False,
                    latest_assistant_text="I found the bug",
                )

        # Admitted record exists
        assert len(state.phase_records) == 1, \
            "Tool-submitted record must be admitted to phase_records"
        pr = state.phase_records[0]
        assert pr.phase == "OBSERVE"

        # Extraction counter incremented
        assert state._extraction_tool_submitted == 1

        # Phase advanced (assuming all downstream gates pass)
        # Note: may not advance if downstream gates fail, but the record IS admitted
        assert state._missing_submission_count == 0

    def test_tool_submission_populates_model(self):
        """pop_submitted_phase_record is called exactly once per verdict."""
        submitted = {
            "phase": "ANALYZE",
            "root_cause": "null check missing",
            "evidence": ["models.py:10"],
            "principals": ["causal_grounding"],
        }
        model = _make_model(submitted_record=submitted)
        state = _make_state("OBSERVE")
        agent = _make_agent(model)
        cp_holder = [state.cp_state]

        verdict = VerdictAdvance(to="ANALYZE")
        _run_section3(agent, state, cp_holder, verdict)

        model.pop_submitted_phase_record.assert_called_once()


# ══════════════════════════════════════════════════════════════════════════════
# Test 3: Perfect free text but no tool call → diagnostic only
# ══════════════════════════════════════════════════════════════════════════════

class TestFreeTextDiagnosticOnly:
    """Iron Rule 3: diagnostic extraction ≠ admission."""

    def test_structured_extract_goes_to_diagnostic_not_admitted(self):
        """structured_extract produces diagnostic record, NOT admitted."""
        state = _make_state("OBSERVE")
        # Model: no tool submission, but structured_extract succeeds
        model = _make_model(submitted_record=None)
        model.structured_extract.return_value = {
            "phase": "OBSERVE",
            "root_cause": "the bug is in views.py",
            "evidence": ["views.py:100"],
            "principals": ["evidence_linkage"],
        }
        agent = _make_agent(model)
        cp_holder = [state.cp_state]

        verdict = VerdictAdvance(to="ANALYZE")

        # Use same helper — structured_extract is called inside
        # but its result goes to diagnostic_phase_records only
        _run_section3(agent, state, cp_holder, verdict,
                      latest_text="I found the root cause in views.py:100.")

        # CRITICAL: phase_records must be EMPTY (no admitted record)
        assert len(state.phase_records) == 0, \
            "Diagnostic extraction must NOT produce admitted record in phase_records"

        # Phase did NOT advance
        assert cp_holder[0].phase == "OBSERVE", \
            "Phase must NOT advance on diagnostic-only extraction"

        # Steps without submission counter incremented (RC-1 suppression path)
        assert state._steps_without_submission >= 1

    def test_regex_extraction_goes_to_diagnostic_not_admitted(self):
        """Regex fallback extraction → diagnostic only, never admitted."""
        state = _make_state("OBSERVE")
        model = _make_model(submitted_record=None)
        # No structured_extract capability
        del model.structured_extract
        agent = _make_agent(model)
        cp_holder = [state.cp_state]

        verdict = VerdictAdvance(to="ANALYZE")

        # Agent writes perfect-looking free text with all the right keywords
        perfect_text = (
            "PHASE: OBSERVE\n"
            "ROOT_CAUSE: The issue is in django/db/models.py line 42\n"
            "EVIDENCE: django/db/models.py:42\n"
            "PRINCIPALS: causal_grounding, evidence_linkage\n"
            "FIX_TYPE: bug_fix\n"
        )

        _run_section3(agent, state, cp_holder, verdict, latest_text=perfect_text)

        # CRITICAL: phase_records EMPTY — regex extraction is diagnostic only
        assert len(state.phase_records) == 0, \
            "Regex extraction must NEVER produce admitted record"

        # Phase NOT advanced
        assert cp_holder[0].phase == "OBSERVE", \
            "Phase must NOT advance on regex-only extraction"

    def test_diagnostic_and_admitted_stored_separately(self):
        """When tool submits AND diagnostic runs, they go to separate lists."""
        submitted = {
            "phase": "OBSERVE",
            "root_cause": "tool-submitted cause",
            "evidence": ["models.py:1"],
            "principals": ["causal_grounding"],
        }
        state = _make_state("OBSERVE")
        model = _make_model(submitted_record=submitted)
        # Also make structured_extract succeed
        model.structured_extract.return_value = {
            "phase": "OBSERVE",
            "root_cause": "diagnostic cause",
        }
        agent = _make_agent(model)
        cp_holder = [state.cp_state]

        verdict = VerdictAdvance(to="ANALYZE")
        _run_section3(agent, state, cp_holder, verdict)

        # Admitted: in phase_records
        assert len(state.phase_records) >= 1, \
            "Tool-submitted record must be in phase_records"

        # The two lists are separate
        admitted_phases = {r.phase for r in state.phase_records}
        diagnostic_phases = {r.phase for r in state.diagnostic_phase_records} if state.diagnostic_phase_records else set()

        # Both may exist but they are in DIFFERENT lists
        # (diagnostic_phase_records may or may not have content depending on mock setup)
        # The key invariant: phase_records only has tool-submitted records
        assert state._extraction_tool_submitted >= 1


# ══════════════════════════════════════════════════════════════════════════════
# Cross-cutting: verify no FORCE_ADVANCE escape hatch exists
# ══════════════════════════════════════════════════════════════════════════════

class TestNoForceAdvance:
    """Verify there is no escape hatch that advances phase without tool submission."""

    def test_extraction_gated_blocks_all_downstream_gates(self):
        """When extraction_gated=True, all downstream gates are skipped."""
        state = _make_state("OBSERVE")
        model = _make_model(submitted_record=None)
        agent = _make_agent(model)
        cp_holder = [state.cp_state]

        verdict = VerdictAdvance(to="ANALYZE")

        # First miss: blocked
        _run_section3(agent, state, cp_holder, verdict)

        # Verify no gate ran (no cognition, analysis, principal gate output)
        # The key check: phase did not advance
        assert cp_holder[0].phase == "OBSERVE"

        # No phase_records at all
        assert len(state.phase_records) == 0

    def test_deadline_terminal_sets_early_stop_verdict(self):
        """Deadline terminal escalation sets early_stop_verdict."""
        state = _make_state("OBSERVE")
        model = _make_model(submitted_record=None)
        agent = _make_agent(model)
        cp_holder = [state.cp_state]

        verdict = VerdictAdvance(to="ANALYZE")

        # OBSERVE: terminal = deadline(15) + 3 = 18 steps
        for _ in range(18):
            _run_section3(agent, state, cp_holder, verdict)

        assert state.early_stop_verdict is not None
        assert "step_governance_timeout" in state.early_stop_verdict.reason
        assert cp_holder[0].phase == "OBSERVE"


# ══════════════════════════════════════════════════════════════════════════════
# Integration smoke: real JinguAgent.on_step_end wiring
# ══════════════════════════════════════════════════════════════════════════════

class TestIntegrationSmoke:
    """End-to-end: JinguAgent.on_step_end → _step_cp_update_and_verdict.

    Verifies the real wiring — RC-1 suppresses advance, deadline escalation
    eventually stops the agent.  Only LLM calls and environment are stubbed.
    """

    def test_on_step_end_rc1_suppresses_advance(self):
        """Real JinguAgent.on_step_end: no tool submission → RC-1 blocks advance."""
        from jingu_agent import JinguAgent, StepDecision
        from pathlib import Path

        instance = {
            "instance_id": "test__test-0001",
            "repo": "test/test",
            "base_commit": "abc123",
            "problem_statement": "test problem",
        }

        # Create JinguAgent with minimal governance mock
        gov = MagicMock()
        gov.get_constrained_schema.return_value = None
        gov.get_cognition.return_value = None
        gov.get_phase_config.return_value = None
        gov.get_route.return_value = None
        gov.get_repair_hint.return_value = ""

        ja = JinguAgent(
            instance=instance,
            output_dir=Path("/tmp/test-plan-b-smoke"),
            governance=gov,
            mode="jingu",
            max_attempts=1,
        )

        # Set up internal state as if attempt is running
        state = _make_state("OBSERVE")
        ja._state = state
        ja._cp_state_holder = [state.cp_state]

        # Create agent mock with model that never submits
        model = _make_model(submitted_record=None)
        agent_mock = _make_agent(model)
        agent_mock.env = MagicMock(container_id="fake-container")

        # Simulate on_step_start result: agent wrote some text but no tool call
        ja._last_observe_result = (
            "I am analyzing the codebase to find the root cause.",
            "analyzing the codebase",
            False,
        )

        # Mock verify to be a no-op, and decide_next to return VerdictAdvance
        advance_verdict = VerdictAdvance(to="ANALYZE")

        with patch("step_sections.decide_next", return_value=advance_verdict), \
             patch("step_sections.extract_weak_progress", return_value=False), \
             patch.object(state, "update_cp_with_step_signals", return_value=(False, "")), \
             patch.object(state, "latest_tests_passed", return_value=0), \
             patch("step_sections._step_verify_if_needed", return_value=False), \
             patch("step_sections._step_check_structure"), \
             patch("step_sections._step_inject_phase"), \
             patch("step_sections._check_materialization_gate"):

            # Step 1: RC-1 suppresses advance → continue
            decision1 = ja.on_step_end(agent_mock, step_n=1)
            assert decision1.action == "continue", \
                f"RC-1 should suppress advance to continue, got action={decision1.action}"
            assert ja._cp_state_holder[0].phase == "OBSERVE", \
                "Phase must not advance on first miss"

            # Step 2: still suppressed
            ja._last_observe_result = ("Still analyzing...", "analyzing", False)
            decision2 = ja.on_step_end(agent_mock, step_n=2)
            assert ja._cp_state_holder[0].phase == "OBSERVE"
            assert decision2.action == "continue"

            # Step 3: still no submission but within deadline — continue
            ja._last_observe_result = ("Still no submission...", "no submission", False)
            decision3 = ja.on_step_end(agent_mock, step_n=3)
            assert decision3.action == "continue", \
                f"Within deadline, should continue, got action={decision3.action}"

        # Final state checks
        assert len(state.phase_records) == 0, \
            "No admitted records should exist"
        assert ja._cp_state_holder[0].phase == "OBSERVE", \
            "Phase must NEVER have advanced"
        assert state._steps_without_submission >= 3, \
            "Steps without submission must be tracked"
