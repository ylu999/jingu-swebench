"""
quick_judge.py — E1: In-loop quick judge (tier 1 verification).

Mid-execution targeted test runner that gives the agent directional signal
during the EXECUTE phase.  This is T1 (advisory, partial) — it does NOT
replace T2 controlled_verify (authoritative, full F2P/P2P oracle).

Components:
  - QuickJudgeResult           structured result of a targeted test run
  - select_targeted_tests()    stable F2P subset selection (max 5)
  - run_quick_judge()          run tests via docker exec, 30s timeout
  - classify_direction()       compare consecutive results
  - format_agent_message()     minimal structured text for agent injection
  - detect_acknowledged()      L2 metric: agent responded to signal
  - detect_effective()         L3 metric: convergence across history

Standalone module — does NOT import from step_monitor_state or jingu_agent.
Exception-safe: run_quick_judge() never raises.
"""

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Literal


# ── Type definitions ─────────────────────────────────────────────────────────

QuickJudgeTriggerSource = Literal[
    "automatic_patch_detected",     # Phase 1: only this enabled
    "automatic_phase_boundary",     # Phase 1: reserved, not enabled
    "agent_requested",              # Phase 3: reserved, not enabled
]

QuickJudgeDirection = Literal[
    "improved",                     # more tests pass than last time
    "regressed",                    # fewer tests pass than last time
    "unchanged",                    # same pass/fail counts
    "inconclusive",                 # tests errored, can't determine direction
    "first_signal",                 # first quick judge this attempt, no baseline
    "likely_right_direction",       # improved + failing tests are a subset of previous
    "likely_wrong_direction",       # regressed OR new failures appeared
]


# ── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class QuickJudgeResult:
    """Structured result of a mid-execution targeted test run."""
    tier: Literal["quick"] = "quick"
    trigger_source: QuickJudgeTriggerSource = "automatic_patch_detected"
    step: int = 0
    tests_targeted: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    tests_error: int = 0
    failing_test_names: list = field(default_factory=list)  # max 3 names
    elapsed_ms: float = 0.0
    direction: QuickJudgeDirection = "first_signal"

    @property
    def all_passed(self):
        return self.tests_failed == 0 and self.tests_error == 0

    @property
    def has_signal(self):
        return self.tests_targeted > 0 and self.tests_error < self.tests_targeted


# ── F2P parsing (standalone, same pattern as jingu_agent._parse_fail_to_pass) ─

def _parse_fail_to_pass(instance):
    """Parse FAIL_TO_PASS from instance dict, handling both list and JSON-string formats."""
    raw = instance.get("FAIL_TO_PASS", [])
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return []


# ── Test selection ───────────────────────────────────────────────────────────

def select_targeted_tests(instance, changed_files):
    """
    Select a stable subset of F2P tests for quick judge (max 5).

    Priority when > 5 tests:
      1. Tests whose name matches a changed file name
      2. Shortest test names first (proxy for simplest/fastest)

    Returns a deterministic sorted list of test IDs.
    """
    f2p = _parse_fail_to_pass(instance)
    if not f2p:
        return []

    if len(f2p) <= 5:
        return sorted(f2p)

    # Build set of changed basenames (without extension) for matching
    changed_basenames = set()
    for fpath in (changed_files or []):
        basename = os.path.basename(fpath)
        name_no_ext = os.path.splitext(basename)[0]
        changed_basenames.add(name_no_ext.lower())

    def _matches_changed(test_id):
        """Check if test ID contains any changed file basename."""
        test_lower = test_id.lower()
        for cb in changed_basenames:
            if cb in test_lower:
                return True
        return False

    # Partition: matching tests first, then rest, both sorted by length then name
    matching = sorted(
        [t for t in f2p if _matches_changed(t)],
        key=lambda t: (len(t), t),
    )
    rest = sorted(
        [t for t in f2p if not _matches_changed(t)],
        key=lambda t: (len(t), t),
    )

    selected = (matching + rest)[:5]
    return sorted(selected)


# ── Direction classification ─────────────────────────────────────────────────

def classify_direction(current, previous):
    """
    Classify the direction of change between consecutive quick judge results.

    Returns a QuickJudgeDirection string.
    """
    if previous is None:
        return "first_signal"

    if current.tests_error >= current.tests_targeted:
        return "inconclusive"

    if current.tests_passed > previous.tests_passed:
        # Check if failures are a strict subset of previous failures
        if set(current.failing_test_names).issubset(set(previous.failing_test_names)):
            return "likely_right_direction"
        return "improved"

    if current.tests_passed < previous.tests_passed:
        return "likely_wrong_direction"

    if current.tests_passed == previous.tests_passed:
        if current.failing_test_names != previous.failing_test_names:
            return "unchanged"  # same count but different tests — no clear direction
        return "unchanged"

    return "inconclusive"


# ── Test runner ──────────────────────────────────────────────────────────────

_QUICK_JUDGE_TIMEOUT_S = 30


def _parse_quick_test_output(output):
    """
    Parse test output to extract pass/fail/error counts and failing test names.

    Returns (passed, failed, errored, failing_names).
    """
    passed = 0
    failed = 0
    errored = 0
    failing_names = []

    # Django unittest format: "test_method (module.ClassName) ... ok/FAIL/ERROR"
    for line in output.split("\n"):
        line = line.strip()
        m = re.match(
            r"^(\w+)\s+\(([^)]+)\)\s+\.\.\.\s+(ok|FAIL|ERROR)", line
        )
        if m:
            test_name = m.group(1)
            test_class = m.group(2)
            status = m.group(3)
            full_id = f"{test_name} ({test_class})"
            if status == "ok":
                passed += 1
            elif status == "ERROR":
                errored += 1
                failing_names.append(full_id)
            else:
                failed += 1
                failing_names.append(full_id)
            continue

        # pytest format: "path/test.py::test_name PASSED/FAILED/ERROR"
        m_pytest = re.match(r"^(.+?)\s+(PASSED|FAILED|ERROR)", line)
        if m_pytest:
            test_id = m_pytest.group(1).strip()
            status = m_pytest.group(2)
            if status == "PASSED":
                passed += 1
            elif status == "ERROR":
                errored += 1
                failing_names.append(test_id)
            else:
                failed += 1
                failing_names.append(test_id)

    # Fallback: if no individual results parsed, try summary lines
    if passed == 0 and failed == 0 and errored == 0:
        # pytest summary: "3 passed, 2 failed"
        m_pass = re.search(r'(\d+) passed', output)
        m_fail = re.search(r'(\d+) failed', output)
        m_err = re.search(r'(\d+) error', output)
        if m_pass:
            passed = int(m_pass.group(1))
        if m_fail:
            failed = int(m_fail.group(1))
        if m_err:
            errored = int(m_err.group(1))

        # unittest summary: "Ran N tests ... OK" or "FAILED (failures=K, errors=E)"
        ran_m = re.search(r'Ran (\d+) tests? in', output)
        if ran_m and not m_pass and not m_fail:
            total = int(ran_m.group(1))
            fail_m = re.search(
                r'FAILED \((?:failures=(\d+))?(?:,\s*)?(?:errors=(\d+))?\)', output
            )
            if fail_m:
                f = int(fail_m.group(1) or 0)
                e = int(fail_m.group(2) or 0)
                failed = f
                errored = e
                passed = max(0, total - f - e)
            else:
                passed = total

    return passed, failed, errored, failing_names[:3]


def _build_quick_test_command(instance, test_ids):
    """
    Build docker exec test command for targeted F2P tests.

    Uses the same test runner as controlled_verify but with specific test IDs.
    """
    from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS

    repo = instance["repo"]
    version = instance["version"]
    specs = MAP_REPO_VERSION_TO_SPECS[repo][version]
    test_cmd = specs["test_cmd"]

    # Extract class-level labels from test IDs for Django runtests.py
    # F2P format: 'test_method (module.submodule.ClassName)'
    classes = set()
    for entry in test_ids:
        m = re.match(r'\w+\s+\((.+)\)', entry)
        if m:
            classes.add(m.group(1))

    if classes:
        labels_str = " ".join(sorted(classes))
    else:
        # Fallback: use test IDs directly (e.g. pytest format)
        labels_str = " ".join(test_ids)

    return (
        "source /opt/miniconda3/bin/activate && "
        "conda activate testbed && "
        "cd /testbed && "
        f"{test_cmd} {labels_str} 2>&1"
    )


def run_quick_judge(patch, instance, container_id, changed_files,
                    previous_result=None, step=0):
    """
    Run a quick judge: targeted F2P test subset in the eval container.

    Exception-safe: on any error, returns a result with tests_error=tests_targeted
    and direction="inconclusive".  Never raises.

    NOTE: This runs tests against the agent's current working tree in /testbed
    (no stash/reset/apply cycle). It is a directional signal, not an eval-aligned
    oracle.  T2 controlled_verify handles the full eval-aligned verification.
    """
    t0 = time.monotonic()

    # Select targeted tests
    try:
        test_ids = select_targeted_tests(instance, changed_files)
    except Exception:
        test_ids = []

    n_targeted = len(test_ids)

    if n_targeted == 0:
        result = QuickJudgeResult(
            step=step,
            tests_targeted=0,
            tests_passed=0,
            tests_failed=0,
            tests_error=0,
            failing_test_names=[],
            elapsed_ms=round((time.monotonic() - t0) * 1000, 1),
            direction="inconclusive",
        )
        return result

    # Build and run test command
    try:
        test_cmd = _build_quick_test_command(instance, test_ids)

        test_result = subprocess.run(
            ["docker", "exec", container_id, "bash", "-c", test_cmd],
            capture_output=True, text=True, timeout=_QUICK_JUDGE_TIMEOUT_S,
        )
        output = (test_result.stdout or "") + (test_result.stderr or "")

        passed, failed, errored, failing_names = _parse_quick_test_output(output)

        # If nothing parsed at all, treat entire run as errored
        if passed == 0 and failed == 0 and errored == 0:
            errored = n_targeted

        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)

        result = QuickJudgeResult(
            step=step,
            tests_targeted=n_targeted,
            tests_passed=passed,
            tests_failed=failed,
            tests_error=errored,
            failing_test_names=failing_names[:3],
            elapsed_ms=elapsed_ms,
            direction="first_signal",  # placeholder, classified below
        )
        result.direction = classify_direction(result, previous_result)
        return result

    except subprocess.TimeoutExpired:
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        return QuickJudgeResult(
            step=step,
            tests_targeted=n_targeted,
            tests_passed=0,
            tests_failed=0,
            tests_error=n_targeted,
            failing_test_names=[],
            elapsed_ms=elapsed_ms,
            direction="inconclusive",
        )
    except Exception:
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        return QuickJudgeResult(
            step=step,
            tests_targeted=n_targeted,
            tests_passed=0,
            tests_failed=0,
            tests_error=n_targeted,
            failing_test_names=[],
            elapsed_ms=elapsed_ms,
            direction="inconclusive",
        )


# ── Agent message formatting ────────────────────────────────────────────────

_DIRECTION_HINTS = {
    "first_signal": "First test signal. {passed}/{targeted} passing.",
    "improved": "Progress: more tests passing than before.",
    "likely_right_direction": "Good direction: failures narrowing.",
    "regressed": "Regression: fewer tests passing. Review your last change.",
    "likely_wrong_direction": "Wrong direction: new failures appeared. Reconsider approach.",
    "unchanged": "No change in test results. Try a different approach.",
    "inconclusive": "Tests could not run. Check for import/syntax errors.",
}


def format_agent_message(result):
    """
    Format a QuickJudgeResult as a minimal structured message for agent injection.

    No raw stdout. Max 3 failing test names. One-sentence direction-based hint.
    """
    hint = _DIRECTION_HINTS.get(
        result.direction,
        "Tests could not run. Check for import/syntax errors.",
    ).format(passed=result.tests_passed, targeted=result.tests_targeted)

    lines = [
        f"[QUICK_CHECK step={result.step}] {result.direction} "
        f"— {result.tests_passed}/{result.tests_targeted} tests passed",
    ]

    if result.failing_test_names:
        names_str = ", ".join(result.failing_test_names[:3])
        lines.append(f"Failing: {names_str}")

    lines.append(hint)
    return "\n".join(lines)


# ── Effectiveness metrics ────────────────────────────────────────────────────

def detect_acknowledged(qj_result, post_injection_assistant_text,
                        post_injection_patch_files):
    """
    L2 metric: detect whether agent acknowledged the quick judge signal.

    Heuristic checks:
      1. Agent text mentions a failing test name (short form)
      2. (Future: agent modifies a file related to failing tests)

    Conservative default: returns False.
    """
    if not qj_result or not qj_result.failing_test_names:
        return False

    text = post_injection_assistant_text or ""
    for test_name in qj_result.failing_test_names:
        # Use last part of test path (e.g. "test_foo" from "test_foo (module.Class)")
        short_name = test_name.split("::")[-1]
        # Also handle Django format: "test_foo (module.Class)" -> "test_foo"
        m = re.match(r'^(\w+)\s+\(', short_name)
        if m:
            short_name = m.group(1)
        if short_name and short_name in text:
            return True

    return False


def detect_effective(quick_judge_history):
    """
    L3 metric: detect convergence across consecutive quick judge results.

    Effective if direction sequence shows at least one transition from
    BAD/unchanged/first_signal to GOOD (improved or likely_right_direction).
    """
    if len(quick_judge_history) < 2:
        return False

    directions = [qj["direction"] for qj in quick_judge_history]
    GOOD = {"improved", "likely_right_direction"}

    for i in range(1, len(directions)):
        if directions[i] in GOOD and directions[i - 1] not in GOOD:
            return True

    return False
