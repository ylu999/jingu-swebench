"""Tests for EFR (Execution Feedback Response) telemetry chain.

Verifies the end-to-end flow:
  classify_failure → get_routing → build_repair_prompt → structured output

This tests the data flow that the new [efr-emit/consume/ack] signals observe.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from failure_classifier import classify_failure, get_routing, FailureType
from repair_prompts import build_repair_prompt


# ── Test classify_failure → get_routing → build_repair_prompt chain ──────

_CV_CASES: list[tuple[dict, str, str]] = [
    # (cv_result, expected_failure_type, expected_next_phase)
    ({"verification_kind": "controlled_error"}, "execution_error", "EXECUTE"),
    ({"f2p_passed": 3, "f2p_failed": 2}, "incomplete_fix", "DESIGN"),
    ({"f2p_passed": 0, "f2p_failed": 5}, "wrong_direction", "ANALYZE"),
    ({"f2p_passed": 3, "f2p_failed": 0, "eval_resolved": False}, "verify_gap", "EXECUTE"),
]


@pytest.mark.parametrize("cv,expected_ft,expected_phase", _CV_CASES,
                         ids=[c[1] for c in _CV_CASES])
def test_efr_chain_produces_structured_repair(cv, expected_ft, expected_phase):
    """Each failure type produces a repair prompt with phase declaration + evidence."""
    ft = classify_failure(cv)
    assert ft == expected_ft

    routing = get_routing(ft)
    assert routing["next_phase"] == expected_phase

    prompt = build_repair_prompt(ft, cv, routing)
    assert f"[REPAIR PHASE: {expected_phase}]" in prompt
    assert "Evidence from previous attempt" in prompt
    assert len(prompt) > 50


def test_efr_chain_success_returns_none():
    """Resolved CV returns None failure type — no EFR emission."""
    cv = {"f2p_passed": 5, "f2p_failed": 0, "eval_resolved": True}
    ft = classify_failure(cv)
    assert ft is None


def test_efr_chain_empty_cv_returns_none():
    """Empty/None CV returns None — no EFR emission."""
    assert classify_failure({}) is None
    assert classify_failure(None) is None


def test_repair_prompt_includes_test_output():
    """When output_tail is present, repair prompt includes it as evidence."""
    cv = {"f2p_passed": 0, "f2p_failed": 3, "output_tail": "FAILED tests/test_foo.py::test_bar"}
    ft = classify_failure(cv)
    routing = get_routing(ft)
    prompt = build_repair_prompt(ft, cv, routing)
    assert "test_foo.py" in prompt


def test_repair_prompt_truncates_long_output():
    """Long test output is truncated in repair prompt."""
    cv = {"f2p_passed": 0, "f2p_failed": 1, "output_tail": "x" * 5000}
    ft = classify_failure(cv)
    routing = get_routing(ft)
    prompt = build_repair_prompt(ft, cv, routing)
    assert "..." in prompt  # truncation marker


def test_verify_gap_includes_p2p_regression_info():
    """verify_gap repair prompt includes p2p regression counts and diagnosis."""
    cv = {
        "f2p_passed": 1, "f2p_failed": 0,
        "p2p_passed": 23, "p2p_failed": 1,
        "eval_resolved": False,
        "output_tail": "test_load_empty_dir (migrations.test_loader.LoaderTests) ... FAIL",
    }
    ft = classify_failure(cv)
    assert ft == "verify_gap"
    routing = get_routing(ft)
    prompt = build_repair_prompt(ft, cv, routing)
    assert "P2P regressions: 1" in prompt
    assert "DIAGNOSIS" in prompt
    assert "CORRECT" in prompt  # "Your fix is CORRECT"
    assert "BROKE" in prompt  # "it BROKE an existing test"
    assert "test_load_empty_dir" in prompt  # failing test name preserved


def test_verify_gap_without_p2p_regression():
    """verify_gap with no p2p data still works (backward compat)."""
    cv = {"f2p_passed": 1, "f2p_failed": 0, "eval_resolved": False}
    ft = classify_failure(cv)
    assert ft == "verify_gap"
    routing = get_routing(ft)
    prompt = build_repair_prompt(ft, cv, routing)
    assert "P2P regressions" not in prompt  # no p2p data = no p2p line
    assert "DIAGNOSIS" not in prompt


def test_incomplete_fix_includes_f2p_counts():
    """incomplete_fix repair prompt shows both f2p passed and failed counts."""
    cv = {"f2p_passed": 436, "f2p_failed": 2, "eval_resolved": False}
    ft = classify_failure(cv)
    assert ft == "incomplete_fix"
    routing = get_routing(ft)
    prompt = build_repair_prompt(ft, cv, routing)
    assert "436 passed" in prompt
    assert "2 failed" in prompt


def test_all_failure_types_have_routing():
    """Every FailureType has a corresponding routing rule."""
    for ft in ["wrong_direction", "incomplete_fix", "verify_gap", "execution_error"]:
        routing = get_routing(ft)
        assert "next_phase" in routing
        assert "repair_goal" in routing
        assert routing["next_phase"] in ("ANALYZE", "DESIGN", "EXECUTE", "JUDGE")


def test_wrong_direction_binding_constraint():
    """wrong_direction with patch_context includes binding language and previous files."""
    cv = {"f2p_passed": 0, "f2p_failed": 3}
    ft = classify_failure(cv)
    routing = get_routing(ft)
    patch_ctx = {
        "files_written": ["django/utils/dateparse.py"],
        "patch_summary": {"lines_added": 1, "lines_removed": 1},
    }
    prompt = build_repair_prompt(ft, cv, routing, patch_context=patch_ctx)
    assert "MUST change direction" in prompt or "MUST NOT modify the same file" in prompt
    assert "dateparse.py" in prompt
    assert "PREVIOUS ATTEMPT" in prompt


def test_wrong_direction_without_patch_context():
    """wrong_direction without patch_context still works (backward compat)."""
    cv = {"f2p_passed": 0, "f2p_failed": 3}
    ft = classify_failure(cv)
    routing = get_routing(ft)
    prompt = build_repair_prompt(ft, cv, routing)
    assert "ENFORCEMENT" in prompt
    assert "PREVIOUS ATTEMPT" not in prompt


def test_wrong_direction_includes_root_cause_and_strategy():
    """wrong_direction with root cause and strategy shows them in repair prompt."""
    cv = {"f2p_passed": 0, "f2p_failed": 3}
    ft = classify_failure(cv)
    routing = get_routing(ft)
    patch_ctx = {
        "files_written": ["django/utils/dateparse.py"],
        "patch_summary": {"lines_added": 5, "lines_removed": 2},
        "prev_root_cause": "The regex pattern in parse_duration does not handle negative values",
        "prev_strategy_type": "REGEX_FIX",
    }
    prompt = build_repair_prompt(ft, cv, routing, patch_context=patch_ctx)
    assert "PROVEN WRONG" in prompt
    assert "parse_duration" in prompt  # root cause content visible
    assert "REGEX_FIX" in prompt  # strategy visible
    assert "dateparse.py" in prompt  # files visible
    assert "REJECT" in prompt  # gate warning present


def test_non_wrong_direction_ignores_patch_context():
    """incomplete_fix does not include patch binding even if context provided."""
    cv = {"f2p_passed": 3, "f2p_failed": 2}
    ft = classify_failure(cv)
    routing = get_routing(ft)
    patch_ctx = {"files_written": ["foo.py"], "patch_summary": {}}
    prompt = build_repair_prompt(ft, cv, routing, patch_context=patch_ctx)
    assert "PREVIOUS ATTEMPT" not in prompt
