"""
in_loop_judge.py — p191: in-loop patch judge (pre-controlled_verify checks).

Runs 4 deterministic checks on the patch BEFORE controlled_verify:
  1. patch_non_empty     — patch has content
  2. patch_format        — basic unified diff format valid (---, +++, @@)
  3. no_semantic_weakening — no skip/xfail/broad-condition-weakening patterns
  4. changed_file_relevant — at least one non-test .py file changed

If any check fails:
  - patch_non_empty fail → caller sets early_stop_verdict = VerdictStop(reason="empty_patch")
  - patch_format fail    → caller sets pending_redirect_hint = "[REDIRECT:EXECUTE] patch_format_error"
  - no_semantic_weakening fail → caller sets pending_redirect_hint = "[REDIRECT:ANALYZE] semantic_weakening_detected"
  - changed_file_relevant fail → caller sets pending_redirect_hint = "[REDIRECT:ANALYZE] wrong_file_changed"

Exception safety: run_in_loop_judge() never raises — on any internal error it returns
InLoopJudgeResult with all checks passing (conservative: don't block on judge failure).
"""

import re
from dataclasses import dataclass


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class InLoopJudgeResult:
    patch_non_empty: bool
    patch_format: bool
    no_semantic_weakening: bool
    changed_file_relevant: bool

    @property
    def all_pass(self) -> bool:
        """
        True if all three HARD checks pass.
        changed_file_relevant is a SOFT check (warning only) and does not block.
        Hard checks: patch_non_empty, patch_format, no_semantic_weakening.
        """
        return (
            self.patch_non_empty
            and self.patch_format
            and self.no_semantic_weakening
        )

    def status_str(self, flag: bool) -> str:
        return "pass" if flag else "fail"


# ── Semantic weakening patterns ───────────────────────────────────────────────
#
# Applied only to lines added by the patch (lines starting with +, not +++).
# These patterns detect test-disabling, assertion-broadening, and regex-widening.
# False negatives acceptable (miss some weakening); false positives not acceptable.
#
# Sources:
#   - task doc / common SWE-bench anti-patterns
#   - No existing patterns found in patch_signals.py (signals there are structural)

_SEMANTIC_WEAKENING_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p)
    for p in [
        # Test-skip patterns
        r"pytest\.skip\(",
        r"@pytest\.mark\.skip",
        r"@pytest\.mark\.xfail",
        r"unittest\.skip\(",
        r"unittest\.skipIf\(",
        r"unittest\.skipUnless\(",
        r"raise\s+unittest\.SkipTest\(",
        # Broad exception swallowing — catching all exceptions silences failures
        r"except\s*:\s*pass",
        r"except\s+Exception\s*:\s*pass",
        r"except\s+Exception\s+as\s+\w+\s*:\s*pass",
        # Assertion weakening — asserting True is a no-op
        r"assert\s+True\b",
        r"self\.assertTrue\(True\)",
        # Empty test body replacement
        r"def\s+test_\w+\([^)]*\)\s*:\s*pass",
        # Regex broadening — replacing specific pattern with catch-all
        r're\.(match|search|fullmatch)\([\'"]\.[\*\+][\'"]',
        r'\.replace\([\'"][^\'\"]+[\'"]\s*,\s*[\'\'\"]\s*[\'"]\)',  # replace with empty string
    ]
)


# ── Public entry point ────────────────────────────────────────────────────────

def run_in_loop_judge(patch: str, instance_data: dict | None = None) -> InLoopJudgeResult:
    """
    Run in-loop judge checks on the patch before controlled_verify.

    Returns InLoopJudgeResult with all checks passed on internal error
    (conservative: don't block main flow on judge failure).
    """
    try:
        return _run_checks(patch, instance_data)
    except Exception:
        # Exception safety: judge failure is non-fatal — return all pass
        return InLoopJudgeResult(
            patch_non_empty=True,
            patch_format=True,
            no_semantic_weakening=True,
            changed_file_relevant=True,
        )


# ── Internal implementation ───────────────────────────────────────────────────

def _run_checks(patch: str, instance_data: dict | None) -> InLoopJudgeResult:
    # Check 1: patch_non_empty
    patch_non_empty = bool(patch and patch.strip())
    if not patch_non_empty:
        return InLoopJudgeResult(
            patch_non_empty=False,
            patch_format=False,
            no_semantic_weakening=True,  # can't detect weakening in empty patch
            changed_file_relevant=True,  # can't check files in empty patch
        )

    # Check 2: patch_format_valid
    patch_format = _check_patch_format(patch)

    # Check 3: no_semantic_weakening
    no_semantic_weakening = _check_no_semantic_weakening(patch)

    # Check 4: changed_file_relevant
    changed_file_relevant = _check_changed_file_relevant(patch, instance_data)

    return InLoopJudgeResult(
        patch_non_empty=patch_non_empty,
        patch_format=patch_format,
        no_semantic_weakening=no_semantic_weakening,
        changed_file_relevant=changed_file_relevant,
    )


def _check_patch_format(patch: str) -> bool:
    """
    Basic unified diff format validation.
    Requires: at least one '--- ' line, one '+++ ' line, one '@@ ' hunk header.
    Mirrors gate_runner.js R1 check: PARSE_FAILED if no @@ header.
    """
    lines = patch.splitlines()
    has_minus_header = any(l.startswith("--- ") for l in lines)
    has_plus_header = any(l.startswith("+++ ") for l in lines)
    has_hunk = any(l.startswith("@@") for l in lines)
    return has_minus_header and has_plus_header and has_hunk


def _check_no_semantic_weakening(patch: str) -> bool:
    """
    Check for semantic weakening patterns in added lines only (lines starting with +).
    Returns True if no weakening detected (clean), False if weakening found.
    """
    added_lines = [
        line[1:]  # strip the leading +
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    added_text = "\n".join(added_lines)

    for pattern in _SEMANTIC_WEAKENING_PATTERNS:
        if pattern.search(added_text):
            return False
    return True


def _check_changed_file_relevant(patch: str, instance_data: dict | None) -> bool:
    """
    Check if changed files are likely relevant to the fix.
    Basic version: at least one non-test .py file must be changed.
    If no .py files are changed at all, returns True (can't determine, assume ok).
    """
    # Extract files from '+++ b/path' lines
    changed_files = [
        line[6:].strip()  # strip '+++ b/'
        for line in patch.splitlines()
        if line.startswith("+++ b/")
    ]

    if not changed_files:
        return True  # no file info extractable — assume ok

    # Find .py files
    py_files = [f for f in changed_files if f.endswith(".py")]
    if not py_files:
        return True  # no Python files — might be other lang, assume ok

    # Check if at least one non-test file changed
    # Test files: path contains /tests/ or /test_*.py pattern
    source_files = [
        f for f in py_files
        if "/tests/" not in f
        and "/test_" not in f
        and not f.startswith("test_")
        and not f.endswith("_test.py")
    ]
    return bool(source_files)
