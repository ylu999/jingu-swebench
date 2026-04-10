"""
Unresolved Case Classifier — targeted F2P_ALL_FAIL sub-classification.

Replaces the generic "re-examine" hint with actionable, category-specific hints
by analyzing the relationship between agent changes and failing test paths.

Three categories:
  - wrong_direction: agent changed files unrelated to F2P tests
  - insufficient_coverage: some F2P tests pass (partial progress)
  - wrong_abstraction: agent found right file but fix doesn't address root cause

Architecture (p207-P13):
  - Input: jingu_body (from extract_jingu_body) + fail_to_pass test list
  - Output: {"category", "confidence", "hint", "signals"}
  - Consumer: swebench_failure_reroute_pack._build_wrong_direction_hint
"""
from __future__ import annotations

import os
import re
from typing import Optional


def _extract_test_paths(fail_to_pass_tests: list[str]) -> set[str]:
    """
    Extract directory paths from F2P test identifiers.

    Test IDs come in forms like:
      - "tests/utils_tests/test_dateformat.py::DateFormatTests::test_r"
      - "django.test.utils.TestCase.test_foo"
      - "tests/model_fields/test_jsonfield.py"

    Returns set of directory components (e.g., {"tests/utils_tests", "tests/model_fields"}).
    """
    paths: set[str] = set()
    for test_id in fail_to_pass_tests:
        # Handle pytest-style paths: "path/to/test.py::Class::method"
        file_part = test_id.split("::")[0]
        if "/" in file_part:
            dirpath = os.path.dirname(file_part)
            if dirpath:
                paths.add(dirpath)
            # Also add the file itself for precise matching
            paths.add(file_part)
        # Handle dotted module paths: "django.utils.tests.test_foo"
        elif "." in test_id:
            parts = test_id.split(".")
            # Convert dotted path to directory-like path for comparison
            # e.g., "django.utils.tests" → "django/utils/tests"
            for i in range(1, len(parts)):
                paths.add("/".join(parts[:i]))
    return paths


def _paths_overlap(files_written: list[str], test_paths: set[str]) -> bool:
    """
    Check if any written file is related to any test path.

    Uses three strategies:
    1. Direct directory overlap (same path prefix)
    2. Shared path components (at least 2 in common)
    3. Filename matching: test_X.py tests X.py (handles Django's separate test/ layout)
    """
    if not files_written or not test_paths:
        return False

    written_dirs: set[str] = set()
    written_files: set[str] = set()
    written_basenames: set[str] = set()
    for f in files_written:
        written_files.add(f)
        basename = os.path.basename(f)
        written_basenames.add(basename)
        dirpath = os.path.dirname(f)
        if dirpath:
            written_dirs.add(dirpath)
            parts = dirpath.split("/")
            for i in range(1, len(parts) + 1):
                written_dirs.add("/".join(parts[:i]))

    for tp in test_paths:
        # Strategy 1: direct directory/file overlap
        if tp in written_dirs or tp in written_files:
            return True

        # Strategy 2: shared path components (at least 2)
        tp_parts = tp.split("/")
        for wd in written_dirs:
            wd_parts = wd.split("/")
            common = 0
            for a, b in zip(tp_parts, wd_parts):
                if a == b:
                    common += 1
                else:
                    break
            if common >= 2:
                return True

        # Strategy 3: filename matching (test_X.py ↔ X.py)
        # Django pattern: tests/utils_tests/test_dateformat.py tests django/utils/dateformat.py
        tp_basename = os.path.basename(tp)
        if tp_basename.startswith("test_"):
            source_name = tp_basename[5:]  # "test_dateformat.py" → "dateformat.py"
            if source_name in written_basenames:
                return True
        # Reverse: agent wrote test_X.py, test is about X.py
        for wb in written_basenames:
            if wb.startswith("test_"):
                if wb[5:] == tp_basename:
                    return True

    return False


def _extract_test_imports(
    fail_to_pass_tests: list[str],
) -> list[str]:
    """
    Extract likely source module paths from F2P test names.

    Heuristic: test file "tests/utils_tests/test_dateformat.py" likely tests
    "django/utils/dateformat.py". Extract the non-test portion.
    """
    source_paths: list[str] = []
    for test_id in fail_to_pass_tests:
        file_part = test_id.split("::")[0]
        if "/" in file_part:
            # Remove "tests/" prefix variations and "test_" prefix from filename
            cleaned = file_part
            cleaned = re.sub(r'^tests?/', '', cleaned)
            cleaned = re.sub(r'_tests?/', '/', cleaned)
            cleaned = re.sub(r'/test_', '/', cleaned)
            cleaned = re.sub(r'^test_', '', cleaned)
            if cleaned != file_part:
                source_paths.append(cleaned)
    return source_paths


def classify_unresolved(
    jingu_body: dict,
    fail_to_pass_tests: list[str],
) -> dict:
    """
    Classify an F2P_ALL_FAIL case into a specific failure category.

    Args:
        jingu_body: structured body from extract_jingu_body (contains files_written,
                    test_results, patch_summary, verify_history)
        fail_to_pass_tests: list of FAIL_TO_PASS test IDs for this instance

    Returns:
        {
            "category": "wrong_direction" | "insufficient_coverage" | "wrong_abstraction",
            "confidence": float (0.0-1.0),
            "hint": str (actionable, injected into retry prompt),
            "signals": list[str] (evidence trail for observability)
        }
    """
    files_written = jingu_body.get("files_written", [])
    test_results = jingu_body.get("test_results", {})
    patch_summary = jingu_body.get("patch_summary", {})
    controlled_passed = test_results.get("controlled_passed", 0)
    controlled_failed = test_results.get("controlled_failed", 0)

    signals: list[str] = []

    # ── Category B: insufficient_coverage ──────────────────────────────────
    # Some F2P tests pass → partial progress, direction is correct
    if controlled_passed > 0 and controlled_failed > 0:
        signals.append(f"partial_progress: {controlled_passed} passed, {controlled_failed} failed")

        # Get error excerpts from verify_history if available
        error_detail = _get_error_detail(jingu_body)

        failing_names = _format_test_names(fail_to_pass_tests, max_names=5)
        hint = (
            f"[JINGU ROUTING] PARTIAL PROGRESS — {controlled_passed} F2P tests now pass, "
            f"{controlled_failed} still failing. "
            f"Your fix direction is CORRECT but incomplete. "
            f"Focus on the remaining {controlled_failed} failing tests: {failing_names}. "
        )
        if error_detail:
            hint += f"Error: {error_detail[:300]}"

        return {
            "category": "insufficient_coverage",
            "confidence": 0.85,
            "hint": hint,
            "signals": signals,
        }

    # ── Category A vs C: need to check file overlap ───────────────────────
    test_paths = _extract_test_paths(fail_to_pass_tests)
    overlap = _paths_overlap(files_written, test_paths)

    # ── Category A: wrong_direction ────────────────────────────────────────
    # Agent changed files that don't overlap with F2P test paths
    if not overlap and files_written:
        signals.append(f"no_path_overlap: files_written={files_written[:5]}")
        signals.append(f"test_paths={sorted(test_paths)[:5]}")

        source_hints = _extract_test_imports(fail_to_pass_tests)
        test_names = _format_test_names(fail_to_pass_tests, max_names=3)

        hint = (
            f"[JINGU ROUTING] WRONG DIRECTION — Your changes are in the wrong area. "
            f"You modified: {', '.join(files_written[:3])}. "
            f"The failing tests are: {test_names}. "
        )
        if source_hints:
            hint += (
                f"Look at what those tests import and modify THOSE source files "
                f"(likely: {', '.join(source_hints[:3])}). "
            )
        else:
            hint += (
                "Read the failing test code to find which module is under test, "
                "then fix THAT module. "
            )
        hint += "Start fresh — do NOT expand the current patch."

        return {
            "category": "wrong_direction",
            "confidence": 0.9,
            "hint": hint,
            "signals": signals,
        }

    # ── Category C: wrong_abstraction ──────────────────────────────────────
    # Agent changed correct file area, all F2P tests still fail, patch exists
    if overlap and files_written:
        lines_changed = patch_summary.get("lines_added", 0) + patch_summary.get("lines_removed", 0)
        signals.append(f"path_overlap=true, files_written={files_written[:3]}")
        signals.append(f"patch_size: +{patch_summary.get('lines_added', 0)} -{patch_summary.get('lines_removed', 0)}")

        error_detail = _get_error_detail(jingu_body)
        assertion_hint = _get_assertion_hint(jingu_body)

        hint = (
            f"[JINGU ROUTING] WRONG ABSTRACTION — "
            f"You're editing the right file(s) ({', '.join(files_written[:2])}) "
            f"but your fix doesn't address the root cause. "
            f"All {controlled_failed} F2P tests still fail. "
        )
        if assertion_hint:
            hint += f"The test expects: {assertion_hint[:300]}. "
        elif error_detail:
            hint += f"Error: {error_detail[:300]}. "
        hint += (
            "Re-read the failing test assertion carefully. "
            "What behavior does the test expect? Trace that behavior back to the source. "
            "Your current change may be at the wrong abstraction level."
        )

        return {
            "category": "wrong_abstraction",
            "confidence": 0.7 if lines_changed < 20 else 0.6,
            "hint": hint,
            "signals": signals,
        }

    # ── Fallback: no files written (agent didn't produce a patch) ─────────
    signals.append("no_files_written")
    test_names = _format_test_names(fail_to_pass_tests, max_names=5)
    hint = (
        f"[JINGU ROUTING] NO PATCH PRODUCED — "
        f"You did not write any code changes. "
        f"The failing tests are: {test_names}. "
        f"Read the test code, identify the module under test, locate the bug, and write a fix."
    )
    return {
        "category": "wrong_direction",
        "confidence": 0.5,
        "hint": hint,
        "signals": signals,
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_error_detail(jingu_body: dict) -> str:
    """Extract error excerpts from verify_history stdout via parse_pytest_output."""
    verify_history = jingu_body.get("verify_history", [])
    if not verify_history:
        return ""

    # Find the most recent controlled_fail_to_pass entry
    stdout = ""
    for entry in reversed(verify_history):
        if entry.get("kind") == "controlled_fail_to_pass":
            stdout = entry.get("stdout", "")
            break
    if not stdout:
        for entry in reversed(verify_history):
            if entry.get("stdout"):
                stdout = entry.get("stdout", "")
                break

    if not stdout:
        return ""

    # Extract assertion/error lines
    error_patterns = [
        re.compile(r'((?:Assertion|Type|Value|Attribute|Key|Import|Runtime)Error[:\s].{10,200})', re.MULTILINE),
        re.compile(r'^E\s+(.+)$', re.MULTILINE),
    ]
    for pat in error_patterns:
        matches = pat.findall(stdout)
        if matches:
            return matches[0].strip()[:300]

    return ""


def _get_assertion_hint(jingu_body: dict) -> str:
    """Extract assertion-specific detail (what the test expects vs got)."""
    verify_history = jingu_body.get("verify_history", [])
    if not verify_history:
        return ""

    stdout = ""
    for entry in reversed(verify_history):
        if entry.get("kind") == "controlled_fail_to_pass":
            stdout = entry.get("stdout", "")
            break

    if not stdout:
        return ""

    # Look for "assert X == Y" or "AssertionError: X != Y" patterns
    patterns = [
        re.compile(r'(assert\w*\s+.{10,200})', re.MULTILINE | re.IGNORECASE),
        re.compile(r'(AssertionError:\s*.{10,200})', re.MULTILINE),
        re.compile(r'(Expected\s+.{10,200})', re.MULTILINE | re.IGNORECASE),
    ]
    for pat in patterns:
        matches = pat.findall(stdout)
        if matches:
            return matches[0].strip()[:300]

    return ""


def _format_test_names(tests: list[str], max_names: int = 5) -> str:
    """Format test names for display, truncating if too many."""
    if not tests:
        return "(none)"
    names = []
    for t in tests[:max_names]:
        # Shorten: "tests/utils_tests/test_dateformat.py::DateFormatTests::test_r" → "test_r (test_dateformat.py)"
        if "::" in t:
            parts = t.split("::")
            short_name = parts[-1]
            file_name = os.path.basename(parts[0]) if "/" in parts[0] else parts[0]
            names.append(f"{short_name} ({file_name})")
        else:
            names.append(t.split(".")[-1] if "." in t else t)
    result = ", ".join(names)
    if len(tests) > max_names:
        result += f" (+{len(tests) - max_names} more)"
    return result
