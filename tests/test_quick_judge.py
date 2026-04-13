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
