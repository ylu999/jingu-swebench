"""
test_retry_controller.py — Unit tests for p177 retry controller extensions.

Covers:
  1. build_retry_plan: STOP_NO_SIGNAL fires at threshold
  2. build_retry_plan: enforced-principal hint injection (ENV_LEAKAGE + PLAN_LOOP)
  3. build_retry_plan: standard CONTINUE / ADJUST / STOP_FAIL paths unaffected
  4. compute_steps_since_last_signal: counts correctly from traj messages
  5. extract_principal_violation_codes: detects violations from declaration dicts
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import pytest
from retry_controller import (
    build_retry_plan,
    classify_failure,
    NO_SIGNAL_THRESHOLD,
    ENFORCED_VIOLATION_CODES,
)
from run_with_jingu_gate import (
    compute_steps_since_last_signal,
    extract_principal_violation_codes,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

def make_jingu_body(
    exit_status="Submitted",
    ran_tests=True,
    last_passed=False,
    files_written=None,
):
    return {
        "exit_status": exit_status,
        "files_written": files_written or ["django/forms/widgets.py"],
        "test_results": {
            "ran_tests": ran_tests,
            "last_passed": last_passed,
            "excerpt": "FAILED (failures=2)",
        },
    }


def make_fp(files=None, lines_added=5, lines_removed=3):
    return {
        "files": files or ["django/forms/widgets.py"],
        "hunks": 1,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
    }


def _base_call(**overrides):
    """Call build_retry_plan with minimal valid defaults."""
    kwargs = dict(
        problem_statement="Test problem",
        patch_text="diff --git a/foo.py",
        jingu_body=make_jingu_body(),
        fail_to_pass_tests=["test_foo"],
        gate_admitted=True,
        gate_reason_codes=[],
        patch_fp=make_fp(),
    )
    kwargs.update(overrides)
    return build_retry_plan(**kwargs)


# ── 1. STOP_NO_SIGNAL ─────────────────────────────────────────────────────────

class TestStopNoSignal:
    def test_fires_at_threshold(self):
        plan = _base_call(steps_since_last_signal=NO_SIGNAL_THRESHOLD)
        assert plan.control_action == "STOP_NO_SIGNAL"

    def test_fires_above_threshold(self):
        plan = _base_call(steps_since_last_signal=NO_SIGNAL_THRESHOLD + 5)
        assert plan.control_action == "STOP_NO_SIGNAL"

    def test_does_not_fire_below_threshold(self):
        plan = _base_call(steps_since_last_signal=NO_SIGNAL_THRESHOLD - 1)
        assert plan.control_action != "STOP_NO_SIGNAL"

    def test_does_not_fire_at_zero(self):
        plan = _base_call(steps_since_last_signal=0)
        assert plan.control_action != "STOP_NO_SIGNAL"

    def test_prompt_mentions_streak(self):
        plan = _base_call(steps_since_last_signal=NO_SIGNAL_THRESHOLD)
        assert str(NO_SIGNAL_THRESHOLD) in plan.next_attempt_prompt

    def test_root_causes_mention_streak(self):
        plan = _base_call(steps_since_last_signal=20)
        assert any("no_signal" in rc for rc in plan.root_causes)

    def test_must_do_present(self):
        plan = _base_call(steps_since_last_signal=NO_SIGNAL_THRESHOLD)
        assert len(plan.must_do) > 0

    def test_principal_violations_empty_on_no_signal(self):
        # STOP_NO_SIGNAL exits early — principal_violations not computed
        plan = _base_call(steps_since_last_signal=NO_SIGNAL_THRESHOLD)
        assert plan.principal_violations == []


# ── 2. Enforced-principal hint injection ─────────────────────────────────────

class TestPrincipalViolationHints:
    def test_env_leakage_produces_adjust(self):
        plan = _base_call(principal_violation_codes=["ENV_LEAKAGE_HARDCODE_PATH"])
        assert plan.control_action == "ADJUST"

    def test_env_leakage_hint_in_prompt(self):
        plan = _base_call(principal_violation_codes=["ENV_LEAKAGE_HARDCODE_PATH"])
        assert "ENVIRONMENT ASSUMPTION" in plan.next_attempt_prompt or \
               "environment" in plan.next_attempt_prompt.lower()

    def test_plan_no_feedback_loop_produces_adjust(self):
        plan = _base_call(principal_violation_codes=["PLAN_NO_FEEDBACK_LOOP"])
        assert plan.control_action == "ADJUST"

    def test_plan_no_feedback_loop_hint_in_prompt(self):
        plan = _base_call(principal_violation_codes=["PLAN_NO_FEEDBACK_LOOP"])
        assert "PLANNING" in plan.next_attempt_prompt or \
               "feedback" in plan.next_attempt_prompt.lower()

    def test_violation_codes_recorded(self):
        plan = _base_call(principal_violation_codes=["ENV_LEAKAGE_HARDCODE_PATH"])
        assert "ENV_LEAKAGE_HARDCODE_PATH" in plan.principal_violations

    def test_non_enforced_code_ignored(self):
        # Declared-only principals → no hard ADJUST from violation alone
        plan = _base_call(
            principal_violation_codes=["SOME_DECLARED_ONLY_CODE"],
            jingu_body=make_jingu_body(ran_tests=False, files_written=[]),
        )
        # failure_type=exploration_loop → ADJUST from failure class, not from viol code
        assert "SOME_DECLARED_ONLY_CODE" not in plan.principal_violations

    def test_empty_violation_codes_no_effect(self):
        plan_no = _base_call(principal_violation_codes=None)
        plan_empty = _base_call(principal_violation_codes=[])
        assert plan_no.control_action == plan_empty.control_action

    def test_both_violations_both_in_prompt(self):
        plan = _base_call(principal_violation_codes=[
            "ENV_LEAKAGE_HARDCODE_PATH",
            "PLAN_NO_FEEDBACK_LOOP",
        ])
        assert len(plan.principal_violations) == 2


# ── 3. Standard control paths ─────────────────────────────────────────────────

class TestControlPaths:
    def test_no_effect_patch_is_adjust(self):
        plan = _base_call()  # tests ran, last_passed=False, files written → no_effect_patch
        assert plan.control_action == "ADJUST"

    def test_unknown_failure_is_continue(self):
        # Force unknown: tests passed (should be STOP_OK at caller), but simulate unknown
        # by using a jingu_body that doesn't match any specific failure type
        jb = make_jingu_body(ran_tests=True, last_passed=True)
        plan = _base_call(jingu_body=jb)
        # last_passed=True → no_effect_patch check doesn't fire → unknown → CONTINUE
        assert plan.control_action == "CONTINUE"

    def test_root_causes_contain_failure_type(self):
        plan = _base_call()
        assert any(rc.startswith("failure_type=") for rc in plan.root_causes)

    def test_hint_length_bounded(self):
        plan = _base_call(exec_feedback="x" * 1000)
        assert len(plan.next_attempt_prompt) <= 600


# ── 4. compute_steps_since_last_signal ───────────────────────────────────────

def _make_assistant_msg(tool_names: list[str]) -> dict:
    """Helper: assistant message with given tool calls in extra.actions."""
    return {
        "role": "assistant",
        "extra": {
            "actions": [{"tool": t, "input": {}} for t in tool_names],
        },
    }


class TestComputeStepsSinceLastSignal:
    def test_empty_traj(self):
        assert compute_steps_since_last_signal([]) == 0

    def test_no_assistant_messages(self):
        msgs = [{"role": "user", "content": "fix this"}]
        assert compute_steps_since_last_signal(msgs) == 0

    def test_last_step_has_write(self):
        msgs = [_make_assistant_msg(["read_file", "edit_file"])]
        assert compute_steps_since_last_signal(msgs) == 0

    def test_last_step_has_submit(self):
        msgs = [_make_assistant_msg(["submit"])]
        assert compute_steps_since_last_signal(msgs) == 0

    def test_one_step_no_signal(self):
        msgs = [_make_assistant_msg(["read_file", "open_file"])]
        assert compute_steps_since_last_signal(msgs) == 1

    def test_two_steps_no_signal(self):
        msgs = [
            _make_assistant_msg(["str_replace_editor"]),  # has signal — stops here
            _make_assistant_msg(["read_file"]),
            _make_assistant_msg(["open_file"]),
        ]
        assert compute_steps_since_last_signal(msgs) == 2

    def test_signal_in_middle_counts_trailing(self):
        msgs = [
            _make_assistant_msg(["read_file"]),
            _make_assistant_msg(["edit_file"]),   # signal here
            _make_assistant_msg(["read_file"]),
            _make_assistant_msg(["read_file"]),
        ]
        assert compute_steps_since_last_signal(msgs) == 2

    def test_all_signals(self):
        msgs = [
            _make_assistant_msg(["edit_file"]),
            _make_assistant_msg(["write_file"]),
        ]
        assert compute_steps_since_last_signal(msgs) == 0

    def test_threshold_detection(self):
        no_signal_steps = [_make_assistant_msg(["read_file"])] * NO_SIGNAL_THRESHOLD
        assert compute_steps_since_last_signal(no_signal_steps) >= NO_SIGNAL_THRESHOLD

    def test_skips_non_assistant_messages(self):
        msgs = [
            _make_assistant_msg(["edit_file"]),  # signal
            {"role": "tool", "content": "output"},
            {"role": "user", "content": "ok"},
            _make_assistant_msg(["read_file"]),  # no signal
        ]
        assert compute_steps_since_last_signal(msgs) == 1


# ── 5. extract_principal_violation_codes ─────────────────────────────────────

class TestExtractPrincipalViolationCodes:
    def test_none_decl_returns_empty(self):
        assert extract_principal_violation_codes(None) == []

    def test_no_relevant_principals(self):
        decl = {
            "principals_used": ["P_DEBUG_ROOT_CAUSE_ISOLATION"],
            "evidence": [{"type": "code", "content": "some code"}],
        }
        assert extract_principal_violation_codes(decl) == []

    def test_env_independence_no_env_evidence(self):
        decl = {
            "principals_used": ["P_DEBUG_ENV_INDEPENDENCE"],
            "evidence": [{"type": "runtime", "content": "ModuleNotFoundError: jingu-protocol"}],
        }
        codes = extract_principal_violation_codes(decl)
        assert "ENV_LEAKAGE_HARDCODE_PATH" in codes

    def test_env_independence_with_env_check_passes(self):
        decl = {
            "principals_used": ["P_DEBUG_ENV_INDEPENDENCE"],
            "evidence": [
                {"type": "runtime", "content": "preflight check: node_modules present"},
            ],
        }
        codes = extract_principal_violation_codes(decl)
        assert "ENV_LEAKAGE_HARDCODE_PATH" not in codes

    def test_env_independence_local_path_leakage(self):
        decl = {
            "principals_used": ["P_DEBUG_ENV_INDEPENDENCE"],
            "evidence": [
                {"type": "code", "content": "env check: /root/jingu-swebench/node_modules"},
            ],
        }
        codes = extract_principal_violation_codes(decl)
        assert "ENV_LEAKAGE_HARDCODE_PATH" in codes

    def test_plan_close_loop_no_feedback(self):
        decl = {
            "principals_used": ["P_PLAN_CLOSE_THE_LOOP"],
            "evidence": [{"type": "doc", "content": "strategy: improve the system"}],
        }
        codes = extract_principal_violation_codes(decl)
        assert "PLAN_NO_FEEDBACK_LOOP" in codes

    def test_plan_close_loop_with_verify_keyword(self):
        decl = {
            "principals_used": ["P_PLAN_CLOSE_THE_LOOP"],
            "evidence": [
                {"type": "runtime", "content": "verification: run pytest and confirm pass"},
            ],
        }
        codes = extract_principal_violation_codes(decl)
        assert "PLAN_NO_FEEDBACK_LOOP" not in codes

    def test_both_violations(self):
        decl = {
            "principals_used": ["P_DEBUG_ENV_INDEPENDENCE", "P_PLAN_CLOSE_THE_LOOP"],
            "evidence": [{"type": "code", "content": "some code only"}],
        }
        codes = extract_principal_violation_codes(decl)
        assert "ENV_LEAKAGE_HARDCODE_PATH" in codes
        assert "PLAN_NO_FEEDBACK_LOOP" in codes

    def test_decl_without_principals_field(self):
        decl = {"type": "debugging", "evidence": []}
        assert extract_principal_violation_codes(decl) == []

    def test_empty_evidence(self):
        decl = {
            "principals_used": ["P_DEBUG_ENV_INDEPENDENCE"],
            "evidence": [],
        }
        codes = extract_principal_violation_codes(decl)
        # No evidence → no env check → violation
        assert "ENV_LEAKAGE_HARDCODE_PATH" in codes
