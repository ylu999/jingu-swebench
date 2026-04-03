"""
test_p177_integration.py — Integration smoke tests for p177 retry controller.

Uses real traj files from results/ to verify the complete pipeline:
  traj_msgs → compute_steps_since_last_signal
            → extract_principal_violation_codes (from mock declaration)
            → build_retry_plan
            → control_action in expected range

Also constructs synthetic "exploration loop" trajs (no writes) to verify
STOP_NO_SIGNAL fires correctly, and "violation" trajs to verify hint injection.

Run:
  cd scripts && python -m pytest test_p177_integration.py -v
"""

import sys, os, json, glob
sys.path.insert(0, os.path.dirname(__file__))

import pytest
from pathlib import Path
from retry_controller import build_retry_plan, NO_SIGNAL_THRESHOLD
from run_with_jingu_gate import (
    compute_steps_since_last_signal,
    extract_principal_violation_codes,
)

RESULTS_DIR = Path(__file__).parent.parent / "results"
REAL_TRAJS = sorted(RESULTS_DIR.glob("**/*.traj.json"))[:10]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_read_step(cmd: str = "grep -r 'def foo' /testbed/") -> dict:
    """One assistant step with a read-only bash command."""
    import json as _json
    return {
        "role": "assistant",
        "tool_calls": [
            {"index": 0, "function": {"name": "bash", "arguments": _json.dumps({"command": cmd})},
             "id": "tid_0"}
        ],
        "extra": {"actions": [{"command": cmd, "tool_call_id": "tid_0"}]},
    }


def _make_write_step(path: str = "/testbed/django/fix.py") -> dict:
    """One assistant step with a file write bash command."""
    import json as _json
    cmd = f"cat > {path} << 'EOF'\n# fix\nEOF"
    return {
        "role": "assistant",
        "tool_calls": [
            {"index": 0, "function": {"name": "bash", "arguments": _json.dumps({"command": cmd})},
             "id": "tid_0"}
        ],
        "extra": {"actions": [{"command": cmd, "tool_call_id": "tid_0"}]},
    }


def _make_submit_step() -> dict:
    """One assistant step with submit sentinel."""
    import json as _json
    cmd = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat patch.txt"
    return {
        "role": "assistant",
        "tool_calls": [
            {"index": 0, "function": {"name": "bash", "arguments": _json.dumps({"command": cmd})},
             "id": "tid_0"}
        ],
        "extra": {"actions": [{"command": cmd, "tool_call_id": "tid_0"}]},
    }


def _base_jingu_body(ran_tests=True, last_passed=False, files_written=None):
    return {
        "exit_status": "Submitted",
        "files_written": files_written or ["django/forms/widgets.py"],
        "test_results": {"ran_tests": ran_tests, "last_passed": last_passed, "excerpt": "FAILED(2)"},
    }


def _retry_plan(msgs, decl=None, **kwargs):
    """Build retry plan from traj messages + optional declaration."""
    steps_since = compute_steps_since_last_signal(msgs)
    viol_codes = extract_principal_violation_codes(decl)
    return build_retry_plan(
        problem_statement="Fix the issue",
        patch_text="diff --git a/foo.py",
        jingu_body=_base_jingu_body(),
        fail_to_pass_tests=["test_foo"],
        gate_admitted=True,
        gate_reason_codes=[],
        steps_since_last_signal=steps_since,
        principal_violation_codes=viol_codes,
        **kwargs,
    )


# ── 1. Real traj: Submitted cases → steps_since_signal = 0 ───────────────────

@pytest.mark.skipif(not REAL_TRAJS, reason="no real traj files found")
class TestRealTrajSignalCounting:
    @pytest.mark.parametrize("traj_path", REAL_TRAJS[:5], ids=lambda p: p.stem)
    def test_submitted_traj_has_zero_steps_since_signal(self, traj_path):
        """All Submitted trajs: last step is submit sentinel → steps_since_signal = 0."""
        traj = json.loads(traj_path.read_text())
        exit_status = traj.get("info", {}).get("exit_status", "")
        msgs = traj.get("messages", [])
        if exit_status != "Submitted":
            pytest.skip(f"exit_status={exit_status}, not Submitted")
        steps = compute_steps_since_last_signal(msgs)
        assert steps == 0, (
            f"{traj_path.stem}: expected 0 steps since signal for Submitted traj, got {steps}"
        )

    @pytest.mark.parametrize("traj_path", REAL_TRAJS[:5], ids=lambda p: p.stem)
    def test_submitted_traj_control_action_not_stop_no_signal(self, traj_path):
        """Submitted trajs should not trigger STOP_NO_SIGNAL."""
        traj = json.loads(traj_path.read_text())
        exit_status = traj.get("info", {}).get("exit_status", "")
        msgs = traj.get("messages", [])
        if exit_status != "Submitted":
            pytest.skip(f"exit_status={exit_status}")
        plan = _retry_plan(msgs)
        assert plan.control_action != "STOP_NO_SIGNAL", (
            f"{traj_path.stem}: Submitted traj wrongly triggered STOP_NO_SIGNAL"
        )


# ── 2. Synthetic exploration loop → STOP_NO_SIGNAL ───────────────────────────

class TestSyntheticExplorationLoop:
    def test_exactly_threshold_triggers_stop(self):
        msgs = [_make_read_step()] * NO_SIGNAL_THRESHOLD
        plan = _retry_plan(msgs)
        assert plan.control_action == "STOP_NO_SIGNAL"

    def test_one_below_threshold_no_stop(self):
        msgs = [_make_read_step()] * (NO_SIGNAL_THRESHOLD - 1)
        plan = _retry_plan(msgs)
        assert plan.control_action != "STOP_NO_SIGNAL"

    def test_write_then_reads_below_threshold(self):
        """Write + N reads where N < threshold: not STOP_NO_SIGNAL."""
        msgs = [_make_write_step()] + [_make_read_step()] * (NO_SIGNAL_THRESHOLD - 1)
        plan = _retry_plan(msgs)
        assert plan.control_action != "STOP_NO_SIGNAL"

    def test_write_then_reads_at_threshold(self):
        """Write + threshold reads → STOP_NO_SIGNAL (only trailing reads counted)."""
        msgs = [_make_write_step()] + [_make_read_step()] * NO_SIGNAL_THRESHOLD
        plan = _retry_plan(msgs)
        assert plan.control_action == "STOP_NO_SIGNAL"

    def test_stop_prompt_mentions_streak(self):
        msgs = [_make_read_step()] * (NO_SIGNAL_THRESHOLD + 3)
        plan = _retry_plan(msgs)
        assert str(NO_SIGNAL_THRESHOLD + 3) in plan.next_attempt_prompt or \
               "signal" in plan.next_attempt_prompt.lower()

    def test_stop_must_do_not_empty(self):
        msgs = [_make_read_step()] * NO_SIGNAL_THRESHOLD
        plan = _retry_plan(msgs)
        assert len(plan.must_do) >= 1

    def test_submit_then_reads_resets_counter(self):
        """Submit then reads: counter resets from submit."""
        msgs = [_make_read_step()] * 10 + [_make_submit_step()] + [_make_read_step()] * 5
        steps = compute_steps_since_last_signal(msgs)
        assert steps == 5


# ── 3. Synthetic principal violation case ────────────────────────────────────

class TestSyntheticPrincipalViolations:
    def _env_violation_decl(self):
        return {
            "principals_used": ["P_DEBUG_ENV_INDEPENDENCE"],
            "evidence": [{"type": "runtime", "content": "ModuleNotFoundError: jingu-protocol"}],
        }

    def _plan_violation_decl(self):
        return {
            "principals_used": ["P_PLAN_CLOSE_THE_LOOP"],
            "evidence": [{"type": "doc", "content": "strategy: improve code quality"}],
        }

    def _clean_decl(self):
        return {
            "principals_used": ["P_DEBUG_ENV_INDEPENDENCE"],
            "evidence": [{"type": "runtime", "content": "preflight check: npm install completed successfully"}],
        }

    def test_env_violation_produces_adjust(self):
        msgs = [_make_write_step(), _make_submit_step()]
        plan = _retry_plan(msgs, decl=self._env_violation_decl())
        assert plan.control_action == "ADJUST"

    def test_env_violation_hint_in_prompt(self):
        msgs = [_make_write_step(), _make_submit_step()]
        plan = _retry_plan(msgs, decl=self._env_violation_decl())
        assert "ENV" in plan.next_attempt_prompt.upper() or \
               "environment" in plan.next_attempt_prompt.lower()

    def test_plan_violation_produces_adjust(self):
        msgs = [_make_write_step(), _make_submit_step()]
        plan = _retry_plan(msgs, decl=self._plan_violation_decl())
        assert plan.control_action == "ADJUST"

    def test_clean_decl_no_violation_code(self):
        msgs = [_make_write_step(), _make_submit_step()]
        plan = _retry_plan(msgs, decl=self._clean_decl())
        assert "ENV_LEAKAGE_HARDCODE_PATH" not in plan.principal_violations

    def test_no_decl_no_violation(self):
        msgs = [_make_write_step(), _make_submit_step()]
        plan = _retry_plan(msgs, decl=None)
        assert plan.principal_violations == []


# ── 4. Priority: STOP_NO_SIGNAL > principal violations ────────────────────────

class TestDecisionPriority:
    def test_no_signal_beats_principal_violation(self):
        """STOP_NO_SIGNAL fires even if there's also a principal violation."""
        msgs = [_make_read_step()] * NO_SIGNAL_THRESHOLD
        decl = {
            "principals_used": ["P_DEBUG_ENV_INDEPENDENCE"],
            "evidence": [{"type": "runtime", "content": "module not found"}],
        }
        plan = _retry_plan(msgs, decl=decl)
        # P7 no-signal wins over enforced violation
        assert plan.control_action == "STOP_NO_SIGNAL"

    def test_normal_flow_violation_then_adjust(self):
        """With signal + violation: ADJUST (no STOP_NO_SIGNAL)."""
        msgs = [_make_write_step(), _make_submit_step()]
        decl = {
            "principals_used": ["P_PLAN_CLOSE_THE_LOOP"],
            "evidence": [{"type": "doc", "content": "improve quality"}],
        }
        plan = _retry_plan(msgs, decl=decl)
        assert plan.control_action == "ADJUST"
        assert plan.control_action != "STOP_NO_SIGNAL"


# ── 5. False positive / noise resistance ─────────────────────────────────────

class TestFalsePositiveResistance:
    def test_grep_with_cat_in_pattern_is_not_write(self):
        """grep containing 'cat' is not a write signal."""
        import json as _j
        cmd = "grep -n 'category' /testbed/file.py"
        msg = {
            "role": "assistant",
            "tool_calls": [{"index": 0, "function": {"name": "bash",
                "arguments": _j.dumps({"command": cmd})}, "id": "t0"}],
            "extra": {"actions": [{"command": cmd, "tool_call_id": "t0"}]},
        }
        # 'cat' appears as substring in 'category' — should NOT match 'cat >'
        assert compute_steps_since_last_signal([msg]) == 1

    def test_read_cat_file_is_not_signal(self):
        """cat /testbed/file.py (read) is not a write signal."""
        import json as _j
        cmd = "cat /testbed/django/forms/widgets.py"
        msg = {
            "role": "assistant",
            "tool_calls": [{"index": 0, "function": {"name": "bash",
                "arguments": _j.dumps({"command": cmd})}, "id": "t0"}],
            "extra": {"actions": [{"command": cmd, "tool_call_id": "t0"}]},
        }
        assert compute_steps_since_last_signal([msg]) == 1

    def test_tee_write_is_signal(self):
        """tee is a write signal (used for file creation with tee)."""
        import json as _j
        cmd = "echo 'fix code' | tee /testbed/fix.py"
        msg = {
            "role": "assistant",
            "tool_calls": [{"index": 0, "function": {"name": "bash",
                "arguments": _j.dumps({"command": cmd})}, "id": "t0"}],
            "extra": {"actions": [{"command": cmd, "tool_call_id": "t0"}]},
        }
        assert compute_steps_since_last_signal([msg]) == 0

    def test_verify_keyword_in_non_feedback_context(self):
        """'verify' in evidence is a feedback keyword — should prevent PLAN violation."""
        decl = {
            "principals_used": ["P_PLAN_CLOSE_THE_LOOP"],
            "evidence": [
                {"type": "runtime", "content": "verify fix works by running test suite"},
            ],
        }
        codes = extract_principal_violation_codes(decl)
        assert "PLAN_NO_FEEDBACK_LOOP" not in codes
