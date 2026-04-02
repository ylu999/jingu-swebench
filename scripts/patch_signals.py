"""
patch_signals.py — Python port of jingu-cognition/src/patch-signals.ts

Extracts structural signals from a unified diff patch.
Signals are deterministic (no LLM). False negatives acceptable; false positives not.

Signals:
  is_normalization   — patch touches only whitespace/import order/formatting
  is_single_line_fix — patch changes exactly one logical line (+1 / -1 pair)
  is_broad_change    — patch touches 5+ files or 20+ lines
  is_comment_only    — patch adds/removes only comment lines
"""

import re


def extract_patch_signals(patch_text: str) -> list[str]:
    """
    Returns list of signal strings present in the patch.
    Returns [] for empty patch.
    """
    if not patch_text or not patch_text.strip():
        return []

    signals: list[str] = []
    lines = patch_text.splitlines()

    # Count files changed
    file_count = sum(1 for l in lines if l.startswith("diff --git"))

    # Collect added/removed lines (excluding diff metadata)
    added = [l[1:] for l in lines if l.startswith("+") and not l.startswith("+++")]
    removed = [l[1:] for l in lines if l.startswith("-") and not l.startswith("---")]

    total_changes = len(added) + len(removed)

    # is_normalization: all changed lines are blank, whitespace-only, or import
    if total_changes > 0:
        def is_normalizing(line: str) -> bool:
            stripped = line.strip()
            return (
                stripped == ""
                or re.match(r"^(import|from)\s", stripped) is not None
            )
        if all(is_normalizing(l) for l in added + removed):
            signals.append("is_normalization")

    # is_single_line_fix: exactly one added line and one removed line
    if len(added) == 1 and len(removed) == 1:
        signals.append("is_single_line_fix")

    # is_broad_change: 5+ files or 20+ changed lines
    if file_count >= 5 or total_changes >= 20:
        signals.append("is_broad_change")

    # is_comment_only: all changed lines are comments
    def is_comment(line: str) -> bool:
        stripped = line.strip()
        return stripped.startswith("#") or stripped.startswith("//") or stripped.startswith("*")

    if total_changes > 0 and all(is_comment(l) for l in added + removed):
        signals.append("is_comment_only")

    return signals


if __name__ == "__main__":
    # Smoke tests

    # Empty patch → no signals
    assert extract_patch_signals("") == []
    print("PASS empty patch")

    # Single line fix
    single = """diff --git a/f.py b/f.py
--- a/f.py
+++ b/f.py
@@ -1,1 +1,1 @@
-old_value = 1
+new_value = 2
"""
    sigs = extract_patch_signals(single)
    assert "is_single_line_fix" in sigs, sigs
    print(f"PASS single_line_fix: {sigs}")

    # Comment only
    comment = """diff --git a/f.py b/f.py
--- a/f.py
+++ b/f.py
@@ -1,1 +1,1 @@
-# old comment
+# new comment
"""
    sigs = extract_patch_signals(comment)
    assert "is_comment_only" in sigs, sigs
    print(f"PASS comment_only: {sigs}")

    # Normalization (import only)
    norm = """diff --git a/f.py b/f.py
--- a/f.py
+++ b/f.py
@@ -1,1 +1,1 @@
-import os
+import sys
"""
    sigs = extract_patch_signals(norm)
    assert "is_normalization" in sigs, sigs
    print(f"PASS is_normalization: {sigs}")
