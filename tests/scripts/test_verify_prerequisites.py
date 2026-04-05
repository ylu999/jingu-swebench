"""
test_verify_prerequisites.py — Tests for p192 unified prerequisite gate.

Tests cover:
  - both prerequisites pass -> (True, "")
  - cognition gate fail -> (False, "cognition_fail")
  - empty patch (judge fail) -> (False, "empty_patch")
  - patch format error (judge fail) -> (False, "patch_format_error")
  - semantic weakening (judge fail) -> (False, "semantic_weakening")
  - exception in judge object -> (True, "") conservative fallback
  - cognition pass + judge pass -> (True, "")
  - cognition None + judge None -> (True, "") no-op case
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

import pytest
from run_with_jingu_gate import _verify_prerequisites


# ── Minimal fake JudgeResult for testing ─────────────────────────────────────

class _FakeJudge:
    """Minimal stand-in for InLoopJudgeResult."""
    def __init__(
        self,
        all_pass: bool = True,
        patch_non_empty: bool = True,
        patch_format: bool = True,
        no_semantic_weakening: bool = True,
        changed_file_relevant: bool = True,
    ):
        self.all_pass = all_pass
        self.patch_non_empty = patch_non_empty
        self.patch_format = patch_format
        self.no_semantic_weakening = no_semantic_weakening
        self.changed_file_relevant = changed_file_relevant


class _BrokenJudge:
    """Judge object that raises AttributeError on any attribute access."""
    @property
    def all_pass(self):
        raise AttributeError("broken judge")


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_prereq_pass_both_none():
    """No cognition result, no judge result -> pass (no-op case)."""
    ok, reason = _verify_prerequisites(cognition_result=None, judge_result=None)
    assert ok is True
    assert reason == ""


def test_prereq_pass_both_ok():
    """Cognition pass, judge all_pass -> (True, '')."""
    judge = _FakeJudge(all_pass=True)
    ok, reason = _verify_prerequisites(cognition_result="pass", judge_result=judge)
    assert ok is True
    assert reason == ""


def test_prereq_fail_cognition():
    """Cognition fail -> (False, 'cognition_fail'), even if judge is fine."""
    judge = _FakeJudge(all_pass=True)
    ok, reason = _verify_prerequisites(cognition_result="fail", judge_result=judge)
    assert ok is False
    assert reason == "cognition_fail"


def test_prereq_fail_cognition_no_judge():
    """Cognition fail with no judge result -> (False, 'cognition_fail')."""
    ok, reason = _verify_prerequisites(cognition_result="fail", judge_result=None)
    assert ok is False
    assert reason == "cognition_fail"


def test_prereq_fail_empty_patch():
    """Judge fails: patch_non_empty=False -> (False, 'empty_patch')."""
    judge = _FakeJudge(
        all_pass=False,
        patch_non_empty=False,
        patch_format=True,
        no_semantic_weakening=True,
    )
    ok, reason = _verify_prerequisites(cognition_result="pass", judge_result=judge)
    assert ok is False
    assert reason == "empty_patch"


def test_prereq_fail_patch_format():
    """Judge fails: patch_format=False -> (False, 'patch_format_error')."""
    judge = _FakeJudge(
        all_pass=False,
        patch_non_empty=True,
        patch_format=False,
        no_semantic_weakening=True,
    )
    ok, reason = _verify_prerequisites(cognition_result="pass", judge_result=judge)
    assert ok is False
    assert reason == "patch_format_error"


def test_prereq_fail_semantic_weakening():
    """Judge fails: no_semantic_weakening=False -> (False, 'semantic_weakening')."""
    judge = _FakeJudge(
        all_pass=False,
        patch_non_empty=True,
        patch_format=True,
        no_semantic_weakening=False,
    )
    ok, reason = _verify_prerequisites(cognition_result=None, judge_result=judge)
    assert ok is False
    assert reason == "semantic_weakening"


def test_prereq_exception_safety():
    """Broken judge object raises AttributeError -> conservative fallback (True, '')."""
    broken = _BrokenJudge()
    ok, reason = _verify_prerequisites(cognition_result=None, judge_result=broken)
    assert ok is True
    assert reason == ""


def test_prereq_cognition_pass_judge_none():
    """Cognition pass, no judge result -> (True, '')."""
    ok, reason = _verify_prerequisites(cognition_result="pass", judge_result=None)
    assert ok is True
    assert reason == ""


def test_prereq_cognition_fail_takes_priority_over_judge():
    """Cognition fail is checked before judge -> reason is cognition_fail."""
    judge = _FakeJudge(
        all_pass=False,
        patch_non_empty=False,
    )
    ok, reason = _verify_prerequisites(cognition_result="fail", judge_result=judge)
    assert ok is False
    assert reason == "cognition_fail"
