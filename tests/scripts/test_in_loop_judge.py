"""
test_in_loop_judge.py — Tests for p191 in-loop judge (pre-controlled_verify checks).

Tests cover:
  - empty patch → patch_non_empty=False, all_pass=False
  - valid patch → all checks pass, all_pass=True
  - semantic weakening (pytest.skip) → no_semantic_weakening=False
  - invalid patch format (no @@) → patch_format=False
  - exception safety → internal error returns all-pass result
  - changed_file_relevant: only test files changed → False
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

import pytest
from in_loop_judge import run_in_loop_judge, InLoopJudgeResult


# ── Fixture: a minimal valid unified diff ─────────────────────────────────────

_VALID_PATCH = """\
diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py
--- a/django/db/models/sql/compiler.py
+++ b/django/db/models/sql/compiler.py
@@ -100,6 +100,7 @@ class SQLCompiler:
     def as_sql(self):
-        old_line = True
+        new_line = False
+        extra_line = True
"""

_WEAKENING_PATCH = """\
diff --git a/tests/test_models.py b/tests/test_models.py
--- a/tests/test_models.py
+++ b/tests/test_models.py
@@ -10,4 +10,5 @@ class TestModels:
     def test_something(self):
-        self.assertEqual(result, expected)
+        pytest.skip("disabled temporarily")
"""

_FORMAT_INVALID_PATCH = """\
This is not a valid diff.
No diff markers at all.
Just random text.
"""

_ONLY_TEST_FILES_PATCH = """\
diff --git a/tests/test_models.py b/tests/test_models.py
--- a/tests/test_models.py
+++ b/tests/test_models.py
@@ -10,4 +10,5 @@ class TestModels:
     def test_something(self):
-        old_assertion()
+        new_assertion()
"""


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestInLoopJudgeEmptyPatch:
    def test_empty_string(self):
        """Empty string → patch_non_empty=False, all_pass=False."""
        result = run_in_loop_judge("")
        assert result.patch_non_empty is False
        assert result.all_pass is False

    def test_whitespace_only(self):
        """Whitespace-only → patch_non_empty=False."""
        result = run_in_loop_judge("   \n  \n  ")
        assert result.patch_non_empty is False
        assert result.all_pass is False

    def test_none_treated_as_empty(self):
        """None → treated as empty patch → patch_non_empty=False."""
        result = run_in_loop_judge(None)
        assert result.patch_non_empty is False
        assert result.all_pass is False


class TestInLoopJudgeValidPatch:
    def test_valid_patch_passes_all(self):
        """Valid patch with source file change → all checks pass."""
        result = run_in_loop_judge(_VALID_PATCH)
        assert result.patch_non_empty is True
        assert result.patch_format is True
        assert result.no_semantic_weakening is True
        assert result.changed_file_relevant is True
        assert result.all_pass is True


class TestInLoopJudgePatchFormat:
    def test_format_invalid_no_markers(self):
        """Patch with no diff markers → patch_format=False."""
        result = run_in_loop_judge(_FORMAT_INVALID_PATCH)
        assert result.patch_non_empty is True   # has content
        assert result.patch_format is False
        assert result.all_pass is False

    def test_format_valid_requires_hunk(self):
        """Patch with --- and +++ but no @@ → patch_format=False."""
        patch_no_hunk = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
-old line
+new line
"""
        result = run_in_loop_judge(patch_no_hunk)
        assert result.patch_format is False


class TestInLoopJudgeSemanticWeakening:
    def test_pytest_skip_detected(self):
        """Patch adding pytest.skip → no_semantic_weakening=False."""
        result = run_in_loop_judge(_WEAKENING_PATCH)
        assert result.no_semantic_weakening is False
        assert result.all_pass is False

    def test_pytest_mark_skip_detected(self):
        """Patch adding @pytest.mark.skip → detected."""
        patch = """\
diff --git a/tests/test_models.py b/tests/test_models.py
--- a/tests/test_models.py
+++ b/tests/test_models.py
@@ -5,3 +5,4 @@ class TestModels:
+@pytest.mark.skip
 def test_something(self):
"""
        result = run_in_loop_judge(patch)
        assert result.no_semantic_weakening is False

    def test_except_pass_detected(self):
        """Patch adding bare except:pass → detected as weakening."""
        patch = """\
diff --git a/django/foo.py b/django/foo.py
--- a/django/foo.py
+++ b/django/foo.py
@@ -10,3 +10,5 @@ def do_thing():
-    raise ValueError()
+    try:
+        risky()
+    except:
+        pass
"""
        result = run_in_loop_judge(patch)
        assert result.no_semantic_weakening is False

    def test_clean_patch_not_flagged(self):
        """Patch with no weakening patterns → no_semantic_weakening=True."""
        result = run_in_loop_judge(_VALID_PATCH)
        assert result.no_semantic_weakening is True

    def test_only_removed_skip_not_flagged(self):
        """Removing (not adding) pytest.skip → not flagged (only added lines checked)."""
        patch = """\
diff --git a/tests/test_models.py b/tests/test_models.py
--- a/tests/test_models.py
+++ b/tests/test_models.py
@@ -10,4 +10,3 @@ class TestModels:
     def test_something(self):
-        pytest.skip("old skip")
+        self.assertEqual(result, expected)
"""
        result = run_in_loop_judge(patch)
        assert result.no_semantic_weakening is True


class TestInLoopJudgeChangedFileRelevant:
    def test_only_test_files_changed_is_soft_check(self):
        """
        Only test files changed → changed_file_relevant=False.
        p204: changed_file_relevant promoted to hard check — all_pass is False.
        """
        result = run_in_loop_judge(_ONLY_TEST_FILES_PATCH)
        assert result.changed_file_relevant is False
        # Hard checks: format, non-empty, no-weakening still pass
        assert result.patch_non_empty is True
        assert result.patch_format is True
        assert result.no_semantic_weakening is True
        # p204: changed_file_relevant is now a hard check — blocks all_pass
        assert result.all_pass is False

    def test_source_file_changed(self):
        """Source file (non-test .py) changed → changed_file_relevant=True."""
        result = run_in_loop_judge(_VALID_PATCH)
        assert result.changed_file_relevant is True

    def test_no_files_extractable_returns_true(self):
        """Patch with +++ b/ present → files extractable → True for source file."""
        result = run_in_loop_judge(_VALID_PATCH)
        assert result.changed_file_relevant is True


class TestInLoopJudgeExceptionSafety:
    def test_returns_all_pass_on_invalid_input(self):
        """
        Exception safety: if internal logic errors, result should be all-pass
        (conservative — don't block main flow on judge failure).
        """
        # Use a very unusual input that might trigger edge cases
        # The real safety net is the try/except in run_in_loop_judge()
        # Test with a non-string (would raise in real code but caught)
        # Since Python is typed, test with a bizarre string instead
        weird_patch = "\x00\x01\x02 binary content \xff\xfe"
        # Should not raise
        result = run_in_loop_judge(weird_patch)
        assert isinstance(result, InLoopJudgeResult)
