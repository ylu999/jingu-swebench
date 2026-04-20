"""Tests for sentinel priority — previous P2P regression tests get priority in quick judge.

Verifies the data flow:
  A1 CV p2p_failing_names → JinguAgent._prev_p2p_regression_names →
  A2 StepMonitorState.priority_sentinel_tests → select_sentinel_tests(priority_tests=...)
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from quick_judge import select_sentinel_tests


# ── Mock instance with P2P tests ─────────────────────────────────────────────

def _make_instance(p2p_tests: list[str]) -> dict:
    """Create a minimal instance dict with PASS_TO_PASS tests."""
    import json
    return {
        "instance_id": "django__django-11490",
        "repo": "django/django",
        "version": "3.0",
        "PASS_TO_PASS": json.dumps(p2p_tests),
        "FAIL_TO_PASS": json.dumps(["test_target (tests.test_foo.FooTest)"]),
    }


_P2P_TESTS = [
    "test_alpha (tests.test_models.ModelTest)",
    "test_beta (tests.test_views.ViewTest)",
    "test_gamma (tests.test_forms.FormTest)",
    "test_delta (tests.test_utils.UtilTest)",
    "test_epsilon (tests.test_admin.AdminTest)",
]


# ── select_sentinel_tests with priority_tests ────────────────────────────────

def test_priority_tests_included_first():
    """Priority tests from previous regression are included in sentinel set."""
    instance = _make_instance(_P2P_TESTS)
    priority = ["test_delta (tests.test_utils.UtilTest)"]

    with patch("quick_judge._parse_pass_to_pass", return_value=_P2P_TESTS):
        selected = select_sentinel_tests(instance, [], priority_tests=priority)

    assert "test_delta (tests.test_utils.UtilTest)" in selected
    assert len(selected) <= 3


def test_priority_tests_take_all_slots():
    """When priority tests fill all slots, no other tests are selected."""
    instance = _make_instance(_P2P_TESTS)
    priority = _P2P_TESTS[:3]  # 3 priority tests = all sentinel slots

    with patch("quick_judge._parse_pass_to_pass", return_value=_P2P_TESTS):
        selected = select_sentinel_tests(instance, [], priority_tests=priority)

    assert set(selected) == set(priority)
    assert len(selected) == 3


def test_priority_tests_not_in_p2p_ignored():
    """Priority tests not in the P2P list are silently skipped."""
    instance = _make_instance(_P2P_TESTS)
    priority = ["test_nonexistent (tests.test_missing.MissingTest)"]

    with patch("quick_judge._parse_pass_to_pass", return_value=_P2P_TESTS):
        selected = select_sentinel_tests(instance, [], priority_tests=priority)

    assert "test_nonexistent (tests.test_missing.MissingTest)" not in selected
    assert len(selected) == 3  # still fills all slots from regular candidates


def test_priority_tests_empty_list():
    """Empty priority list behaves like no priority (backward compat)."""
    instance = _make_instance(_P2P_TESTS)

    with patch("quick_judge._parse_pass_to_pass", return_value=_P2P_TESTS):
        baseline = select_sentinel_tests(instance, [])
        with_empty = select_sentinel_tests(instance, [], priority_tests=[])

    assert baseline == with_empty


def test_priority_tests_none():
    """None priority behaves like no priority (backward compat)."""
    instance = _make_instance(_P2P_TESTS)

    with patch("quick_judge._parse_pass_to_pass", return_value=_P2P_TESTS):
        baseline = select_sentinel_tests(instance, [])
        with_none = select_sentinel_tests(instance, [], priority_tests=None)

    assert baseline == with_none


def test_priority_mixed_with_changed_file_matching():
    """Priority tests + changed file matching: priority first, then matches."""
    instance = _make_instance(_P2P_TESTS)
    priority = ["test_epsilon (tests.test_admin.AdminTest)"]
    changed = ["models.py"]  # matches test_alpha's "ModelTest"

    with patch("quick_judge._parse_pass_to_pass", return_value=_P2P_TESTS):
        selected = select_sentinel_tests(instance, changed, priority_tests=priority)

    # Priority test always included
    assert "test_epsilon (tests.test_admin.AdminTest)" in selected
    # Changed-file matching test also included (models.py → ModelTest)
    assert "test_alpha (tests.test_models.ModelTest)" in selected
    assert len(selected) <= 3


def test_priority_no_duplicates():
    """Priority tests that also match changed files are not duplicated."""
    instance = _make_instance(_P2P_TESTS)
    priority = ["test_alpha (tests.test_models.ModelTest)"]
    changed = ["models.py"]  # also matches test_alpha

    with patch("quick_judge._parse_pass_to_pass", return_value=_P2P_TESTS):
        selected = select_sentinel_tests(instance, changed, priority_tests=priority)

    # Should appear exactly once
    assert selected.count("test_alpha (tests.test_models.ModelTest)") == 1
    assert len(selected) <= 3


# ── controlled_verify p2p_failing_names ──────────────────────────────────────

def test_cv_parse_f2p_p2p_returns_failing_names():
    """_parse_f2p_p2p returns p2p_failing_names as 5th element."""
    from controlled_verify import _parse_f2p_p2p

    output = (
        "test_ok (tests.test_models.ModelTest) ... ok\n"
        "test_fail (tests.test_views.ViewTest) ... FAIL\n"
        "test_target (tests.test_foo.FooTest) ... ok\n"
    )
    f2p = ["test_target (tests.test_foo.FooTest)"]
    p2p = [
        "test_ok (tests.test_models.ModelTest)",
        "test_fail (tests.test_views.ViewTest)",
    ]

    result = _parse_f2p_p2p(output, f2p, p2p)
    assert len(result) == 5
    f2p_pass, f2p_fail, p2p_pass, p2p_fail, p2p_names = result
    assert f2p_pass == 1
    assert f2p_fail == 0
    assert p2p_pass == 1
    assert p2p_fail == 1
    assert p2p_names == ["test_fail (tests.test_views.ViewTest)"]


def test_cv_parse_f2p_p2p_no_regression():
    """When all P2P tests pass, p2p_failing_names is empty."""
    from controlled_verify import _parse_f2p_p2p

    output = (
        "test_a (tests.test_models.ModelTest) ... ok\n"
        "test_b (tests.test_views.ViewTest) ... ok\n"
    )
    p2p = [
        "test_a (tests.test_models.ModelTest)",
        "test_b (tests.test_views.ViewTest)",
    ]

    result = _parse_f2p_p2p(output, [], p2p)
    _, _, _, _, p2p_names = result
    assert p2p_names == []


def test_cv_parse_f2p_p2p_empty_output():
    """Empty output returns empty p2p_failing_names."""
    from controlled_verify import _parse_f2p_p2p

    result = _parse_f2p_p2p("", ["test_f2p"], ["test_p2p"])
    assert len(result) == 5
    _, _, _, _, p2p_names = result
    assert p2p_names == []
