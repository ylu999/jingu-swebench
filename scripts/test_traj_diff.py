#!/usr/bin/env python3
"""Tests for traj_diff.py — divergence detector.

Run: python -m pytest scripts/test_traj_diff.py -v
  or: cd scripts && python test_traj_diff.py
"""

from __future__ import annotations

import sys
import os

# Ensure scripts/ is on path
sys.path.insert(0, os.path.dirname(__file__))

from traj_diff import (
    DivergencePoint,
    StepSummary,
    extract_steps,
    find_divergence,
    compare_trajs,
    format_comparison,
)


# ---------------------------------------------------------------------------
# Helpers — build synthetic trajs
# ---------------------------------------------------------------------------

def _make_assistant_msg(content: str, actions: list[dict] | None = None) -> dict:
    msg: dict = {"role": "assistant", "content": content}
    if actions:
        msg["extra"] = {"actions": actions}
    return msg


def _make_user_msg(content: str = "ok") -> dict:
    return {"role": "user", "content": content}


def _make_tool_msg(content: str = "done") -> dict:
    return {"role": "tool", "content": content}


def _bash_action(cmd: str) -> dict:
    return {"command": cmd}


def _tool_action(name: str, **kwargs: str) -> dict:
    return {"tool": name, "input": kwargs}


def _build_traj(
    steps: list[tuple[str, list[dict] | None]],
    files_written: list[str] | None = None,
    submission: str = "",
    exit_status: str = "submitted",
    phase_records: list[dict] | None = None,
) -> dict:
    """Build a minimal traj dict from a list of (content, actions) tuples."""
    messages: list[dict] = [{"role": "system", "content": "You are a coding agent."}]
    for content, actions in steps:
        messages.append(_make_assistant_msg(content, actions))
        messages.append(_make_tool_msg("ok"))
        messages.append(_make_user_msg())

    jb: dict = {
        "files_written": files_written or [],
        "phase_records": phase_records or [],
    }

    return {
        "messages": messages,
        "info": {"exit_status": exit_status, "submission": submission},
        "jingu_body": jb,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_identical_trajs_no_divergence():
    """Identical trajectories should return None divergence."""
    steps = [
        ("PHASE: OBSERVE\nLet me look at the code.", [_bash_action("cat file.py")]),
        ("PHASE: ANALYZE\nThe bug is in line 42.", [_tool_action("str_replace_editor", path="/testbed/file.py")]),
        ("PHASE: EXECUTE\nFixed.", [_bash_action("git diff")]),
    ]
    orig = _build_traj(steps)
    repl = _build_traj(steps)

    result = find_divergence(orig, repl)
    assert result is None, f"Expected None, got {result}"

    comp = compare_trajs(orig, repl)
    assert comp["divergence"] is None
    assert comp["original_steps"] == 3
    assert comp["replayed_steps"] == 3
    print("PASS: test_identical_trajs_no_divergence")


def test_tool_choice_divergence():
    """Different tool calls at step 3 should be detected."""
    common = [
        ("PHASE: OBSERVE\nLet me look.", [_bash_action("cat file.py")]),
        ("PHASE: ANALYZE\nAnalyzing.", [_bash_action("grep -r 'bug' .")]),
    ]
    orig_steps = common + [
        ("PHASE: EXECUTE\nEditing.", [_tool_action("str_replace_editor", path="/testbed/a.py")]),
    ]
    repl_steps = common + [
        ("PHASE: EXECUTE\nRunning tests.", [_bash_action("python -m pytest tests/")]),
    ]

    orig = _build_traj(orig_steps)
    repl = _build_traj(repl_steps)

    div = find_divergence(orig, repl)
    assert div is not None, "Expected divergence"
    assert div.step_n == 3
    assert div.category == "tool_choice"
    print(f"PASS: test_tool_choice_divergence (step={div.step_n}, cat={div.category})")


def test_tool_args_divergence():
    """Same tool but different file targets."""
    common = [
        ("PHASE: OBSERVE\nLooking.", [_bash_action("ls")]),
    ]
    orig_steps = common + [
        ("PHASE: EXECUTE\nEditing a.py.", [_tool_action("str_replace_editor", path="/testbed/models/a.py")]),
    ]
    repl_steps = common + [
        ("PHASE: EXECUTE\nEditing z.py.", [_tool_action("str_replace_editor", path="/testbed/views/z.py")]),
    ]

    orig = _build_traj(orig_steps)
    repl = _build_traj(repl_steps)

    div = find_divergence(orig, repl)
    assert div is not None
    assert div.step_n == 2
    assert div.category == "tool_args"
    print(f"PASS: test_tool_args_divergence (step={div.step_n}, cat={div.category})")


def test_length_divergence():
    """Different number of steps."""
    short_steps = [
        ("PHASE: OBSERVE\nLooking.", [_bash_action("cat file.py")]),
        ("PHASE: EXECUTE\nDone.", [_bash_action("git diff")]),
    ]
    long_steps = short_steps + [
        ("Running more tests.", [_bash_action("python -m pytest")]),
        ("One more fix.", [_tool_action("str_replace_editor", path="/testbed/fix.py")]),
    ]

    orig = _build_traj(short_steps)
    repl = _build_traj(long_steps)

    div = find_divergence(orig, repl)
    assert div is not None
    assert div.category == "length"
    assert div.step_n == 3  # first step after short one ends
    print(f"PASS: test_length_divergence (step={div.step_n}, cat={div.category})")


def test_early_stop_divergence():
    """One trajectory submits, the other keeps going."""
    common = [
        ("PHASE: OBSERVE\nLooking.", [_bash_action("cat file.py")]),
    ]
    orig_steps = common + [
        ("Submitting.", [_tool_action("submit", patch="diff")]),
    ]
    repl_steps = common + [
        ("Submitting.", [_tool_action("submit", patch="diff")]),
        ("More work.", [_bash_action("echo extra")]),
    ]

    orig = _build_traj(orig_steps, submission="diff patch")
    repl = _build_traj(repl_steps)

    div = find_divergence(orig, repl)
    assert div is not None
    assert div.category == "early_stop"  # orig submitted
    print(f"PASS: test_early_stop_divergence (step={div.step_n}, cat={div.category})")


def test_reasoning_divergence():
    """One has tool call, other has text only."""
    common = [
        ("PHASE: OBSERVE\nLooking.", [_bash_action("cat file.py")]),
    ]
    orig_steps = common + [
        ("PHASE: ANALYZE\nThinking deeply about the problem...", None),  # text only
    ]
    repl_steps = common + [
        ("PHASE: ANALYZE\nLet me check.", [_bash_action("grep -r pattern .")]),
    ]

    orig = _build_traj(orig_steps)
    repl = _build_traj(repl_steps)

    div = find_divergence(orig, repl)
    assert div is not None
    assert div.step_n == 2
    assert div.category == "reasoning"
    print(f"PASS: test_reasoning_divergence (step={div.step_n}, cat={div.category})")


def test_compare_trajs_full():
    """Full compare_trajs returns all expected fields."""
    orig = _build_traj(
        [("PHASE: OBSERVE\nLooking.", [_bash_action("cat file.py")])],
        files_written=["/testbed/fix.py"],
        submission="diff --git a/fix.py",
        phase_records=[{"phase": "OBSERVE"}, {"phase": "EXECUTE"}],
    )
    repl = _build_traj(
        [("PHASE: OBSERVE\nLooking.", [_bash_action("cat file.py")])],
        files_written=["/testbed/fix.py", "/testbed/test.py"],
        submission="diff --git a/fix.py",
        phase_records=[{"phase": "OBSERVE"}, {"phase": "ANALYZE"}, {"phase": "EXECUTE"}],
    )

    result = compare_trajs(orig, repl)
    assert "divergence" in result
    assert "original_steps" in result
    assert "replayed_steps" in result
    assert "original_phases" in result
    assert "replayed_phases" in result
    assert "original_files_modified" in result
    assert "replayed_files_modified" in result
    assert "original_submitted" in result
    assert "replayed_submitted" in result
    assert result["original_submitted"] is True
    assert result["original_phases"] == ["OBSERVE", "EXECUTE"]
    assert result["replayed_phases"] == ["OBSERVE", "ANALYZE", "EXECUTE"]
    print("PASS: test_compare_trajs_full")


def test_format_comparison_output():
    """format_comparison returns a non-empty string."""
    orig = _build_traj(
        [
            ("PHASE: OBSERVE\nLooking.", [_bash_action("cat file.py")]),
            ("PHASE: EXECUTE\nFixing.", [_tool_action("str_replace_editor", path="/testbed/a.py")]),
        ],
        files_written=["/testbed/a.py"],
    )
    repl = _build_traj(
        [
            ("PHASE: OBSERVE\nLooking.", [_bash_action("cat file.py")]),
            ("PHASE: EXECUTE\nFixing differently.", [_bash_action("sed -i 's/old/new/' /testbed/b.py")]),
        ],
        files_written=["/testbed/b.py"],
    )

    result = compare_trajs(orig, repl)
    output = format_comparison(result)
    assert len(output) > 100, f"Output too short: {len(output)} chars"
    assert "DIVERGENCE" in output
    assert "step" in output.lower()
    print("PASS: test_format_comparison_output")
    print()
    print("--- Sample output ---")
    print(output)


def test_empty_trajs():
    """Empty trajs should not crash."""
    orig = {"messages": [], "info": {}, "jingu_body": {}}
    repl = {"messages": [], "info": {}, "jingu_body": {}}

    result = compare_trajs(orig, repl)
    assert result["divergence"] is None
    assert result["original_steps"] == 0
    assert result["replayed_steps"] == 0
    print("PASS: test_empty_trajs")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_identical_trajs_no_divergence()
    test_tool_choice_divergence()
    test_tool_args_divergence()
    test_length_divergence()
    test_early_stop_divergence()
    test_reasoning_divergence()
    test_compare_trajs_full()
    test_format_comparison_output()
    test_empty_trajs()
    print()
    print("All tests passed.")
