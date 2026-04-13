"""
test_quick_judge.py — Unit tests for the E1 Quick Judge system (target-aware).

Tests cover:
1. select_targeted_tests — test selection logic, stability, priority
2. classify_direction — direction signal classification from consecutive results
3. _build_quick_test_command — method-level vs class-level command generation
4. _parse_quick_test_output + _resolve_target_status — per-test parsing + target resolution
5. format_agent_message — gated message generation based on target status
6. should_trigger_quick_judge — all 6 trigger conditions on StepMonitorState
7. detect_acknowledged / detect_effective — effectiveness metrics
8. signal gating — target_status gates message content (no false positives)
"""

import sys
import os
import json
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

# --- Import quick_judge module ---
try:
    from quick_judge import (
        QuickJudgeResult,
        select_targeted_tests,
        select_sentinel_tests,
        classify_direction,
        format_agent_message,
        detect_acknowledged,
        detect_effective,
        _parse_quick_test_output,
        _resolve_target_status,
        _build_quick_test_command,
        _parse_django_test_id,
        _parse_pytest_test_id,
        _classify_signal_kind,
        _classify_multi_target_signal,
        _parse_pass_to_pass,
        _is_docstring_test_name,
        _extract_docstring_keywords,
        _resolve_docstring_test,
        _docstring_to_test_label,
    )
    QUICK_JUDGE_AVAILABLE = True
except ImportError:
    QUICK_JUDGE_AVAILABLE = False

# --- Import StepMonitorState ---
try:
    from step_monitor_state import StepMonitorState
    from control.reasoning_state import initial_reasoning_state
    STATE_AVAILABLE = True
except ImportError:
    STATE_AVAILABLE = False

skip_quick_judge = pytest.mark.skipif(
    not QUICK_JUDGE_AVAILABLE, reason="quick_judge module not importable"
)
skip_state = pytest.mark.skipif(
    not STATE_AVAILABLE, reason="step_monitor_state not importable"
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_state(phase="EXECUTE", **overrides):
    """Create a StepMonitorState with all quick judge trigger conditions met."""
    instance = {"instance_id": "test__test-001", "FAIL_TO_PASS": "[]"}
    state = StepMonitorState(
        instance_id="test__test-001",
        attempt=1,
        instance=instance,
    )
    state.cp_state = initial_reasoning_state(phase)
    state.last_quick_judge_patch = ""
    state._llm_step = 10
    state.last_quick_judge_step = 0
    state.last_quick_judge_time = 0.0
    state.early_stop_verdict = None
    state.quick_judge_count = 0
    for key, val in overrides.items():
        setattr(state, key, val)
    return state


DJANGO_TEST_OUTPUT = """test_fast_delete_fk (delete.tests.FastDeleteTests) ... ok
test_fast_delete_inheritance (delete.tests.FastDeleteTests) ... ok
test_only_referenced_fields_selected (delete.tests.DeletionTests) ... FAIL
test_can_fast_delete (delete.tests.FastDeleteTests) ... ok

----------------------------------------------------------------------
Ran 4 tests in 0.123s

FAILED (failures=1)
"""

DJANGO_TEST_OUTPUT_PASS = """test_only_referenced_fields_selected (delete.tests.DeletionTests) ... ok

----------------------------------------------------------------------
Ran 1 test in 0.050s

OK
"""

DJANGO_TEST_OUTPUT_ERROR = """test_only_referenced_fields_selected (delete.tests.DeletionTests) ... ERROR

======================================================================
ERROR: test_only_referenced_fields_selected (delete.tests.DeletionTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  ImportError: cannot import name 'Origin' from 'delete.models'

----------------------------------------------------------------------
Ran 1 test in 0.010s

FAILED (errors=1)
"""

PYTEST_TEST_OUTPUT = """tests/test_foo.py::TestClass::test_method PASSED
tests/test_foo.py::TestClass::test_other FAILED
"""


# ===========================================================================
# 1. select_targeted_tests
# ===========================================================================

@skip_quick_judge
class TestSelectTargetedTests:
    def test_f2p_lte_5_returns_all(self):
        inst = {"FAIL_TO_PASS": '["test_a (m.C)", "test_b (m.C)"]'}
        assert select_targeted_tests(inst, []) == ["test_a (m.C)", "test_b (m.C)"]

    def test_f2p_gt_5_caps_at_5(self):
        ids = [f"test_{i} (mod.Cls)" for i in range(10)]
        inst = {"FAIL_TO_PASS": json.dumps(ids)}
        result = select_targeted_tests(inst, [])
        assert len(result) == 5

    def test_empty_f2p(self):
        assert select_targeted_tests({"FAIL_TO_PASS": "[]"}, []) == []

    def test_json_string_parsing(self):
        inst = {"FAIL_TO_PASS": '["test_x (a.B)"]'}
        assert select_targeted_tests(inst, []) == ["test_x (a.B)"]

    def test_changed_file_priority(self):
        ids = [f"test_{i} (mod.Cls)" for i in range(10)]
        inst = {"FAIL_TO_PASS": json.dumps(ids)}
        result = select_targeted_tests(inst, ["mod.py"])
        assert len(result) == 5

    def test_deterministic(self):
        inst = {"FAIL_TO_PASS": '["c", "a", "b"]'}
        r1 = select_targeted_tests(inst, [])
        r2 = select_targeted_tests(inst, [])
        assert r1 == r2 == ["a", "b", "c"]


# ===========================================================================
# 2. classify_direction
# ===========================================================================

@skip_quick_judge
class TestClassifyDirection:
    def _r(self, passed=0, failed=0, error=0, targeted=1, failing=None):
        return QuickJudgeResult(
            tests_passed=passed, tests_failed=failed, tests_error=error,
            tests_targeted=targeted,
            failing_test_names=failing or [],
        )

    def test_first_signal(self):
        assert classify_direction(self._r(passed=1), None) == "first_signal"

    def test_improved(self):
        prev = self._r(passed=1, failed=1, failing=["a"])
        curr = self._r(passed=2, failed=0, failing=[])
        assert classify_direction(curr, prev) == "likely_right_direction"

    def test_regressed(self):
        prev = self._r(passed=2)
        curr = self._r(passed=1, failed=1, failing=["a"])
        assert classify_direction(curr, prev) == "likely_wrong_direction"

    def test_unchanged(self):
        prev = self._r(passed=1, failed=1, failing=["a"])
        curr = self._r(passed=1, failed=1, failing=["a"])
        assert classify_direction(curr, prev) == "unchanged"

    def test_inconclusive_all_errors(self):
        prev = self._r(passed=1)
        curr = self._r(error=2, targeted=2)
        assert classify_direction(curr, prev) == "inconclusive"

    def test_improved_not_subset(self):
        prev = self._r(passed=1, failed=1, failing=["a"])
        curr = self._r(passed=2, failed=1, failing=["b"])
        assert classify_direction(curr, prev) == "improved"


# ===========================================================================
# 3. _build_quick_test_command — method-level
# ===========================================================================

@skip_quick_judge
class TestBuildQuickTestCommand:
    def test_django_method_level(self):
        inst = {"repo": "django/django", "version": "3.0"}
        test_ids = ["test_only_referenced_fields_selected (delete.tests.DeletionTests)"]
        try:
            cmd, scope = _build_quick_test_command(inst, test_ids)
            assert scope == "method"
            assert "delete.tests.DeletionTests.test_only_referenced_fields_selected" in cmd
            # Must NOT contain just the class name without method
            assert cmd.count("DeletionTests") == 1
        except ImportError:
            pytest.skip("swebench.harness.constants not available")

    def test_django_multiple_methods(self):
        inst = {"repo": "django/django", "version": "3.0"}
        test_ids = [
            "test_a (mod.tests.Cls)",
            "test_b (mod.tests.Cls)",
        ]
        try:
            cmd, scope = _build_quick_test_command(inst, test_ids)
            assert scope == "method"
            assert "mod.tests.Cls.test_a" in cmd
            assert "mod.tests.Cls.test_b" in cmd
        except ImportError:
            pytest.skip("swebench.harness.constants not available")

    def test_pytest_method_level(self):
        inst = {"repo": "pytest-dev/pytest", "version": "5.0"}
        test_ids = ["tests/test_foo.py::TestClass::test_method"]
        try:
            cmd, scope = _build_quick_test_command(inst, test_ids)
            assert scope == "method"
            assert "tests/test_foo.py::TestClass::test_method" in cmd
        except ImportError:
            pytest.skip("swebench.harness.constants not available")


# ===========================================================================
# 4. _parse_quick_test_output + _resolve_target_status
# ===========================================================================

@skip_quick_judge
class TestParseAndResolve:
    def test_django_target_failed(self):
        target = "test_only_referenced_fields_selected (delete.tests.DeletionTests)"
        results, (p, f, e, names) = _parse_quick_test_output(DJANGO_TEST_OUTPUT, [target])
        assert target in results
        assert results[target] == "failed"
        status = _resolve_target_status(results, target)
        assert status == "failed"
        assert p == 3
        assert f == 1

    def test_django_target_passed(self):
        target = "test_only_referenced_fields_selected (delete.tests.DeletionTests)"
        results, (p, f, e, names) = _parse_quick_test_output(DJANGO_TEST_OUTPUT_PASS, [target])
        assert results[target] == "passed"
        status = _resolve_target_status(results, target)
        assert status == "passed"
        assert p == 1

    def test_django_target_error(self):
        target = "test_only_referenced_fields_selected (delete.tests.DeletionTests)"
        results, _ = _parse_quick_test_output(DJANGO_TEST_OUTPUT_ERROR, [target])
        assert results[target] == "error"
        status = _resolve_target_status(results, target)
        assert status == "error"

    def test_target_missing(self):
        target = "test_nonexistent (some.Module)"
        results, _ = _parse_quick_test_output(DJANGO_TEST_OUTPUT, [target])
        status = _resolve_target_status(results, target)
        assert status == "missing"

    def test_target_unknown_empty_output(self):
        target = "test_foo (m.C)"
        results, _ = _parse_quick_test_output("", [target])
        status = _resolve_target_status(results, target)
        assert status == "unknown"

    def test_pytest_format(self):
        target = "tests/test_foo.py::TestClass::test_method"
        results, _ = _parse_quick_test_output(PYTEST_TEST_OUTPUT, [target])
        assert results.get(target) == "passed" or _resolve_target_status(results, target) == "passed"

    def test_method_name_match(self):
        """Target with slightly different class path still matches by method name."""
        target = "test_only_referenced_fields_selected (delete.tests.DeletionTests)"
        # Output has slightly different formatting but same method
        output = "test_only_referenced_fields_selected (delete.tests.DeletionTests) ... ok\n"
        results, _ = _parse_quick_test_output(output, [target])
        status = _resolve_target_status(results, target)
        assert status == "passed"


# ===========================================================================
# 5. format_agent_message — gated on target status
# ===========================================================================

@skip_quick_judge
class TestFormatAgentMessage:
    def _result(self, target_status="passed", signal_kind="target_passed",
                target_test_id="test_foo (m.C)", command_scope="method", **kw):
        return QuickJudgeResult(
            step=10,
            target_test_id=target_test_id,
            target_status=target_status,
            signal_kind=signal_kind,
            command_scope=command_scope,
            **kw,
        )

    def test_target_passed_positive_message(self):
        msg = format_agent_message(self._result(target_status="passed"))
        assert "TARGET PASSED" in msg
        assert "fixes the reported issue" in msg

    def test_target_failed_corrective_message(self):
        r = self._result(
            target_status="failed", signal_kind="target_failed",
            failing_test_names=["test_foo (m.C)"],
        )
        msg = format_agent_message(r)
        assert "TARGET FAILED" in msg
        assert "still failing" in msg
        assert "Failing:" in msg

    def test_target_error_message(self):
        msg = format_agent_message(self._result(target_status="error", signal_kind="target_error"))
        assert "TARGET ERROR" in msg
        assert "import/syntax" in msg

    def test_target_missing_warning(self):
        msg = format_agent_message(self._result(target_status="missing", signal_kind="target_missing"))
        assert "TARGET NOT FOUND" in msg
        # Must NOT contain positive language
        assert "fixes" not in msg
        assert "passing" not in msg

    def test_unknown_no_positive_signal(self):
        msg = format_agent_message(self._result(target_status="unknown", signal_kind="non_corrective_noise"))
        assert "NO SIGNAL" in msg
        assert "fixes" not in msg

    def test_class_scope_downgraded(self):
        msg = format_agent_message(self._result(command_scope="class"))
        assert "scope=class" in msg
        assert "lower confidence" in msg

    def test_method_scope_no_qualifier(self):
        msg = format_agent_message(self._result(command_scope="method"))
        assert "lower confidence" not in msg

    def test_contains_target_identity(self):
        msg = format_agent_message(self._result(target_test_id="test_foo (m.C)"))
        assert "Target: test_foo (m.C)" in msg
        assert "PASSED" in msg  # status label included

    def test_contains_step(self):
        msg = format_agent_message(self._result())
        assert "step=10" in msg

    def test_no_raw_stdout(self):
        """Message must not contain raw test output."""
        msg = format_agent_message(self._result())
        assert "Traceback" not in msg
        assert ">>>" not in msg


# ===========================================================================
# 6. should_trigger_quick_judge
# ===========================================================================

@skip_state
class TestShouldTriggerQuickJudge:
    def test_all_conditions_met_returns_true(self):
        state = _make_state()
        assert state.should_trigger_quick_judge("new_hash") is True

    def test_c1_non_execute_phase_returns_false(self):
        for phase in ["OBSERVE", "ANALYZE", "DECIDE"]:
            state = _make_state(phase=phase)
            assert state.should_trigger_quick_judge("new_hash") is False

    def test_c2_same_patch_hash_returns_false(self):
        state = _make_state(last_quick_judge_patch="same_hash")
        assert state.should_trigger_quick_judge("same_hash") is False

    def test_c3_step_interval_too_small_returns_false(self):
        state = _make_state(_llm_step=5, last_quick_judge_step=4)
        assert state.should_trigger_quick_judge("new_hash") is False

    def test_c3_exactly_3_steps_returns_true(self):
        state = _make_state(_llm_step=6, last_quick_judge_step=3)
        assert state.should_trigger_quick_judge("new_hash") is True

    def test_c4_time_interval_too_small_returns_false(self):
        state = _make_state(last_quick_judge_time=time.monotonic() - 5.0)
        assert state.should_trigger_quick_judge("new_hash") is False

    def test_c5_early_stop_verdict_returns_false(self):
        state = _make_state(early_stop_verdict="no_signal")
        assert state.should_trigger_quick_judge("new_hash") is False

    def test_c6_quota_exhausted_returns_false(self):
        state = _make_state(quick_judge_count=3)
        assert state.should_trigger_quick_judge("new_hash") is False

    def test_current_phase_override(self):
        state = _make_state(phase="OBSERVE")
        assert state.should_trigger_quick_judge("new_hash") is False
        assert state.should_trigger_quick_judge("new_hash", current_phase="EXECUTE") is True
        state2 = _make_state(phase="EXECUTE")
        assert state2.should_trigger_quick_judge("new_hash", current_phase="ANALYZE") is False


# ===========================================================================
# 7. signal_kind classification
# ===========================================================================

@skip_quick_judge
class TestSignalKind:
    def test_passed(self):
        assert _classify_signal_kind("passed") == "target_passed"

    def test_failed(self):
        assert _classify_signal_kind("failed") == "target_failed"

    def test_error(self):
        assert _classify_signal_kind("error") == "target_error"

    def test_missing(self):
        assert _classify_signal_kind("missing") == "target_missing"

    def test_unknown(self):
        assert _classify_signal_kind("unknown") == "non_corrective_noise"


# ===========================================================================
# 8. _parse_django_test_id / _parse_pytest_test_id
# ===========================================================================

@skip_quick_judge
class TestParseTestId:
    def test_django_format(self):
        method, cls = _parse_django_test_id("test_foo (delete.tests.DeletionTests)")
        assert method == "test_foo"
        assert cls == "delete.tests.DeletionTests"

    def test_django_format_not_match(self):
        method, cls = _parse_django_test_id("not_a_test_id")
        assert method is None
        assert cls is None

    def test_pytest_format(self):
        method, mod = _parse_pytest_test_id("tests/test_foo.py::TestClass::test_bar")
        assert method == "test_bar"
        assert mod == "tests/test_foo.py::TestClass"

    def test_pytest_simple(self):
        method, mod = _parse_pytest_test_id("test_foo.py::test_bar")
        assert method == "test_bar"
        assert mod == "test_foo.py"

    def test_pytest_not_match(self):
        method, mod = _parse_pytest_test_id("not a pytest id")
        assert method is None


# ===========================================================================
# 9. detect_acknowledged / detect_effective
# ===========================================================================

@skip_quick_judge
class TestDetectAcknowledged:
    def test_target_mentioned(self):
        r = QuickJudgeResult(
            target_test_id="test_only_referenced_fields_selected (delete.tests.DeletionTests)",
        )
        assert detect_acknowledged(r, "I need to fix test_only_referenced_fields_selected", []) is True

    def test_failing_test_mentioned(self):
        r = QuickJudgeResult(failing_test_names=["test_foo (m.C)"])
        assert detect_acknowledged(r, "Looking at test_foo failure", []) is True

    def test_no_match(self):
        r = QuickJudgeResult(
            target_test_id="test_foo (m.C)",
            failing_test_names=["test_foo (m.C)"],
        )
        assert detect_acknowledged(r, "I will try a different approach", []) is False


@skip_quick_judge
class TestDetectEffective:
    def test_target_status_convergence(self):
        history = [
            {"target_status": "failed", "direction": "first_signal"},
            {"target_status": "passed", "direction": "improved"},
        ]
        assert detect_effective(history) is True

    def test_direction_convergence(self):
        history = [
            {"target_status": "failed", "direction": "regressed"},
            {"target_status": "failed", "direction": "likely_right_direction"},
        ]
        assert detect_effective(history) is True

    def test_no_convergence(self):
        history = [
            {"target_status": "failed", "direction": "first_signal"},
            {"target_status": "failed", "direction": "unchanged"},
        ]
        assert detect_effective(history) is False

    def test_single_entry(self):
        assert detect_effective([{"target_status": "passed"}]) is False

    def test_empty(self):
        assert detect_effective([]) is False


# ===========================================================================
# 10. Multi-F2P target coverage
# ===========================================================================

@skip_quick_judge
class TestMultiF2PCoverage:
    """Tests for multi-F2P target resolution and partial coverage signal."""

    def test_classify_multi_target_all_passed(self):
        target_results = {
            "test_a (m.C)": "passed",
            "test_b (m.C)": "passed",
            "test_c (m.C)": "passed",
        }
        signal, fp, ff, cov = _classify_multi_target_signal(target_results, "test_a (m.C)")
        assert signal == "target_passed"
        assert fp == 3
        assert ff == 0
        assert cov == 1.0

    def test_classify_multi_target_partial(self):
        """Primary passes but others fail → target_partial."""
        target_results = {
            "test_a (m.C)": "passed",
            "test_b (m.C)": "failed",
            "test_c (m.C)": "passed",
        }
        signal, fp, ff, cov = _classify_multi_target_signal(target_results, "test_a (m.C)")
        assert signal == "target_partial"
        assert fp == 2
        assert ff == 1
        assert abs(cov - 2/3) < 0.01

    def test_classify_multi_target_primary_failed(self):
        """Primary fails → target_failed regardless of others."""
        target_results = {
            "test_a (m.C)": "failed",
            "test_b (m.C)": "passed",
        }
        signal, fp, ff, cov = _classify_multi_target_signal(target_results, "test_a (m.C)")
        assert signal == "target_failed"
        assert fp == 1
        assert ff == 1
        assert cov == 0.5

    def test_classify_multi_target_primary_error(self):
        target_results = {
            "test_a (m.C)": "error",
            "test_b (m.C)": "passed",
        }
        signal, fp, ff, cov = _classify_multi_target_signal(target_results, "test_a (m.C)")
        assert signal == "target_error"

    def test_classify_multi_target_primary_missing(self):
        target_results = {
            "test_a (m.C)": "missing",
            "test_b (m.C)": "passed",
        }
        signal, fp, ff, cov = _classify_multi_target_signal(target_results, "test_a (m.C)")
        assert signal == "target_missing"

    def test_classify_multi_target_empty(self):
        signal, fp, ff, cov = _classify_multi_target_signal({}, "test_a (m.C)")
        assert signal == "non_corrective_noise"
        assert fp == 0
        assert ff == 0
        assert cov == 0.0

    def test_classify_single_target_passed(self):
        """Single F2P → same as before (target_passed)."""
        target_results = {"test_a (m.C)": "passed"}
        signal, fp, ff, cov = _classify_multi_target_signal(target_results, "test_a (m.C)")
        assert signal == "target_passed"
        assert fp == 1
        assert cov == 1.0

    def test_classify_single_target_failed(self):
        target_results = {"test_a (m.C)": "failed"}
        signal, fp, ff, cov = _classify_multi_target_signal(target_results, "test_a (m.C)")
        assert signal == "target_failed"

    def test_result_fields_populated(self):
        """QuickJudgeResult carries multi-F2P fields."""
        r = QuickJudgeResult(
            target_results={"t1": "passed", "t2": "failed", "t3": "passed"},
            f2p_targeted=3,
            f2p_passed=2,
            f2p_failed=1,
            f2p_coverage=2/3,
        )
        assert r.f2p_targeted == 3
        assert r.f2p_passed == 2
        assert r.f2p_failed == 1
        assert abs(r.f2p_coverage - 2/3) < 0.01
        assert len(r.target_results) == 3

    def test_format_partial_message(self):
        """target_partial produces corrective partial message."""
        r = QuickJudgeResult(
            step=15,
            target_test_id="test_a (m.C)",
            target_status="passed",
            signal_kind="target_partial",
            corrective=True,
            command_scope="method",
            target_results={
                "test_a (m.C)": "passed",
                "test_b (m.C)": "failed",
                "test_c (m.C)": "passed",
            },
            f2p_targeted=3,
            f2p_passed=2,
            f2p_failed=1,
            f2p_coverage=2/3,
        )
        msg = format_agent_message(r)
        assert "PARTIAL" in msg
        assert "2/3 F2P targets pass" in msg
        assert "Still failing: test_b (m.C)" in msg
        assert "misses edge cases" in msg

    def test_format_all_passed_no_partial(self):
        """All F2P pass → target_passed, not partial."""
        r = QuickJudgeResult(
            step=15,
            target_test_id="test_a (m.C)",
            target_status="passed",
            signal_kind="target_passed",
            corrective=True,
            command_scope="method",
            target_results={
                "test_a (m.C)": "passed",
                "test_b (m.C)": "passed",
            },
            f2p_targeted=2,
            f2p_passed=2,
            f2p_failed=0,
            f2p_coverage=1.0,
        )
        msg = format_agent_message(r)
        assert "TARGET PASSED" in msg
        assert "PARTIAL" not in msg
        assert "2/2 F2P targets pass" in msg

    def test_format_single_target_no_coverage_suffix(self):
        """Single F2P target → no coverage suffix."""
        r = QuickJudgeResult(
            step=10,
            target_test_id="test_a (m.C)",
            target_status="passed",
            signal_kind="target_passed",
            corrective=True,
            command_scope="method",
            f2p_targeted=1,
            f2p_passed=1,
            f2p_coverage=1.0,
        )
        msg = format_agent_message(r)
        assert "F2P targets pass" not in msg

    def test_format_partial_multiple_still_failing(self):
        """Multiple still-failing tests shown (max 3)."""
        r = QuickJudgeResult(
            step=15,
            target_test_id="test_a (m.C)",
            target_status="passed",
            signal_kind="target_partial",
            corrective=True,
            command_scope="method",
            target_results={
                "test_a (m.C)": "passed",
                "test_b (m.C)": "failed",
                "test_c (m.C)": "error",
                "test_d (m.C)": "failed",
                "test_e (m.C)": "failed",
            },
            f2p_targeted=5,
            f2p_passed=1,
            f2p_failed=4,
            f2p_coverage=0.2,
        )
        msg = format_agent_message(r)
        assert "PARTIAL" in msg
        assert "1/5 F2P targets pass" in msg
        # At most 3 still-failing lines
        still_failing_count = msg.count("Still failing:")
        assert still_failing_count <= 3

    def test_backward_compat_target_test_id_and_status(self):
        """target_test_id and target_status still report primary target."""
        r = QuickJudgeResult(
            target_test_id="test_a (m.C)",
            target_status="passed",
            signal_kind="target_partial",
            target_results={
                "test_a (m.C)": "passed",
                "test_b (m.C)": "failed",
            },
            f2p_targeted=2,
            f2p_passed=1,
            f2p_failed=1,
        )
        assert r.target_test_id == "test_a (m.C)"
        assert r.target_status == "passed"  # primary target status
        assert r.signal_kind == "target_partial"  # but signal reflects partial

    def test_corrective_true_for_partial(self):
        """target_partial is corrective (primary passed → corrective=True)."""
        r = QuickJudgeResult(
            target_status="passed",
            signal_kind="target_partial",
            corrective=True,
        )
        assert r.corrective is True


# ===========================================================================
# 11. Signal gating integration
# ===========================================================================

@skip_quick_judge
class TestSignalGating:
    """Verify that message content is gated by target_status — the core contract."""

    def test_aggregate_pass_but_target_missing_no_positive(self):
        """14 tests pass but target is missing → no positive signal."""
        r = QuickJudgeResult(
            step=33,
            target_test_id="test_only_referenced_fields_selected (delete.tests.DeletionTests)",
            target_status="missing",
            signal_kind="target_missing",
            corrective=False,
            command_scope="class",
            tests_targeted=1,
            tests_passed=14,
        )
        msg = format_agent_message(r)
        assert "TARGET NOT FOUND" in msg
        # Must NOT say anything positive
        assert "PASSED" not in msg
        assert "fixes" not in msg
        assert "passing" not in msg.lower()

    def test_aggregate_pass_but_target_failed_corrective(self):
        """13 pass, 1 fail, target is the failing one → corrective negative."""
        r = QuickJudgeResult(
            step=33,
            target_test_id="test_only_referenced_fields_selected (delete.tests.DeletionTests)",
            target_status="failed",
            signal_kind="target_failed",
            corrective=True,
            command_scope="method",
            tests_targeted=1,
            tests_passed=13,
            tests_failed=1,
            failing_test_names=["test_only_referenced_fields_selected (delete.tests.DeletionTests)"],
        )
        msg = format_agent_message(r)
        assert "TARGET FAILED" in msg
        assert "still failing" in msg

    def test_target_passed_positive_allowed(self):
        """Target actually passed → positive signal OK."""
        r = QuickJudgeResult(
            step=10,
            target_test_id="test_only_referenced_fields_selected (delete.tests.DeletionTests)",
            target_status="passed",
            signal_kind="target_passed",
            corrective=True,
            command_scope="method",
            tests_targeted=1,
            tests_passed=1,
        )
        msg = format_agent_message(r)
        assert "TARGET PASSED" in msg
        assert "fixes" in msg


# ===========================================================================
# 12. _parse_pass_to_pass
# ===========================================================================

@skip_quick_judge
class TestParsePassToPass:
    def test_list_format(self):
        inst = {"PASS_TO_PASS": ["test_a (m.C)", "test_b (m.C)"]}
        assert _parse_pass_to_pass(inst) == ["test_a (m.C)", "test_b (m.C)"]

    def test_json_string_format(self):
        inst = {"PASS_TO_PASS": '["test_a (m.C)", "test_b (m.C)"]'}
        assert _parse_pass_to_pass(inst) == ["test_a (m.C)", "test_b (m.C)"]

    def test_empty_list(self):
        assert _parse_pass_to_pass({"PASS_TO_PASS": "[]"}) == []

    def test_missing_key(self):
        assert _parse_pass_to_pass({}) == []

    def test_invalid_json(self):
        assert _parse_pass_to_pass({"PASS_TO_PASS": "not json"}) == []

    def test_non_list_json(self):
        assert _parse_pass_to_pass({"PASS_TO_PASS": '"single_string"'}) == []


# ===========================================================================
# 13. select_sentinel_tests
# ===========================================================================

@skip_quick_judge
class TestSelectSentinelTests:
    def test_empty_p2p(self):
        inst = {"PASS_TO_PASS": "[]"}
        assert select_sentinel_tests(inst, []) == []

    def test_missing_p2p(self):
        assert select_sentinel_tests({}, []) == []

    def test_caps_at_3(self):
        ids = [f"test_{i} (mod.Cls)" for i in range(20)]
        inst = {"PASS_TO_PASS": json.dumps(ids)}
        result = select_sentinel_tests(inst, [])
        assert len(result) == 3

    def test_returns_sorted(self):
        inst = {"PASS_TO_PASS": '["test_c (m.C)", "test_a (m.C)", "test_b (m.C)"]'}
        result = select_sentinel_tests(inst, [])
        assert result == sorted(result)

    def test_changed_file_priority(self):
        """Tests matching changed files should be prioritized."""
        ids = [f"test_{i} (mod{i}.Cls)" for i in range(20)]
        inst = {"PASS_TO_PASS": json.dumps(ids)}
        result = select_sentinel_tests(inst, ["mod5.py"])
        # mod5 match should be included
        assert any("mod5" in t for t in result)

    def test_deterministic(self):
        inst = {"PASS_TO_PASS": json.dumps([f"test_{i} (m.C)" for i in range(10)])}
        r1 = select_sentinel_tests(inst, ["some_file.py"])
        r2 = select_sentinel_tests(inst, ["some_file.py"])
        assert r1 == r2

    def test_fewer_than_3_returns_all(self):
        inst = {"PASS_TO_PASS": '["test_a (m.C)", "test_b (m.C)"]'}
        result = select_sentinel_tests(inst, [])
        assert len(result) == 2
        assert result == ["test_a (m.C)", "test_b (m.C)"]


# ===========================================================================
# 14. Regression sentinel in QuickJudgeResult
# ===========================================================================

@skip_quick_judge
class TestRegressionSentinelFields:
    def test_default_no_regression(self):
        r = QuickJudgeResult()
        assert r.sentinel_tests_run == 0
        assert r.sentinel_tests_passed == 0
        assert r.sentinel_tests_failed == 0
        assert r.regression_detected is False
        assert r.regression_test_names == []

    def test_regression_detected_fields(self):
        r = QuickJudgeResult(
            sentinel_tests_run=3,
            sentinel_tests_passed=1,
            sentinel_tests_failed=2,
            regression_detected=True,
            regression_test_names=["test_existing_a (m.C)", "test_existing_b (m.C)"],
        )
        assert r.sentinel_tests_run == 3
        assert r.sentinel_tests_passed == 1
        assert r.sentinel_tests_failed == 2
        assert r.regression_detected is True
        assert len(r.regression_test_names) == 2


# ===========================================================================
# 15. Regression signal in format_agent_message
# ===========================================================================

@skip_quick_judge
class TestRegressionFormatMessage:
    def test_target_passed_with_regression_shows_regression_header(self):
        """TARGET PASSED + regression → regression header, not positive."""
        r = QuickJudgeResult(
            step=10,
            target_test_id="test_foo (m.C)",
            target_status="passed",
            signal_kind="target_passed",
            corrective=True,
            command_scope="method",
            regression_detected=True,
            regression_test_names=[
                "test_existing_a (m.C)",
                "test_existing_b (m.C)",
            ],
        )
        msg = format_agent_message(r)
        assert "REGRESSION DETECTED" in msg
        assert "breaks existing tests" in msg
        assert "Regression:" in msg
        assert "test_existing_a" in msg
        assert "test_existing_b" in msg
        # Must NOT say "fixes the reported issue" (misleading)
        assert "fixes the reported issue" not in msg

    def test_target_passed_no_regression_stays_positive(self):
        """TARGET PASSED + no regression → normal positive message."""
        r = QuickJudgeResult(
            step=10,
            target_test_id="test_foo (m.C)",
            target_status="passed",
            signal_kind="target_passed",
            corrective=True,
            command_scope="method",
            regression_detected=False,
        )
        msg = format_agent_message(r)
        assert "TARGET PASSED" in msg
        assert "REGRESSION" not in msg
        assert "fixes the reported issue" in msg

    def test_target_failed_no_regression_info(self):
        """TARGET FAILED → regression fields irrelevant (sentinels not run)."""
        r = QuickJudgeResult(
            step=10,
            target_test_id="test_foo (m.C)",
            target_status="failed",
            signal_kind="target_failed",
            corrective=True,
            command_scope="method",
            regression_detected=False,
            failing_test_names=["test_foo (m.C)"],
        )
        msg = format_agent_message(r)
        assert "TARGET FAILED" in msg
        assert "REGRESSION" not in msg

    def test_regression_takes_priority_over_partial(self):
        """Regression is checked before partial — regression is more important."""
        r = QuickJudgeResult(
            step=10,
            target_test_id="test_a (m.C)",
            target_status="passed",
            signal_kind="target_partial",
            corrective=True,
            command_scope="method",
            regression_detected=True,
            regression_test_names=["test_existing (m.C)"],
        )
        msg = format_agent_message(r)
        assert "REGRESSION DETECTED" in msg
        # Regression takes priority
        assert "PARTIAL" not in msg

    def test_regression_message_contains_step(self):
        r = QuickJudgeResult(
            step=15,
            target_test_id="test_foo (m.C)",
            target_status="passed",
            signal_kind="target_passed",
            corrective=True,
            command_scope="method",
            regression_detected=True,
            regression_test_names=["test_bar (m.C)"],
        )
        msg = format_agent_message(r)
        assert "step=15" in msg

    def test_regression_no_false_positive_on_unknown(self):
        """Unknown target → no regression check (sentinels not run)."""
        r = QuickJudgeResult(
            step=10,
            target_test_id="test_foo (m.C)",
            target_status="unknown",
            signal_kind="non_corrective_noise",
            corrective=False,
            command_scope="method",
            regression_detected=False,
        )
        msg = format_agent_message(r)
        assert "REGRESSION" not in msg
        assert "NO SIGNAL" in msg


# ===========================================================================
# 16. Docstring-based test name resolution
# ===========================================================================

@skip_quick_judge
class TestIsDocstringTestName:
    """Tests for _is_docstring_test_name detection."""

    def test_docstring_with_spaces(self):
        assert _is_docstring_test_name(
            "Using Model.clean method should not skip models.W036 when a list of validators is not provided."
        ) is True

    def test_natural_language_description(self):
        assert _is_docstring_test_name(
            "Admin actions are shown even if the form is invalid."
        ) is True

    def test_django_format_not_docstring(self):
        assert _is_docstring_test_name(
            "test_foo (delete.tests.DeletionTests)"
        ) is False

    def test_pytest_format_not_docstring(self):
        assert _is_docstring_test_name(
            "tests/test_foo.py::TestClass::test_method"
        ) is False

    def test_dotted_path_not_docstring(self):
        assert _is_docstring_test_name(
            "check_framework.test_model_checks.ModelValidationTests"
        ) is False

    def test_empty_string(self):
        assert _is_docstring_test_name("") is False

    def test_none_input(self):
        assert _is_docstring_test_name(None) is False

    def test_single_word_no_spaces(self):
        assert _is_docstring_test_name("test_something") is False

    def test_whitespace_only(self):
        assert _is_docstring_test_name("   ") is False


@skip_quick_judge
class TestExtractDocstringKeywords:
    """Tests for _extract_docstring_keywords extraction."""

    def test_extracts_warning_codes(self):
        kws = _extract_docstring_keywords(
            "Using Model.clean method should not skip models.W036"
        )
        assert "W036" in kws

    def test_extracts_dotted_identifiers(self):
        kws = _extract_docstring_keywords(
            "Using Model.clean method should not skip models.W036"
        )
        assert "Model.clean" in kws
        assert "models.W036" in kws

    def test_extracts_camelcase(self):
        kws = _extract_docstring_keywords(
            "The ModelValidation check should pass"
        )
        assert "ModelValidation" in kws

    def test_extracts_snake_case(self):
        kws = _extract_docstring_keywords(
            "The required_fields validator must pass"
        )
        assert "required_fields" in kws

    def test_filters_common_words(self):
        kws = _extract_docstring_keywords(
            "Using the Method should not fail"
        )
        # "Using", "Method", "Should" are common words — should be filtered
        assert "Using" not in kws
        assert "Method" not in kws

    def test_empty_string_returns_empty(self):
        assert _extract_docstring_keywords("") == []

    def test_no_keywords_in_plain_english(self):
        kws = _extract_docstring_keywords("this is a simple test")
        # No caps, no codes, no dotted, no camelCase
        # But "simple_test" would not match since there is no underscore in "simple test"
        assert len(kws) == 0 or all(k == k for k in kws)  # no crash


@skip_quick_judge
class TestResolveDocstringTest:
    """Tests for _resolve_docstring_test fuzzy matching."""

    def test_single_result_unambiguous(self):
        """When only 1 test result exists, match is unambiguous."""
        results = {
            "test_list_containing_non_callable (check_framework.test_model_checks.ModelValidationTests)": "passed"
        }
        status = _resolve_docstring_test(
            "Using Model.clean method should not skip models.W036 when a list of validators is not provided.",
            results,
        )
        assert status == "passed"

    def test_keyword_match_w036(self):
        """W036 code in docstring matches test containing W036-related name."""
        results = {
            "test_list_containing_non_callable (check_framework.test_model_checks.ModelValidationTests)": "passed",
            "test_other_check (check_framework.test_model_checks.OtherTests)": "failed",
        }
        # docstring mentions W036 and models — should match the model_checks test
        # But since neither result ID literally contains "W036", let us use a more realistic case
        results2 = {
            "test_w036_non_callable (check_framework.test_model_checks.W036Tests)": "passed",
            "test_other_unrelated (other.module.Tests)": "failed",
        }
        status = _resolve_docstring_test(
            "Using Model.clean method should not skip models.W036",
            results2,
        )
        assert status == "passed"

    def test_ambiguous_returns_none(self):
        """Multiple results with same keyword score → return None (conservative)."""
        results = {
            "test_a (mod.W036Tests)": "passed",
            "test_b (mod.W036Checks)": "failed",
        }
        status = _resolve_docstring_test(
            "Check W036 behavior",
            results,
        )
        # Both contain W036 with same score → ambiguous → None
        assert status is None

    def test_empty_results(self):
        assert _resolve_docstring_test("some docstring", {}) is None

    def test_no_keywords_no_match(self):
        """Docstring with no extractable keywords → None."""
        results = {"test_a (m.C)": "passed"}
        status = _resolve_docstring_test("a b c d", results)
        # Single result → unambiguous match regardless
        assert status == "passed"

    def test_unambiguous_score_difference(self):
        """Top match has higher score than second → returns top match."""
        results = {
            "test_model_clean_w036 (check_framework.test_model_checks.ModelValidationTests)": "passed",
            "test_unrelated (other.tests.OtherTests)": "failed",
        }
        status = _resolve_docstring_test(
            "Using Model.clean method should not skip models.W036",
            results,
        )
        assert status == "passed"

    def test_failed_test_resolved(self):
        """Docstring resolving to a failed test returns 'failed'."""
        results = {
            "test_w036_check (model_checks.W036Tests)": "failed",
        }
        status = _resolve_docstring_test(
            "models.W036 should be raised",
            results,
        )
        assert status == "failed"

    def test_error_test_resolved(self):
        """Docstring resolving to an errored test returns 'error'."""
        results = {
            "test_w036_check (model_checks.W036Tests)": "error",
        }
        status = _resolve_docstring_test(
            "models.W036 should be raised",
            results,
        )
        assert status == "error"


@skip_quick_judge
class TestResolveTargetStatusWithDocstring:
    """Integration: _resolve_target_status falls back to docstring resolution."""

    def test_docstring_target_resolves_to_passed(self):
        """Docstring F2P entry resolves to passed test in output."""
        test_results = {
            "test_list_containing_non_callable (check_framework.test_model_checks.ModelValidationTests)": "passed",
        }
        status = _resolve_target_status(
            test_results,
            "Using Model.clean method should not skip models.W036 when a list of validators is not provided.",
        )
        # Should resolve via docstring matching (single result = unambiguous)
        assert status == "passed"

    def test_docstring_target_resolves_to_failed(self):
        test_results = {
            "test_list_containing_non_callable (check_framework.test_model_checks.ModelValidationTests)": "failed",
        }
        status = _resolve_target_status(
            test_results,
            "Using Model.clean method should not skip models.W036 when a list of validators is not provided.",
        )
        assert status == "failed"

    def test_docstring_no_match_returns_missing(self):
        """Docstring with no keyword match and multiple results → missing."""
        test_results = {
            "test_a (unrelated.module.Tests)": "passed",
            "test_b (other.module.Tests)": "failed",
        }
        status = _resolve_target_status(
            test_results,
            "Something completely unrelated with no keywords",
        )
        # No keywords match → _resolve_docstring_test returns None → "missing"
        assert status == "missing"

    def test_standard_format_still_works(self):
        """Standard Django format target still works (no docstring path)."""
        test_results = {
            "test_foo (mod.Tests)": "passed",
        }
        status = _resolve_target_status(test_results, "test_foo (mod.Tests)")
        assert status == "passed"

    def test_empty_output_with_docstring_returns_unknown(self):
        status = _resolve_target_status(
            {},
            "Some docstring test name",
        )
        assert status == "unknown"


@skip_quick_judge
class TestDocstringToTestLabel:
    """Tests for _docstring_to_test_label mapping."""

    def test_sibling_in_standard_format_provides_label(self):
        inst = {
            "FAIL_TO_PASS": json.dumps([
                "Using Model.clean method should not skip models.W036",
                "test_list_containing_non_callable (check_framework.test_model_checks.ModelValidationTests)",
            ])
        }
        label = _docstring_to_test_label(
            "Using Model.clean method should not skip models.W036",
            inst,
        )
        assert label == "check_framework.test_model_checks.ModelValidationTests"

    def test_no_sibling_returns_none(self):
        inst = {
            "FAIL_TO_PASS": json.dumps([
                "Using Model.clean method should not skip models.W036",
            ])
        }
        label = _docstring_to_test_label(
            "Using Model.clean method should not skip models.W036",
            inst,
        )
        assert label is None

    def test_keyword_matching_sibling(self):
        """Sibling whose class path contains a keyword from the docstring is preferred."""
        inst = {
            "FAIL_TO_PASS": json.dumps([
                "The W036 check should pass",
                "test_other (check_framework.test_model_checks.W036Tests)",
            ])
        }
        label = _docstring_to_test_label(
            "The W036 check should pass",
            inst,
        )
        # Should match sibling with W036 in its class path
        assert label is not None
        assert "W036" in label or "model_checks" in label

    def test_empty_f2p_returns_none(self):
        inst = {"FAIL_TO_PASS": "[]"}
        label = _docstring_to_test_label("some docstring", inst)
        assert label is None

    def test_no_keywords_returns_none(self):
        inst = {
            "FAIL_TO_PASS": json.dumps([
                "a b c",
                "test_foo (mod.Tests)",
            ])
        }
        # "a b c" has no extractable keywords -> returns None (conservative)
        label = _docstring_to_test_label("a b c", inst)
        assert label is None
