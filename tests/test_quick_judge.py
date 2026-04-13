"""
test_quick_judge.py — Unit tests for the E1 Quick Judge system.

Tests cover:
1. select_targeted_tests — test selection logic, stability, priority
2. classify_direction — direction signal classification from consecutive results
3. format_agent_message — minimal structured message formatting
4. should_trigger_quick_judge — all 6 trigger conditions on StepMonitorState
5. detect_acknowledged — heuristic acknowledged detection
6. detect_effective — convergence detection across quick judge history
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
    )
    _QUICK_JUDGE_AVAILABLE = True
except ImportError:
    _QUICK_JUDGE_AVAILABLE = False

# --- Import StepMonitorState ---
try:
    from step_monitor_state import StepMonitorState
    from control.reasoning_state import initial_reasoning_state
    _STATE_AVAILABLE = True
except ImportError:
    _STATE_AVAILABLE = False

skip_quick_judge = pytest.mark.skipif(
    not _QUICK_JUDGE_AVAILABLE,
    reason="quick_judge.py not yet implemented",
)

skip_state = pytest.mark.skipif(
    not _STATE_AVAILABLE,
    reason="step_monitor_state.py not available",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_instance(f2p_tests=None):
    """Create a minimal SWE-bench instance dict with FAIL_TO_PASS."""
    return {
        "instance_id": "test__test-001",
        "FAIL_TO_PASS": json.dumps(f2p_tests or []),
    }


def _make_result(**kwargs):
    """Create a QuickJudgeResult with sensible defaults."""
    defaults = dict(
        step=1,
        tests_targeted=5,
        tests_passed=3,
        tests_failed=2,
        tests_error=0,
        failing_test_names=["test_a", "test_b"],
        elapsed_ms=100.0,
        direction="first_signal",
    )
    defaults.update(kwargs)
    return QuickJudgeResult(**defaults)


def _make_state(phase="EXECUTE", **overrides):
    """Create a StepMonitorState with all quick judge trigger conditions met.

    By default should_trigger_quick_judge("new_hash") returns True.
    Override individual fields to test specific conditions.
    """
    instance = {"instance_id": "test__test-001", "FAIL_TO_PASS": "[]"}
    state = StepMonitorState(
        instance_id="test__test-001",
        attempt=1,
        instance=instance,
    )
    # Set cp_state with the desired phase (frozen dataclass — must replace entirely)
    state.cp_state = initial_reasoning_state(phase)

    # Defaults: all trigger conditions pass
    state.last_quick_judge_patch = ""
    state._llm_step = 10
    state.last_quick_judge_step = 0
    state.last_quick_judge_time = 0.0
    state.early_stop_verdict = None
    state.quick_judge_count = 0

    for key, val in overrides.items():
        setattr(state, key, val)
    return state


# ===========================================================================
# 1. test_select_targeted_tests
# ===========================================================================


@skip_quick_judge
class TestSelectTargetedTests:
    """Test selection logic for F2P test subset."""

    def test_f2p_lte_5_returns_all(self):
        """F2P with <= 5 tests returns all of them."""
        instance = _make_instance(["test_a", "test_b", "test_c"])
        result = select_targeted_tests(instance, changed_files=[])
        assert set(result) == {"test_a", "test_b", "test_c"}
        assert len(result) == 3

    def test_f2p_gt_5_returns_max_5(self):
        """F2P with > 5 tests returns at most 5."""
        instance = _make_instance([f"test_{i}" for i in range(10)])
        result = select_targeted_tests(instance, changed_files=[])
        assert len(result) <= 5

    def test_f2p_json_string_parsed(self):
        """F2P provided as JSON string (not list) is parsed correctly."""
        instance = {
            "instance_id": "test__test-001",
            "FAIL_TO_PASS": '["test_alpha", "test_beta"]',
        }
        result = select_targeted_tests(instance, changed_files=[])
        assert len(result) == 2
        assert "test_alpha" in result
        assert "test_beta" in result

    def test_changed_files_prioritized(self):
        """Tests matching changed files are prioritized in selection."""
        f2p = [
            "tests/test_utils.py::test_parse",
            "tests/test_models.py::test_create",
            "tests/test_views.py::test_list",
            "tests/test_forms.py::test_validate",
            "tests/test_admin.py::test_admin_view",
            "tests/test_serializers.py::test_serialize",
            "tests/test_middleware.py::test_middleware",
        ]
        instance = _make_instance(f2p)
        changed = ["models.py"]
        result = select_targeted_tests(instance, changed_files=changed)
        assert len(result) <= 5
        # test_models should be prioritized (matches changed file basename)
        assert any("test_models" in t for t in result)

    def test_output_deterministic(self):
        """Output is deterministic (sorted) across calls."""
        instance = _make_instance(["test_c", "test_a", "test_b"])
        result1 = select_targeted_tests(instance, changed_files=[])
        result2 = select_targeted_tests(instance, changed_files=[])
        assert result1 == result2
        # Verify sorted
        assert result1 == sorted(result1)

    def test_empty_f2p_returns_empty(self):
        """Empty F2P returns empty list."""
        instance = _make_instance([])
        result = select_targeted_tests(instance, changed_files=[])
        assert result == []


# ===========================================================================
# 2. test_classify_direction
# ===========================================================================


@skip_quick_judge
class TestClassifyDirection:
    """Direction signal classification from consecutive quick judge results."""

    def test_previous_none_is_first_signal(self):
        """previous=None -> first_signal."""
        current = _make_result(tests_targeted=3, tests_passed=2, tests_failed=1)
        result = classify_direction(current, None)
        assert result == "first_signal"

    def test_more_pass_is_improved(self):
        """More tests pass than before -> improved (when failures not a subset)."""
        previous = _make_result(
            tests_passed=2, tests_failed=3,
            failing_test_names=["test_a", "test_b", "test_c"],
        )
        current = _make_result(
            tests_passed=3, tests_failed=2,
            failing_test_names=["test_x", "test_y"],  # NOT a subset of previous
        )
        result = classify_direction(current, previous)
        assert result == "improved"

    def test_more_pass_subset_is_likely_right(self):
        """More tests pass + failures are subset of previous -> likely_right_direction."""
        previous = _make_result(
            tests_passed=2, tests_failed=3,
            failing_test_names=["test_a", "test_b", "test_c"],
        )
        current = _make_result(
            tests_passed=4, tests_failed=1,
            failing_test_names=["test_a"],  # subset of previous
        )
        result = classify_direction(current, previous)
        assert result == "likely_right_direction"

    def test_fewer_pass_is_likely_wrong_direction(self):
        """Fewer tests pass than before -> likely_wrong_direction."""
        previous = _make_result(
            tests_passed=4, tests_failed=1,
            failing_test_names=["test_a"],
        )
        current = _make_result(
            tests_passed=2, tests_failed=3,
            failing_test_names=["test_a", "test_b", "test_c"],
        )
        result = classify_direction(current, previous)
        assert result == "likely_wrong_direction"

    def test_same_counts_same_tests_is_unchanged(self):
        """Same pass count, same failing tests -> unchanged."""
        previous = _make_result(
            tests_passed=3, tests_failed=2,
            failing_test_names=["test_a", "test_b"],
        )
        current = _make_result(
            tests_passed=3, tests_failed=2,
            failing_test_names=["test_a", "test_b"],
        )
        result = classify_direction(current, previous)
        assert result == "unchanged"

    def test_same_counts_different_tests_is_unchanged(self):
        """Same pass count, different failing tests -> unchanged."""
        previous = _make_result(
            tests_passed=3, tests_failed=2,
            failing_test_names=["test_a", "test_b"],
        )
        current = _make_result(
            tests_passed=3, tests_failed=2,
            failing_test_names=["test_c", "test_d"],
        )
        result = classify_direction(current, previous)
        assert result == "unchanged"

    def test_all_errors_is_inconclusive(self):
        """All tests errored -> inconclusive."""
        previous = _make_result(
            tests_passed=3, tests_failed=2,
            failing_test_names=["test_a", "test_b"],
        )
        current = _make_result(
            tests_targeted=5, tests_passed=0, tests_failed=0,
            tests_error=5,
            failing_test_names=[],
        )
        result = classify_direction(current, previous)
        assert result == "inconclusive"

    def test_fewer_pass_not_all_error(self):
        """Fewer pass but not all errors -> likely_wrong_direction (not inconclusive)."""
        previous = _make_result(
            tests_passed=4, tests_failed=1,
            failing_test_names=["test_a"],
        )
        current = _make_result(
            tests_targeted=5, tests_passed=2, tests_failed=2,
            tests_error=1,
            failing_test_names=["test_a", "test_b"],
        )
        result = classify_direction(current, previous)
        assert result == "likely_wrong_direction"


# ===========================================================================
# 3. test_format_agent_message
# ===========================================================================


@skip_quick_judge
class TestFormatAgentMessage:
    """Minimal structured message formatting for agent injection."""

    def test_contains_quick_check_header(self):
        """Message contains [QUICK_CHECK step=N]."""
        result = _make_result(
            step=7, tests_targeted=3, tests_passed=2, tests_failed=1,
            direction="improved",
            failing_test_names=["test_a"],
        )
        msg = format_agent_message(result)
        assert "[QUICK_CHECK step=7]" in msg

    def test_contains_direction(self):
        """Message contains the direction string."""
        result = _make_result(
            step=5, tests_targeted=3, tests_passed=1, tests_failed=2,
            direction="regressed",
            failing_test_names=["test_a", "test_b"],
        )
        msg = format_agent_message(result)
        # Direction appears in the header line
        assert "regressed" in msg.lower()

    def test_failing_names_limited_to_3(self):
        """Failing test names are limited to at most 3 in the message."""
        result = _make_result(
            step=3, tests_targeted=10, tests_passed=4, tests_failed=6,
            direction="likely_wrong_direction",
            # QuickJudgeResult.failing_test_names is already capped at 3 by run_quick_judge,
            # but format_agent_message also caps at 3 defensively
            failing_test_names=["test_a", "test_b", "test_c", "test_d", "test_e"],
        )
        msg = format_agent_message(result)
        # Count how many of the 5 test names appear
        found = sum(1 for name in result.failing_test_names if name in msg)
        assert found <= 3

    def test_contains_direction_hint(self):
        """Message contains a one-sentence hint matching the direction."""
        for direction, hint_fragment in [
            ("first_signal", "First test signal"),
            ("improved", "Progress"),
            ("likely_right_direction", "Good direction"),
            ("likely_wrong_direction", "Wrong direction"),
            ("unchanged", "No change"),
            ("inconclusive", "could not run"),
        ]:
            result = _make_result(
                step=5, tests_targeted=3, tests_passed=1, tests_failed=2,
                direction=direction,
                failing_test_names=["test_a"],
            )
            msg = format_agent_message(result)
            assert hint_fragment.lower() in msg.lower(), (
                f"Direction {direction!r}: expected hint containing {hint_fragment!r}, "
                f"got: {msg!r}"
            )

    def test_no_raw_stdout(self):
        """Message does NOT contain raw stdout content (traceback, etc.)."""
        result = _make_result(
            step=5, tests_targeted=3, tests_passed=1, tests_failed=2,
            direction="regressed",
            failing_test_names=["test_a"],
        )
        msg = format_agent_message(result)
        assert "Traceback" not in msg
        assert "FAILED" not in msg
        assert "stdout" not in msg.lower()
        assert "stderr" not in msg.lower()


# ===========================================================================
# 4. test_should_trigger_quick_judge
# ===========================================================================


@skip_state
class TestShouldTriggerQuickJudge:
    """All 6 trigger conditions on StepMonitorState.should_trigger_quick_judge()."""

    def test_all_conditions_met_returns_true(self):
        """When all conditions are satisfied, trigger returns True."""
        state = _make_state()
        assert state.should_trigger_quick_judge("new_patch_hash") is True

    def test_c1_non_execute_phase_returns_false(self):
        """C1: phase != EXECUTE -> False."""
        for phase in ["OBSERVE", "ANALYZE", "DECIDE"]:
            state = _make_state(phase=phase)
            assert state.should_trigger_quick_judge("new_patch_hash") is False, (
                f"phase={phase} should return False"
            )

    def test_c2_same_patch_hash_returns_false(self):
        """C2: same patch hash as last quick judge -> False."""
        state = _make_state(last_quick_judge_patch="same_hash")
        assert state.should_trigger_quick_judge("same_hash") is False

    def test_c3_step_interval_too_small_returns_false(self):
        """C3: fewer than 3 steps since last quick judge -> False."""
        state = _make_state(_llm_step=5, last_quick_judge_step=4)
        assert state.should_trigger_quick_judge("new_hash") is False

        state = _make_state(_llm_step=5, last_quick_judge_step=3)
        assert state.should_trigger_quick_judge("new_hash") is False

    def test_c3_exactly_3_steps_returns_true(self):
        """C3: exactly 3 steps since last -> True (boundary)."""
        state = _make_state(_llm_step=6, last_quick_judge_step=3)
        assert state.should_trigger_quick_judge("new_hash") is True

    def test_c4_time_interval_too_small_returns_false(self):
        """C4: less than 15s since last quick judge -> False."""
        state = _make_state(last_quick_judge_time=time.monotonic() - 5.0)
        assert state.should_trigger_quick_judge("new_hash") is False

    def test_c5_early_stop_verdict_returns_false(self):
        """C5: early_stop_verdict is set -> False."""
        state = _make_state(early_stop_verdict="no_signal")
        assert state.should_trigger_quick_judge("new_hash") is False

    def test_c6_quota_exhausted_returns_false(self):
        """C6: quick_judge_count >= 3 -> False."""
        state = _make_state(quick_judge_count=3)
        assert state.should_trigger_quick_judge("new_hash") is False

        state = _make_state(quick_judge_count=5)
        assert state.should_trigger_quick_judge("new_hash") is False

    def test_current_phase_override_takes_precedence(self):
        """current_phase kwarg overrides stale cp_state.phase (production bug fix)."""
        # cp_state says OBSERVE (stale), but current_phase says EXECUTE (real)
        state = _make_state(phase="OBSERVE")
        assert state.should_trigger_quick_judge("new_hash") is False  # without override
        assert state.should_trigger_quick_judge("new_hash", current_phase="EXECUTE") is True

        # cp_state says EXECUTE, but current_phase says ANALYZE (override wins)
        state2 = _make_state(phase="EXECUTE")
        assert state2.should_trigger_quick_judge("new_hash", current_phase="ANALYZE") is False


# ===========================================================================
# 5. test_detect_acknowledged
# ===========================================================================


@skip_quick_judge
class TestDetectAcknowledged:
    """Heuristic detection of whether agent acknowledged a quick judge result."""

    def test_test_name_in_assistant_text(self):
        """Test name appears in assistant text -> True."""
        qj = _make_result(
            failing_test_names=["tests/test_utils.py::test_parse_date"],
        )
        text = "I see test_parse_date is failing. Let me look at the date parsing logic."
        assert detect_acknowledged(qj, text, []) is True

    def test_no_match_returns_false(self):
        """No matching test name in assistant text -> False."""
        qj = _make_result(
            failing_test_names=["tests/test_utils.py::test_parse_date"],
        )
        text = "Let me continue editing the models file."
        assert detect_acknowledged(qj, text, []) is False

    def test_short_name_extraction_works(self):
        """Short name extraction via split on '::' works for matching."""
        qj = _make_result(
            failing_test_names=["very/long/path/test_module.py::TestClass::test_method"],
        )
        # The short name is "test_method" (last part after ::)
        text = "I need to fix test_method to handle the edge case."
        assert detect_acknowledged(qj, text, []) is True


# ===========================================================================
# 6. test_detect_effective
# ===========================================================================


@skip_quick_judge
class TestDetectEffective:
    """Convergence detection across quick judge history."""

    def test_single_entry_returns_false(self):
        """Need >= 2 entries to detect effectiveness."""
        history = [{"direction": "improved"}]
        assert detect_effective(history) is False

    def test_regressed_then_improved_is_effective(self):
        """BAD -> GOOD transition = convergence = effective."""
        history = [
            {"direction": "regressed"},
            {"direction": "improved"},
        ]
        assert detect_effective(history) is True

    def test_improved_then_regressed_is_not_effective(self):
        """GOOD -> BAD transition = divergence = not effective."""
        history = [
            {"direction": "improved"},
            {"direction": "regressed"},
        ]
        assert detect_effective(history) is False

    def test_unchanged_unchanged_is_not_effective(self):
        """unchanged -> unchanged = no convergence = not effective."""
        history = [
            {"direction": "unchanged"},
            {"direction": "unchanged"},
        ]
        assert detect_effective(history) is False

    def test_first_signal_then_likely_right_is_effective(self):
        """first_signal (not in GOOD) -> likely_right_direction (GOOD) = effective."""
        history = [
            {"direction": "first_signal"},
            {"direction": "likely_right_direction"},
        ]
        assert detect_effective(history) is True

    def test_empty_history_returns_false(self):
        """Empty history -> False."""
        assert detect_effective([]) is False
