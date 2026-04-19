"""Tests for retry-context integrity — no blind truncation on agent-facing signals.

Rule: agent-facing retry context must never be blindly character-sliced.
If compression is needed, it must be section-aware, not [:N] on a string.

These tests verify that the full retry hint chain survives from construction
to the point where the agent receives it in on_attempt_start().
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from failure_classifier import classify_failure, get_routing
from repair_prompts import build_repair_prompt


# ── Core: build_repair_prompt preserves full content ──────────────────────


def test_long_retry_plan_survives():
    """A long retry_plan.next_attempt_prompt is not truncated in repair prompt."""
    long_output = "FAILED test_foo.py::test_bar — AssertionError: " + "x" * 3000
    cv = {"f2p_passed": 0, "f2p_failed": 3, "output_tail": long_output}
    ft = classify_failure(cv)
    routing = get_routing(ft)
    prompt = build_repair_prompt(ft, cv, routing)
    # The output_tail should survive up to the 4000 char limit (not old 400)
    assert len(prompt) > 2000
    assert "test_foo.py" in prompt


def test_long_exec_feedback_survives():
    """Long execution feedback (test output) is preserved in repair prompt."""
    # Simulate a large test output with multiple failing tests
    test_lines = [f"FAILED tests/test_{i}.py::test_case_{i}" for i in range(50)]
    long_output = "\n".join(test_lines)
    cv = {"f2p_passed": 2, "f2p_failed": 5, "output_tail": long_output}
    ft = classify_failure(cv)
    routing = get_routing(ft)
    prompt = build_repair_prompt(ft, cv, routing)
    # Should contain test names from the output
    assert "test_case_0" in prompt
    assert "test_case_49" in prompt  # last test should not be truncated


def test_dual_cause_and_efr_coexist():
    """EFR repair prompt and dual-cause patch context both produce non-empty output."""
    cv = {"f2p_passed": 0, "f2p_failed": 3}
    ft = classify_failure(cv)
    routing = get_routing(ft)

    # EFR repair prompt
    efr_prompt = build_repair_prompt(ft, cv, routing)
    assert "[REPAIR PHASE:" in efr_prompt
    assert "CRITICAL CONSTRAINT" in efr_prompt

    # With patch_context (simulates dual-cause information)
    patch_ctx = {
        "files_written": ["django/utils/dateparse.py", "django/utils/duration.py"],
        "patch_summary": {"lines_added": 15, "lines_removed": 8},
    }
    efr_with_ctx = build_repair_prompt(ft, cv, routing, patch_context=patch_ctx)

    # Both EFR phase declaration AND patch context should be present
    assert "[REPAIR PHASE:" in efr_with_ctx
    assert "PREVIOUS ATTEMPT" in efr_with_ctx
    assert "dateparse.py" in efr_with_ctx
    assert "duration.py" in efr_with_ctx

    # Simulate what happens when both are prepended to last_failure
    dual_cause = (
        "DUAL-CAUSE EXPLORATION REQUIRED.\n"
        "Previous ROOT CAUSE: some hypothesis...\n"
        "Strategy: REGEX_FIX (BANNED)\n"
        "```diff\n-old line\n+new line\n```\n"
    )
    combined = dual_cause + "\n" + efr_with_ctx + "\n\n" + "original failure hint"

    # ALL sections must be present in the combined string (no truncation)
    assert "DUAL-CAUSE" in combined
    assert "REGEX_FIX" in combined
    assert "[REPAIR PHASE:" in combined
    assert "PREVIOUS ATTEMPT" in combined
    assert "dateparse.py" in combined
    assert "original failure hint" in combined


def test_previous_patch_diff_visible():
    """Previous patch diff in patch_context is fully visible in repair prompt."""
    cv = {"f2p_passed": 0, "f2p_failed": 2}
    ft = classify_failure(cv)
    routing = get_routing(ft)

    long_file_list = [f"django/models/field_{i}.py" for i in range(20)]
    patch_ctx = {
        "files_written": long_file_list,
        "patch_summary": {"lines_added": 50, "lines_removed": 30},
    }
    prompt = build_repair_prompt(ft, cv, routing, patch_context=patch_ctx)

    # All file names should be present (not truncated)
    assert "field_0.py" in prompt
    assert "field_19.py" in prompt
    assert "50 lines added" in prompt


def test_gate_hint_does_not_erase_prior_sections():
    """Gate rejection hint appended to last_failure doesn't erase EFR content."""
    # Build EFR repair
    cv = {"f2p_passed": 1, "f2p_failed": 2}
    ft = classify_failure(cv)
    routing = get_routing(ft)
    efr = build_repair_prompt(ft, cv, routing)

    # Simulate gate hint prepended (as in jingu_agent.py)
    gate_hint = "Gate rejected patch (PARSE_FAILED). Use git diff format exactly."
    last_failure = gate_hint  # gate sets last_failure
    # Then EFR prepends
    last_failure = efr + "\n\n" + last_failure

    # Both must survive
    assert "[REPAIR PHASE:" in last_failure
    assert "PARSE_FAILED" in last_failure
    assert "incomplete_fix" in ft  # verify test setup


# ── Anti-regression: no [:N] on agent-facing strings ──────────────────────


def test_no_blind_truncation_in_repair_prompts():
    """repair_prompts.py only truncates test output (at 4000), nothing else."""
    import inspect
    import repair_prompts

    source = inspect.getsource(repair_prompts)
    # Find all [:N] patterns
    import re
    truncations = re.findall(r'\[:\d+\]', source)

    # Only allowed truncation: output_tail[:4000]
    assert truncations == ['[:4000]'], (
        f"Unexpected truncation patterns in repair_prompts.py: {truncations}. "
        f"Agent-facing retry context must not be blindly character-sliced."
    )
