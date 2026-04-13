"""
quick_judge.py — E1: In-loop quick judge (tier 1 verification).

Target-aware corrective signal system.  Runs targeted F2P tests at method
level and produces a signal keyed on the *target test status*, not aggregate
pass counts.

Core principle: quick judge is target-aware, not aggregate-aware.

Components:
  - QuickJudgeResult           target-aware structured result
  - select_targeted_tests()    stable F2P subset selection (max 5)
  - run_quick_judge()          run tests via docker exec, 30s timeout
  - classify_direction()       compare consecutive results
  - format_agent_message()     gated message: only positive when target passed
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

TargetStatus = Literal[
    "passed",       # target F2P test explicitly passed
    "failed",       # target F2P test explicitly failed
    "error",        # target F2P test had an error (import, syntax, etc.)
    "missing",      # target F2P test not found in output
    "unknown",      # could not determine (timeout, parse failure, etc.)
]

SignalKind = Literal[
    "target_passed",            # target test passed — corrective positive
    "target_failed",            # target test failed — corrective negative
    "target_error",             # target test errored — likely infra/syntax issue
    "target_missing",           # target test not in output — test selection or infra
    "non_corrective_noise",     # could not determine target status — no signal
]

CommandScope = Literal[
    "method",       # ran specific test method(s)
    "class",        # ran whole test class (fallback)
    "module",       # ran whole module (fallback)
]


# ── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class QuickJudgeResult:
    """Target-aware structured result of a mid-execution targeted test run."""
    tier: Literal["quick"] = "quick"
    trigger_source: QuickJudgeTriggerSource = "automatic_patch_detected"
    step: int = 0

    # Target-aware fields (the core contract)
    target_test_id: str = ""
    target_status: TargetStatus = "unknown"
    signal_kind: SignalKind = "non_corrective_noise"
    corrective: bool = False
    command_scope: CommandScope = "class"

    # Aggregate counts (secondary, for telemetry)
    tests_targeted: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    tests_error: int = 0
    executed_test_ids: list = field(default_factory=list)
    failing_test_names: list = field(default_factory=list)  # max 3 names
    elapsed_ms: float = 0.0
    direction: QuickJudgeDirection = "first_signal"

    @property
    def all_passed(self):
        return self.tests_failed == 0 and self.tests_error == 0

    @property
    def has_signal(self):
        return self.target_status != "unknown"


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

    if current.tests_error >= current.tests_targeted and current.tests_targeted > 0:
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


# ── Test ID parsing helpers ──────────────────────────────────────────────────

def _parse_django_test_id(test_id):
    """Parse Django-format test ID into (method, class_path).

    'test_foo (delete.tests.DeletionTests)' -> ('test_foo', 'delete.tests.DeletionTests')
    Returns (None, None) if not Django format.
    """
    m = re.match(r'^(\w+)\s+\((.+)\)$', test_id.strip())
    if m:
        return m.group(1), m.group(2)
    return None, None


def _parse_pytest_test_id(test_id):
    """Parse pytest-format test ID into (method, module_path).

    'tests/test_foo.py::TestClass::test_method' -> ('test_method', 'tests/test_foo.py::TestClass')
    Returns (None, None) if not pytest format.
    """
    if "::" in test_id:
        parts = test_id.rsplit("::", 1)
        if len(parts) == 2:
            return parts[1], parts[0]
    return None, None


# ── Test runner ──────────────────────────────────────────────────────────────

_QUICK_JUDGE_TIMEOUT_S = 30


def _parse_quick_test_output(output, target_test_ids):
    """
    Parse test output to extract per-test results and target status.

    Returns (test_results, aggregate) where:
      test_results: dict mapping full_test_id -> "passed" | "failed" | "error"
      aggregate: (passed, failed, errored, failing_names)
    """
    test_results = {}  # full_id -> status
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
                test_results[full_id] = "passed"
            elif status == "ERROR":
                errored += 1
                failing_names.append(full_id)
                test_results[full_id] = "error"
            else:
                failed += 1
                failing_names.append(full_id)
                test_results[full_id] = "failed"
            continue

        # pytest format: "path/test.py::test_name PASSED/FAILED/ERROR"
        m_pytest = re.match(r"^(.+?)\s+(PASSED|FAILED|ERROR)", line)
        if m_pytest:
            test_id = m_pytest.group(1).strip()
            status = m_pytest.group(2)
            if status == "PASSED":
                passed += 1
                test_results[test_id] = "passed"
            elif status == "ERROR":
                errored += 1
                failing_names.append(test_id)
                test_results[test_id] = "error"
            else:
                failed += 1
                failing_names.append(test_id)
                test_results[test_id] = "failed"

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

    return test_results, (passed, failed, errored, failing_names[:3])


def _resolve_target_status(test_results, target_test_id):
    """
    Determine the status of the target F2P test from parsed test results.

    Tries exact match first, then method-name match (handles class path variations).
    Returns a TargetStatus.
    """
    # Exact match
    if target_test_id in test_results:
        s = test_results[target_test_id]
        if s == "passed":
            return "passed"
        elif s == "error":
            return "error"
        else:
            return "failed"

    # Method-name match: extract method name from target and search results
    target_method, _ = _parse_django_test_id(target_test_id)
    if target_method is None:
        target_method, _ = _parse_pytest_test_id(target_test_id)

    if target_method:
        for result_id, status in test_results.items():
            result_method, _ = _parse_django_test_id(result_id)
            if result_method is None:
                result_method, _ = _parse_pytest_test_id(result_id)
            if result_method == target_method:
                if status == "passed":
                    return "passed"
                elif status == "error":
                    return "error"
                else:
                    return "failed"

    # Not found in results
    if test_results:
        return "missing"  # tests ran but target not in output
    return "unknown"  # no results at all


def _classify_signal_kind(target_status):
    """Map target_status to signal_kind."""
    if target_status == "passed":
        return "target_passed"
    elif target_status == "failed":
        return "target_failed"
    elif target_status == "error":
        return "target_error"
    elif target_status == "missing":
        return "target_missing"
    else:
        return "non_corrective_noise"


def _build_quick_test_command(instance, test_ids):
    """
    Build docker exec test command for targeted F2P tests.

    Produces method-level test commands when possible:
    - Django: ./tests/runtests.py delete.tests.DeletionTests.test_method
    - pytest: pytest path/test.py::Class::method

    Falls back to class-level if method extraction fails.
    Returns (command_string, command_scope).
    """
    from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS

    repo = instance["repo"]
    version = instance["version"]
    specs = MAP_REPO_VERSION_TO_SPECS[repo][version]
    test_cmd = specs["test_cmd"]

    # Try method-level labels first
    method_labels = []
    class_labels = set()
    for entry in test_ids:
        method, class_path = _parse_django_test_id(entry)
        if method and class_path:
            # Django runtests.py: module.Class.method
            method_labels.append(f"{class_path}.{method}")
            class_labels.add(class_path)
            continue

        method, module_path = _parse_pytest_test_id(entry)
        if method and module_path:
            method_labels.append(f"{module_path}::{method}")
            class_labels.add(module_path)
            continue

        # Unknown format — use as-is at class level
        class_labels.add(entry)

    if method_labels:
        labels_str = " ".join(sorted(method_labels))
        scope = "method"
    elif class_labels:
        labels_str = " ".join(sorted(class_labels))
        scope = "class"
    else:
        labels_str = " ".join(test_ids)
        scope = "module"

    cmd = (
        "source /opt/miniconda3/bin/activate && "
        "conda activate testbed && "
        "cd /testbed && "
        f"{test_cmd} {labels_str} 2>&1"
    )
    return cmd, scope


def run_quick_judge(patch, instance, container_id, changed_files,
                    previous_result=None, step=0):
    """
    Run a quick judge: targeted F2P test subset in the eval container.

    Exception-safe: on any error, returns a result with target_status="unknown"
    and signal_kind="non_corrective_noise".  Never raises.

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
    # Primary target = first F2P test (most instances have exactly 1)
    primary_target = test_ids[0] if test_ids else ""

    if n_targeted == 0:
        return QuickJudgeResult(
            step=step,
            target_test_id="",
            target_status="unknown",
            signal_kind="non_corrective_noise",
            corrective=False,
            command_scope="class",
            tests_targeted=0,
            elapsed_ms=round((time.monotonic() - t0) * 1000, 1),
            direction="inconclusive",
        )

    # Build and run test command
    try:
        test_cmd, scope = _build_quick_test_command(instance, test_ids)

        test_result = subprocess.run(
            ["docker", "exec", container_id, "bash", "-c", test_cmd],
            capture_output=True, text=True, timeout=_QUICK_JUDGE_TIMEOUT_S,
        )
        output = (test_result.stdout or "") + (test_result.stderr or "")

        test_results, (passed, failed, errored, failing_names) = (
            _parse_quick_test_output(output, test_ids)
        )

        # If nothing parsed at all, treat entire run as errored
        if passed == 0 and failed == 0 and errored == 0:
            errored = n_targeted

        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)

        # Resolve target status
        target_status = _resolve_target_status(test_results, primary_target)
        signal_kind = _classify_signal_kind(target_status)
        corrective = target_status in ("passed", "failed", "error")

        result = QuickJudgeResult(
            step=step,
            target_test_id=primary_target,
            target_status=target_status,
            signal_kind=signal_kind,
            corrective=corrective,
            command_scope=scope,
            tests_targeted=n_targeted,
            tests_passed=passed,
            tests_failed=failed,
            tests_error=errored,
            executed_test_ids=list(test_results.keys()),
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
            target_test_id=primary_target,
            target_status="unknown",
            signal_kind="non_corrective_noise",
            corrective=False,
            command_scope="class",
            tests_targeted=n_targeted,
            elapsed_ms=elapsed_ms,
            direction="inconclusive",
        )
    except Exception:
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        return QuickJudgeResult(
            step=step,
            target_test_id=primary_target,
            target_status="unknown",
            signal_kind="non_corrective_noise",
            corrective=False,
            command_scope="class",
            tests_targeted=n_targeted,
            elapsed_ms=elapsed_ms,
            direction="inconclusive",
        )


# ── Agent message formatting (gated on target status) ────────────────────────

def format_agent_message(result):
    """
    Format a QuickJudgeResult as a minimal structured message for agent injection.

    GATING RULES:
      target_status=passed  → positive signal allowed
      target_status=failed  → corrective negative signal
      target_status=error   → corrective error signal
      target_status=missing → warning (target not found)
      target_status=unknown → warning (no signal)
      command_scope != method → downgrade confidence
    """
    # Determine message based on target_status (not aggregate counts)
    if result.target_status == "passed":
        header = "TARGET PASSED"
        hint = "Target F2P test is passing. Your patch fixes the reported issue."
    elif result.target_status == "failed":
        header = "TARGET FAILED"
        hint = "Target F2P test is still failing. Your patch does not yet fix the issue."
    elif result.target_status == "error":
        header = "TARGET ERROR"
        hint = "Target F2P test has an error (import/syntax). Check your changes for errors."
    elif result.target_status == "missing":
        header = "TARGET NOT FOUND"
        hint = "Target F2P test was not found in test output. The test may require new model definitions or imports."
    else:  # unknown
        header = "NO SIGNAL"
        hint = "Could not determine target test status. Check for import/syntax errors."

    # Confidence qualifier based on command_scope
    confidence = ""
    if result.command_scope != "method":
        confidence = f" (scope={result.command_scope}, lower confidence)"

    lines = [
        f"[QUICK_CHECK step={result.step}] {header}{confidence}",
    ]

    # Show target test identity
    if result.target_test_id:
        lines.append(f"Target: {result.target_test_id}")

    # Show failing tests only for failed/error
    if result.target_status in ("failed", "error") and result.failing_test_names:
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
      2. Agent text mentions the target test
    """
    if not qj_result:
        return False

    text = post_injection_assistant_text or ""

    # Check target test name
    if qj_result.target_test_id:
        target_method, _ = _parse_django_test_id(qj_result.target_test_id)
        if target_method is None:
            target_method, _ = _parse_pytest_test_id(qj_result.target_test_id)
        if target_method and target_method in text:
            return True

    # Check failing test names
    for test_name in (qj_result.failing_test_names or []):
        # Use last part of test path
        short_name = test_name.split("::")[-1]
        m = re.match(r'^(\w+)\s+\(', short_name)
        if m:
            short_name = m.group(1)
        if short_name and short_name in text:
            return True

    return False


def detect_effective(quick_judge_history):
    """
    L3 metric: detect convergence across consecutive quick judge results.

    Effective if target_status transitions to "passed" at any point,
    or if direction sequence shows at least one transition from BAD to GOOD.
    """
    if len(quick_judge_history) < 2:
        return False

    # Primary: target_status convergence
    for i in range(1, len(quick_judge_history)):
        curr = quick_judge_history[i].get("target_status", "unknown")
        prev = quick_judge_history[i - 1].get("target_status", "unknown")
        if curr == "passed" and prev != "passed":
            return True

    # Secondary: direction convergence
    directions = [qj.get("direction", "") for qj in quick_judge_history]
    GOOD = {"improved", "likely_right_direction"}
    for i in range(1, len(directions)):
        if directions[i] in GOOD and directions[i - 1] not in GOOD:
            return True

    return False
