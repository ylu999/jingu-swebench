"""
Tests for retry_controller handling of None values in controlled_verify.

Regression tests for the NoneType >= int crash (batch-repair-routing-v1).
Root cause: dict.get("f2p_passed", -1) returns None when key exists with
value None. Then None >= 0 in classify_outcome_v2 crashes.
"""
import sys
from pathlib import Path

# Add scripts/ to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest
from retry_controller import build_retry_plan, classify_outcome_v2


class TestNoneCVValues:
    """Controlled verify can have None f2p_passed/f2p_failed when kind=controlled_error."""

    def test_classify_outcome_v2_with_none_f2p_passed_and_nonzero_total_crashes(self):
        """When f2p_total > 0 and f2p_passed is None, comparison crashes.

        f2p_total=0 returns early ("no_signal") before hitting None comparison.
        But with f2p_total > 0, it reaches f2p_passed < f2p_total → TypeError.
        """
        with pytest.raises(TypeError):
            classify_outcome_v2(f2p_passed=None, f2p_total=1, new_failures=0, patch_exists=True)

    def test_classify_outcome_v2_with_sanitized_none(self):
        """When None is converted to -1 or 0, no crash."""
        # -1 means "unknown" — should map to 0 via caller sanitization
        result = classify_outcome_v2(f2p_passed=0, f2p_total=0, new_failures=0, patch_exists=True)
        assert isinstance(result, str)

    def test_build_retry_plan_with_none_inner_f2p(self):
        """build_retry_plan must not crash when inner_f2p_passed is None.

        This is the exact crash path from batch-repair-routing-v1:
        controlled_verify kind=controlled_error → f2p_passed=None →
        inner_f2p_passed=None → classify_outcome_v2 → None >= 0 → TypeError.
        """
        # Simulate the caller sanitization: None → -1
        plan = build_retry_plan(
            problem_statement="test problem",
            patch_text="diff --git a/test.py b/test.py\n--- a/test.py\n+++ b/test.py\n@@ -1 +1 @@\n-old\n+new",
            jingu_body={"test_results": {"ran_tests": True}},
            fail_to_pass_tests=["test_foo"],
            gate_admitted=True,
            gate_reason_codes=[],
            exec_feedback="tests failed",
            attempt=1,
            controlled_verify={"kind": "controlled_error", "f2p_passed": None, "f2p_failed": None},
            inner_f2p_passed=-1,  # sanitized from None
            inner_f2p_total=0,
            inner_new_failures=0,
            patch_exists=True,
        )
        assert plan is not None
        assert plan.control_action in ("CONTINUE", "ADJUST", "STOP_FAIL", "STOP_NO_SIGNAL")


class TestCallerSanitization:
    """Test the sanitization pattern used in run_with_jingu_gate.py."""

    @pytest.mark.parametrize("raw_value,expected", [
        (None, -1),    # key exists, value None → should become -1
        (0, 0),        # key exists, value 0
        (5, 5),        # key exists, value 5
        (-1, -1),      # key exists, value -1
    ])
    def test_inner_cv_f2p_passed_sanitization(self, raw_value, expected):
        """Simulate _inner_cv.get("f2p_passed") with None value.

        The fix: use explicit None check instead of dict.get() default.
        dict.get("key", default) returns None when key exists with value=None,
        NOT the default. This is a Python gotcha.
        """
        _inner_cv = {"f2p_passed": raw_value, "f2p_failed": raw_value}

        # OLD (buggy): _inner_cv.get("f2p_passed", -1) → returns None when value is None
        buggy_result = _inner_cv.get("f2p_passed", -1)
        assert buggy_result == raw_value  # proves .get() returns None, not -1

        # NEW (fixed): explicit None check
        fixed_result = _inner_cv.get("f2p_passed") if _inner_cv.get("f2p_passed") is not None else -1
        assert fixed_result == expected

    def test_none_f2p_total_sanitization(self):
        """f2p_total computation must handle None values without crash."""
        _inner_cv = {"f2p_passed": None, "f2p_failed": None, "p2p_failed": None}

        # Fixed pattern: (value or 0) handles None → 0
        f2p_total = (_inner_cv.get("f2p_passed") or 0) + (_inner_cv.get("f2p_failed") or 0)
        assert f2p_total == 0

        new_failures = _inner_cv.get("p2p_failed") or 0
        assert new_failures == 0


class TestControlledErrorScenario:
    """End-to-end test for the controlled_error scenario that caused the crash."""

    def test_execution_error_cv_does_not_crash_retry_plan(self):
        """Simulate exact batch-repair-routing-v1 crash scenario.

        controlled_verify kind=controlled_error produces:
        - f2p_passed: None (no F2P test results because test runner errored)
        - f2p_failed: None
        - p2p_failed: None or 0
        """
        cv_flat = {
            "verification_kind": "controlled_error",
            "tests_passed": 0,
            "tests_failed": 2,
            "exit_code": 1,
            "f2p_passed": None,
            "f2p_failed": None,
            "p2p_passed": None,
            "p2p_failed": None,
            "eval_resolved": None,
        }

        # Simulate caller sanitization (run_with_jingu_gate.py lines 4042-4044)
        inner_f2p_passed = cv_flat.get("f2p_passed") if cv_flat.get("f2p_passed") is not None else -1
        inner_f2p_total = (cv_flat.get("f2p_passed") or 0) + (cv_flat.get("f2p_failed") or 0)
        inner_new_failures = cv_flat.get("p2p_failed") or 0

        assert inner_f2p_passed == -1
        assert inner_f2p_total == 0
        assert inner_new_failures == 0

        # Now build_retry_plan should not crash
        plan = build_retry_plan(
            problem_statement="test",
            patch_text="diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b",
            jingu_body={"test_results": {"ran_tests": True}, "controlled_verify": cv_flat},
            fail_to_pass_tests=["test_foo"],
            gate_admitted=True,
            gate_reason_codes=[],
            exec_feedback="test runner errored",
            attempt=1,
            controlled_verify=cv_flat,
            inner_f2p_passed=inner_f2p_passed,
            inner_f2p_total=inner_f2p_total,
            inner_new_failures=inner_new_failures,
            patch_exists=True,
        )
        assert plan is not None
