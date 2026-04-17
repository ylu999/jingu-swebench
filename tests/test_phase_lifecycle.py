"""
test_phase_lifecycle.py — Tests for L4 Phase Lifecycle.

Verifies:
1. PhaseResult construction from admitted records
2. route_from_phase_result: ANALYZE protocol-driven routing
3. route_from_phase_result: non-ANALYZE default advance
4. Edge cases: missing record, invalid strategy, empty fields
"""

import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mini-swe-agent"))


# ── PhaseResult Construction ─────────────────────────────────────────────────

class TestPhaseResultConstruction:

    def test_build_from_complete_analyze_record(self):
        from phase_lifecycle import build_phase_result_from_admission
        record = {
            "phase": "ANALYZE",
            "root_cause": "Bug in django/db/models/query.py:filter() line 423",
            "causal_chain": "QuerySet.filter() passes wrong lookup → SQL WHERE fails",
            "evidence_refs": ["django/db/models/query.py:423"],
            "alternative_hypotheses": [{"hypothesis": "ORM caching issue", "ruled_out": "no cache involved"}],
            "repair_strategy_type": "REGEX_FIX",
        }
        result = build_phase_result_from_admission("ANALYZE", record, "tool_submitted")
        assert result.completed is True
        assert result.phase == "ANALYZE"
        assert result.admission_source == "tool_submitted"
        assert result.routing is not None

    def test_build_from_none_record(self):
        from phase_lifecycle import build_phase_result_from_admission
        result = build_phase_result_from_admission("ANALYZE", None)
        assert result.completed is False
        assert result.routing is not None
        assert result.routing.retry_current is True

    def test_protocol_fields_tracking(self):
        from phase_lifecycle import build_phase_result_from_admission
        record = {
            "phase": "ANALYZE",
            "root_cause": "Bug in file.py",
            "repair_strategy_type": "REGEX_FIX",
            # Missing: causal_chain, evidence_refs, alternative_hypotheses
        }
        result = build_phase_result_from_admission("ANALYZE", record)
        assert "root_cause" in result.protocol_fields_present
        assert "repair_strategy_type" in result.protocol_fields_present


# ── ANALYZE Protocol-Driven Routing ──────────────────────────────────────────

class TestAnalyzeRouting:

    def test_regex_fix_routes_to_decide(self):
        from phase_lifecycle import PhaseResult, route_from_phase_result
        result = PhaseResult(
            phase="ANALYZE",
            completed=True,
            admitted_record={"repair_strategy_type": "REGEX_FIX"},
        )
        routing = route_from_phase_result(result)
        assert routing.next_phase == "DECIDE"
        assert routing.source == "protocol"
        assert routing.retry_current is False
        assert "REGEX_FIX" in routing.reason

    def test_dataflow_fix_routes_to_decide(self):
        from phase_lifecycle import PhaseResult, route_from_phase_result
        result = PhaseResult(
            phase="ANALYZE",
            completed=True,
            admitted_record={"repair_strategy_type": "DATAFLOW_FIX"},
        )
        routing = route_from_phase_result(result)
        assert routing.next_phase == "DECIDE"
        assert routing.source == "protocol"

    def test_case_insensitive_strategy(self):
        """Strategy values should work case-insensitively."""
        from phase_lifecycle import PhaseResult, route_from_phase_result
        result = PhaseResult(
            phase="ANALYZE",
            completed=True,
            admitted_record={"repair_strategy_type": "regex_fix"},  # lowercase
        )
        routing = route_from_phase_result(result)
        assert routing.next_phase == "DECIDE"
        assert routing.source == "protocol"

    def test_missing_strategy_retries(self):
        from phase_lifecycle import PhaseResult, route_from_phase_result
        result = PhaseResult(
            phase="ANALYZE",
            completed=True,
            admitted_record={"root_cause": "bug in X"},  # no strategy
        )
        routing = route_from_phase_result(result)
        assert routing.retry_current is True
        assert routing.source == "protocol"
        assert "repair_strategy_type" in routing.retry_hint

    def test_empty_strategy_retries(self):
        from phase_lifecycle import PhaseResult, route_from_phase_result
        result = PhaseResult(
            phase="ANALYZE",
            completed=True,
            admitted_record={"repair_strategy_type": ""},
        )
        routing = route_from_phase_result(result)
        assert routing.retry_current is True

    def test_invalid_strategy_retries(self):
        from phase_lifecycle import PhaseResult, route_from_phase_result
        result = PhaseResult(
            phase="ANALYZE",
            completed=True,
            admitted_record={"repair_strategy_type": "nonexistent_strategy_xyz"},
        )
        routing = route_from_phase_result(result)
        assert routing.retry_current is True
        assert "not valid" in routing.retry_hint

    def test_incomplete_phase_retries(self):
        from phase_lifecycle import PhaseResult, route_from_phase_result
        result = PhaseResult(phase="ANALYZE", completed=False)
        routing = route_from_phase_result(result)
        assert routing.retry_current is True
        assert routing.source == "incomplete_record"


# ── Non-ANALYZE Default Advance ──────────────────────────────────────────────

class TestNonAnalyzeRouting:

    def test_observe_default_advance(self):
        from phase_lifecycle import PhaseResult, route_from_phase_result
        result = PhaseResult(
            phase="OBSERVE",
            completed=True,
            admitted_record={"observations": "found relevant files"},
        )
        routing = route_from_phase_result(result)
        assert routing.next_phase == "ANALYZE"
        assert routing.source == "default_advance"

    def test_decide_default_advance(self):
        from phase_lifecycle import PhaseResult, route_from_phase_result
        result = PhaseResult(
            phase="DECIDE",
            completed=True,
            admitted_record={"decision": "fix approach A"},
        )
        routing = route_from_phase_result(result)
        assert routing.next_phase == "DESIGN"
        assert routing.source == "default_advance"

    def test_execute_default_advance(self):
        from phase_lifecycle import PhaseResult, route_from_phase_result
        result = PhaseResult(
            phase="EXECUTE",
            completed=True,
            admitted_record={"patch": "diff --git ..."},
        )
        routing = route_from_phase_result(result)
        assert routing.next_phase == "JUDGE"
        assert routing.source == "default_advance"

    def test_judge_terminal(self):
        from phase_lifecycle import PhaseResult, route_from_phase_result
        result = PhaseResult(
            phase="JUDGE",
            completed=True,
            admitted_record={"verdict": "pass"},
        )
        routing = route_from_phase_result(result)
        assert routing.next_phase is None  # terminal
        assert routing.source == "default_advance"


# ── Integration: route_from_phase_result matches _ADVANCE_TABLE ──────────────

class TestRoutingConsistency:

    def test_all_phases_have_routing(self):
        from phase_lifecycle import PhaseResult, route_from_phase_result, _DEFAULT_ADVANCE
        for phase, expected_next in _DEFAULT_ADVANCE.items():
            result = PhaseResult(
                phase=phase,
                completed=True,
                admitted_record={"repair_strategy_type": "REGEX_FIX"} if phase == "ANALYZE" else {"dummy": "value"},
            )
            routing = route_from_phase_result(result)
            if phase == "ANALYZE":
                # ANALYZE uses protocol routing → DECIDE
                assert routing.next_phase == "DECIDE"
            else:
                assert routing.next_phase == expected_next, (
                    f"Phase {phase}: expected {expected_next}, got {routing.next_phase}"
                )
