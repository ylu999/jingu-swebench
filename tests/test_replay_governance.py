"""Tests for Replay L1 Governance Scorer.

Tests the scoring logic with synthetic events/decisions — no S3 access needed.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "replay"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from scoring.governance import _compute_metrics, GovernanceMetrics


# ── Helpers ──────────────────────────────────────────────────────────────────

def _step_event(step_n: int, phase: str, **kwargs) -> dict:
    return {
        "step_n": step_n,
        "timestamp_ms": 1000.0 * step_n,
        "phase": phase,
        "gate_verdict": kwargs.get("gate_verdict"),
        "gate_reason": kwargs.get("gate_reason"),
        "cp_state_snapshot": kwargs.get("cp_state_snapshot", {
            "phase": phase, "step": step_n, "no_progress_steps": 0,
            "patch_first_write": False, "phase_records_count": kwargs.get("records", 0),
        }),
        "tool_calls_count": kwargs.get("tools", 1),
        "files_read": kwargs.get("files_read", []),
        "files_written": kwargs.get("files_written", []),
        "step_duration_ms": 5000.0,
        "patch_non_empty": kwargs.get("patch", False),
        "env_error": False,
    }


def _decision(decision_type: str, step_n: int, verdict: str, **kwargs) -> dict:
    return {
        "decision_type": decision_type,
        "step_n": step_n,
        "timestamp_ms": 1000.0 * step_n,
        "verdict": verdict,
        "rule_violated": kwargs.get("rule_violated"),
        "signals_evaluated": kwargs.get("signals"),
        "reason_text": kwargs.get("reason", ""),
        "phase_from": kwargs.get("phase_from"),
        "phase_to": kwargs.get("phase_to"),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 1. Basic phase sequence extraction
# ══════════════════════════════════════════════════════════════════════════════

class TestPhaseSequence:
    def test_simple_sequence(self):
        events = [
            _step_event(1, "OBSERVE"),
            _step_event(2, "OBSERVE"),
            _step_event(3, "ANALYZE"),
            _step_event(4, "EXECUTE"),
        ]
        m = _compute_metrics(events, [])
        assert m.phase_sequence == ["OBSERVE", "ANALYZE", "EXECUTE"]
        assert m.total_steps == 4
        assert m.steps_per_phase == {"OBSERVE": 2, "ANALYZE": 1, "EXECUTE": 1}

    def test_empty_events(self):
        m = _compute_metrics([], [])
        assert m.phase_sequence == []
        assert m.total_steps == 0


# ══════════════════════════════════════════════════════════════════════════════
# 2. Redirect and retry counting
# ══════════════════════════════════════════════════════════════════════════════

class TestRedirectRetry:
    def test_redirect_counted(self):
        decisions = [
            _decision("gate_redirect", 10, "redirect",
                       phase_from="ANALYZE", phase_to="OBSERVE",
                       reason="analysis_gate_rejected",
                       signals={"strategy": "complete_causal_chain",
                                "repair_hints": ["Strengthen root cause evidence"]}),
        ]
        m = _compute_metrics([], decisions)
        assert m.redirect_count == 1
        assert m.redirects[0]["from"] == "ANALYZE"
        assert m.redirects[0]["to"] == "OBSERVE"
        assert m.redirects[0]["strategy"] == "complete_causal_chain"

    def test_retry_specific(self):
        decisions = [
            _decision("gate_retry", 15, "retry",
                       phase_from="EXECUTE", phase_to="EXECUTE",
                       reason="execute_gate_rejected",
                       signals={"strategy": "fix_execution",
                                "repair_hints": ["Fix the patch to handle edge case"]}),
        ]
        m = _compute_metrics([], decisions)
        assert m.retry_count == 1
        assert m.specific_retry_count == 1
        assert m.generic_retry_count == 0
        assert m.generic_retry_ratio == 0.0

    def test_retry_generic(self):
        decisions = [
            _decision("gate_retry", 15, "retry",
                       phase_from="EXECUTE", phase_to="EXECUTE",
                       reason="gate_rejected",
                       signals={"strategy": None, "repair_hints": []}),
        ]
        m = _compute_metrics([], decisions)
        assert m.retry_count == 1
        assert m.generic_retry_count == 1
        assert m.generic_retry_ratio == 1.0

    def test_mixed_retries(self):
        decisions = [
            _decision("gate_retry", 10, "retry",
                       phase_from="EXECUTE", phase_to="EXECUTE",
                       reason="r1", signals={"repair_hints": ["Fix the null check"]}),
            _decision("gate_retry", 12, "retry",
                       phase_from="EXECUTE", phase_to="EXECUTE",
                       reason="r2", signals={"repair_hints": []}),
            _decision("gate_retry", 14, "retry",
                       phase_from="EXECUTE", phase_to="EXECUTE",
                       reason="r3", signals={"repair_hints": ["Add test coverage"]}),
        ]
        m = _compute_metrics([], decisions)
        assert m.retry_count == 3
        assert m.specific_retry_count == 2
        assert m.generic_retry_count == 1
        assert abs(m.generic_retry_ratio - 1 / 3) < 0.01


# ══════════════════════════════════════════════════════════════════════════════
# 3. Effective redirect detection
# ══════════════════════════════════════════════════════════════════════════════

class TestEffectiveRedirect:
    def test_effective_redirect_new_files(self):
        events = [
            _step_event(1, "OBSERVE", files_read=["models.py"]),
            _step_event(2, "ANALYZE", files_read=["models.py"]),
            # Redirect at step 3: ANALYZE → OBSERVE
            _step_event(4, "OBSERVE", files_read=["tests/test_model.py"]),  # new file!
            _step_event(5, "OBSERVE", files_read=["validators.py"]),
        ]
        decisions = [
            _decision("gate_redirect", 3, "redirect",
                       phase_from="ANALYZE", phase_to="OBSERVE",
                       reason="analysis_gate_rejected",
                       signals={"strategy": "gather_more", "repair_hints": ["Get more evidence"]}),
        ]
        m = _compute_metrics(events, decisions)
        assert m.redirect_count == 1
        assert m.effective_redirect_count == 1
        assert m.effective_redirect_rate == 1.0

    def test_ineffective_redirect_no_target_phase(self):
        events = [
            _step_event(1, "OBSERVE"),
            _step_event(2, "ANALYZE"),
            # Redirect at step 3: ANALYZE → OBSERVE, but agent stays in ANALYZE
            _step_event(4, "ANALYZE"),
            _step_event(5, "EXECUTE"),
        ]
        decisions = [
            _decision("gate_redirect", 3, "redirect",
                       phase_from="ANALYZE", phase_to="OBSERVE",
                       reason="rejected",
                       signals={"repair_hints": ["Go back"]}),
        ]
        m = _compute_metrics(events, decisions)
        assert m.redirect_count == 1
        assert m.effective_redirect_count == 0
        assert m.effective_redirect_rate == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# 4. Phase budget and advances
# ══════════════════════════════════════════════════════════════════════════════

class TestDecisionTypes:
    def test_phase_advance_counted(self):
        decisions = [
            _decision("phase_advance", 10, "advance", phase_from="OBSERVE", phase_to="ANALYZE"),
            _decision("phase_advance", 20, "advance", phase_from="ANALYZE", phase_to="EXECUTE"),
        ]
        m = _compute_metrics([], decisions)
        assert m.phase_advance_count == 2

    def test_phase_budget_exceeded(self):
        decisions = [
            _decision("gate_verdict", 15, "advance", reason="phase_budget_exceeded"),
        ]
        m = _compute_metrics([], decisions)
        assert m.phase_budget_exceeded_count == 1

    def test_tolerated_advance(self):
        decisions = [
            _decision("gate_verdict", 20, "tolerated_advance",
                       reason="tolerance_policy", signals={"tolerated_gate": "analysis_gate"}),
        ]
        m = _compute_metrics([], decisions)
        assert m.tolerated_advance_count == 1


# ══════════════════════════════════════════════════════════════════════════════
# 5. Composite score
# ══════════════════════════════════════════════════════════════════════════════

class TestCompositeScore:
    def test_good_run_no_redirects(self):
        """Good agent that doesn't need redirects gets partial credit."""
        events = [
            _step_event(1, "OBSERVE", records=0),
            _step_event(5, "ANALYZE", records=1),
            _step_event(10, "EXECUTE", records=3),
        ]
        m = _compute_metrics(events, [])
        # Phase discipline OK, no redirect needed, no retry needed, records present
        assert m.governance_score >= 0.5

    def test_redirect_effective_boosts_score(self):
        """Effective redirect should boost governance score."""
        events = [
            _step_event(1, "OBSERVE"),
            _step_event(2, "ANALYZE"),
            _step_event(4, "OBSERVE", files_read=["new_file.py"]),
            _step_event(5, "OBSERVE", records=2),
        ]
        decisions = [
            _decision("gate_redirect", 3, "redirect",
                       phase_from="ANALYZE", phase_to="OBSERVE",
                       reason="rejected",
                       signals={"strategy": "gather", "repair_hints": ["Get evidence"]}),
        ]
        m = _compute_metrics(events, decisions)
        assert m.effective_redirect_rate == 1.0
        # Effective redirect contributes to score
        assert m.governance_score >= 0.6

    def test_skip_analyze_penalized(self):
        """Skipping ANALYZE and going to EXECUTE should lower score."""
        events = [
            _step_event(1, "OBSERVE"),
            _step_event(2, "EXECUTE"),
        ]
        m = _compute_metrics(events, [])
        score_skip = m.governance_score

        events_good = [
            _step_event(1, "OBSERVE"),
            _step_event(2, "ANALYZE"),
            _step_event(3, "EXECUTE"),
        ]
        m_good = _compute_metrics(events_good, [])
        assert m_good.governance_score > score_skip


# ══════════════════════════════════════════════════════════════════════════════
# 6. Admitted record count
# ══════════════════════════════════════════════════════════════════════════════

class TestAdmittedRecords:
    def test_records_from_last_event(self):
        events = [
            _step_event(1, "OBSERVE", records=0),
            _step_event(10, "EXECUTE", records=5),
        ]
        m = _compute_metrics(events, [])
        assert m.admitted_record_count == 5
