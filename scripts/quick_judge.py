"""
quick_judge.py — E1: In-loop quick judge (tier 1 verification).

Target-aware corrective signal system.  Runs targeted F2P tests at method
level and produces a signal keyed on the *target test status*, not aggregate
pass counts.

Core principle: quick judge is target-aware, not aggregate-aware.

Components:
  - QuickJudgeResult           target-aware structured result
  - select_targeted_tests()    stable F2P subset selection (max 5)
  - select_sentinel_tests()    P2P sentinel selection for regression detection (max 3)
  - run_quick_judge()          run tests via docker exec, 30s timeout + sentinel
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
    "target_partial",           # primary target passed but other F2P tests still failing
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

    # Multi-F2P target coverage
    target_results: dict = field(default_factory=dict)  # test_id -> TargetStatus
    f2p_targeted: int = 0    # how many F2P tests were targeted
    f2p_passed: int = 0      # how many F2P tests passed
    f2p_failed: int = 0      # how many F2P tests failed (includes error)
    f2p_coverage: float = 0.0  # f2p_passed / f2p_targeted (0.0 if no targets)

    # Aggregate counts (secondary, for telemetry)
    tests_targeted: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    tests_error: int = 0
    executed_test_ids: list = field(default_factory=list)
    failing_test_names: list = field(default_factory=list)  # max 3 names
    elapsed_ms: float = 0.0
    direction: QuickJudgeDirection = "first_signal"

    # P2P sentinel regression detection
    sentinel_tests_run: int = 0
    sentinel_tests_passed: int = 0
    sentinel_tests_failed: int = 0
    regression_detected: bool = False
    regression_test_names: list = field(default_factory=list)

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


def _parse_pass_to_pass(instance):
    """Parse PASS_TO_PASS from instance dict, handling both list and JSON-string formats."""
    raw = instance.get("PASS_TO_PASS", [])
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


# ── Sentinel test selection (P2P regression detection) ───────────────────────

_SENTINEL_MAX = 3
_SENTINEL_TIMEOUT_S = 30


def select_sentinel_tests(instance, changed_files, *, priority_tests=None):
    """
    Select up to 3 P2P sentinel tests for regression detection.

    These tests SHOULD keep passing after the patch. If any fail, the patch
    has introduced a regression.

    Priority:
      0. priority_tests — known regression tests from previous attempt (always included first)
      1. Tests whose module name matches a changed file name (most related)
      2. Shortest test names first (proxy for simplest/fastest)

    Returns a deterministic sorted list of P2P test IDs (max 3).
    """
    p2p = _parse_pass_to_pass(instance)
    if not p2p:
        return []

    p2p_set = set(p2p)

    # Priority 0: known regression tests from previous attempt
    priority_selected = []
    if priority_tests:
        for pt in priority_tests:
            if pt in p2p_set and pt not in priority_selected:
                priority_selected.append(pt)
                if len(priority_selected) >= _SENTINEL_MAX:
                    break
        if priority_selected:
            print(f"    [sentinel-priority] {len(priority_selected)} prev regression tests "
                  f"included in sentinel: {priority_selected[:3]}", flush=True)

    remaining_slots = _SENTINEL_MAX - len(priority_selected)
    if remaining_slots <= 0:
        return sorted(priority_selected)

    # Build set of changed basenames (without extension) for matching
    already_selected = set(priority_selected)
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

    # Partition: matching tests first (most likely to regress), then rest
    candidates = [t for t in p2p if t not in already_selected]
    matching = sorted(
        [t for t in candidates if _matches_changed(t)],
        key=lambda t: (len(t), t),
    )
    rest = sorted(
        [t for t in candidates if not _matches_changed(t)],
        key=lambda t: (len(t), t),
    )

    selected = priority_selected + (matching + rest)[:remaining_slots]
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


# ── Docstring-based test name resolution ─────────────────────────────────────

def _is_docstring_test_name(test_id):
    """Detect whether a test ID is a docstring-based description rather than a standard test ID.

    A docstring-based name:
      - Does NOT match Django format: method (class.path)
      - Does NOT match pytest format: path::method
      - Typically contains spaces and natural language

    Returns True if the test_id appears to be a docstring description.
    """
    if not test_id or not isinstance(test_id, str):
        return False

    test_id = test_id.strip()
    if not test_id:
        return False

    # Check Django format: word (dotted.path)
    if re.match(r'^(\w+)\s+\([\w.]+\)$', test_id):
        return False

    # Check pytest format: contains ::
    if '::' in test_id:
        return False

    # Check simple dotted path (e.g., "module.Class.method")
    if re.match(r'^[\w.]+$', test_id) and '.' in test_id:
        return False

    # If it contains spaces, it is likely a docstring description
    if ' ' in test_id:
        return True

    return False


def _extract_docstring_keywords(test_id):
    """Extract meaningful keywords from a docstring-based test name for fuzzy matching.

    Extracts:
      - Warning/error codes (e.g., W036, E001)
      - Dotted identifiers (e.g., Model.clean, models.W036)
      - CamelCase identifiers
      - snake_case identifiers

    Returns a list of keywords ordered by specificity (codes first, then identifiers).
    """
    keywords = []

    # 1. Warning/error codes like W036, E001, W003
    codes = re.findall(r'\b[A-Z]\d{3}\b', test_id)
    keywords.extend(codes)

    # 2. Dotted identifiers (e.g., Model.clean, models.W036)
    dotted = re.findall(r'\b[\w]+\.[\w]+\b', test_id)
    keywords.extend(dotted)

    # 3. CamelCase identifiers
    camel = re.findall(r'\b[A-Z][a-z]+[A-Z]\w*\b', test_id)
    keywords.extend(camel)

    # 4. Capitalized words that might be class names (filter common English)
    _COMMON_WORDS = {
        'Using', 'When', 'The', 'This', 'That', 'With', 'Should', 'Must',
        'Can', 'Cannot', 'Does', 'Not', 'And', 'But', 'For', 'Are', 'Is',
        'If', 'Or', 'An', 'In', 'On', 'At', 'To', 'Of', 'By', 'As',
        'It', 'Be', 'Do', 'Has', 'Had', 'Have', 'Was', 'Were', 'Will',
        'May', 'Might', 'Could', 'Would', 'After', 'Before', 'From',
        'FAIL', 'PASS', 'ERROR', 'OK', 'Test', 'Tests', 'No', 'All',
        'Any', 'Each', 'Every', 'Some', 'Only', 'Both', 'Also',
        'Method', 'Check', 'Field', 'List', 'Set', 'Type',
    }
    caps = re.findall(r'\b[A-Z][a-z]+\b', test_id)
    for w in caps:
        if w not in _COMMON_WORDS and w not in keywords:
            keywords.append(w)

    # 5. snake_case identifiers (look like code)
    snake = re.findall(r'\b[a-z]+_[a-z_]+\b', test_id)
    for s in snake:
        if s not in keywords:
            keywords.append(s)

    return keywords


def _resolve_docstring_test(test_id, test_results):
    """Try to resolve a docstring-based test name against parsed test results.

    Strategies (in order):
      1. If only 1 test result exists -> unambiguous match
      2. Keyword matching: extract codes/identifiers from docstring,
         match against test result IDs
      3. If exactly 1 match found -> return it; if ambiguous -> return None

    Returns the matched status ("passed"/"failed"/"error") or None if no match.
    """
    if not test_results:
        return None

    # Strategy 1: unambiguous single result
    if len(test_results) == 1:
        return list(test_results.values())[0]

    # Strategy 2: keyword matching
    keywords = _extract_docstring_keywords(test_id)
    if not keywords:
        return None

    # Score each test result by how many keywords match
    scored = []
    for result_id, status in test_results.items():
        result_lower = result_id.lower()
        score = 0
        for kw in keywords:
            kw_lower = kw.lower()
            if kw_lower in result_lower:
                # Codes and dotted identifiers are higher value
                if re.match(r'^[A-Z]\d{3}$', kw) or '.' in kw:
                    score += 3
                else:
                    score += 1
        if score > 0:
            scored.append((score, result_id, status))

    if not scored:
        return None

    # Sort by score descending
    scored.sort(key=lambda x: -x[0])

    # Only return if the top match is unambiguous (clearly ahead of second)
    if len(scored) == 1:
        return scored[0][2]

    # If top two have different scores, the top one wins
    if scored[0][0] > scored[1][0]:
        return scored[0][2]

    # Ambiguous -- multiple matches with same score
    return None


def _docstring_to_test_label(test_id, instance):
    """Try to map a docstring-based test name to a runnable Django test label.

    Uses keyword extraction to find a plausible test module/class path from
    sibling F2P entries that are in standard format.
    Returns a test label string or None if mapping fails.
    """
    keywords = _extract_docstring_keywords(test_id)
    if not keywords:
        return None

    # Try to find a matching test module from the instance's F2P siblings
    f2p = _parse_fail_to_pass(instance)
    for sibling in f2p:
        if sibling == test_id:
            continue
        # If a sibling is in standard format, use its module as a hint
        method, class_path = _parse_django_test_id(sibling)
        if method and class_path:
            # Check if any keyword matches the sibling's class path
            class_lower = class_path.lower()
            for kw in keywords:
                if kw.lower() in class_lower:
                    return class_path  # Use sibling's class as the test label
            # Even without keyword match, if there is a sibling in standard format,
            # it is likely in the same module -- use it as fallback
            return class_path

    return None


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

    # Docstring-based test name resolution
    try:
        if _is_docstring_test_name(target_test_id):
            resolved = _resolve_docstring_test(target_test_id, test_results)
            if resolved:
                if resolved == "passed":
                    return "passed"
                elif resolved == "error":
                    return "error"
                else:
                    return "failed"
    except Exception:
        pass  # exception-safe: fall through to missing/unknown

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


def _classify_multi_target_signal(target_results, primary_target):
    """
    Classify signal_kind accounting for ALL F2P targets, not just primary.

    Returns (signal_kind, f2p_passed, f2p_failed, f2p_coverage).

    Logic:
      - ALL pass → target_passed
      - Primary passes but others fail → target_partial
      - Primary fails → target_failed / target_error / target_missing
      - No targets → non_corrective_noise
    """
    if not target_results:
        return "non_corrective_noise", 0, 0, 0.0

    f2p_targeted = len(target_results)
    f2p_passed = sum(1 for s in target_results.values() if s == "passed")
    f2p_failed = sum(1 for s in target_results.values() if s in ("failed", "error"))
    f2p_coverage = f2p_passed / f2p_targeted if f2p_targeted > 0 else 0.0

    primary_status = target_results.get(primary_target, "unknown")

    # If primary target didn't pass, classify based on primary status alone
    if primary_status != "passed":
        return _classify_signal_kind(primary_status), f2p_passed, f2p_failed, f2p_coverage

    # Primary passed — check if ALL passed
    if f2p_passed == f2p_targeted:
        return "target_passed", f2p_passed, f2p_failed, f2p_coverage

    # Primary passed but some others failed → partial
    return "target_partial", f2p_passed, f2p_failed, f2p_coverage


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

        # Docstring-based test name — try to map to a runnable label
        if _is_docstring_test_name(entry):
            try:
                label = _docstring_to_test_label(entry, instance)
                if label:
                    class_labels.add(label)
                    continue
            except Exception:
                pass  # exception-safe fallback

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
                    previous_result=None, step=0, *,
                    priority_sentinel_tests=None):
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

        # Resolve target status for ALL F2P targets
        all_target_results = {}
        for tid in test_ids:
            try:
                all_target_results[tid] = _resolve_target_status(test_results, tid)
            except Exception:
                all_target_results[tid] = "unknown"

        # Primary target status (backward compatible)
        target_status = all_target_results.get(primary_target, "unknown")

        # Multi-target signal classification
        signal_kind, f2p_passed, f2p_failed, f2p_coverage = (
            _classify_multi_target_signal(all_target_results, primary_target)
        )
        corrective = target_status in ("passed", "failed", "error")

        # ── Sentinel regression check (only when target passes) ──────
        sentinel_run = 0
        sentinel_passed_count = 0
        sentinel_failed_count = 0
        regression_detected = False
        regression_names = []

        if target_status == "passed":
            try:
                sentinel_ids = select_sentinel_tests(
                    instance, changed_files,
                    priority_tests=priority_sentinel_tests,
                )
                print(f"    [quick-judge] sentinel selected={len(sentinel_ids)}"
                      f"{' ids=' + ','.join(sentinel_ids[:3]) if sentinel_ids else ''}",
                      flush=True)
                if sentinel_ids:
                    sentinel_cmd, _ = _build_quick_test_command(
                        instance, sentinel_ids
                    )
                    sentinel_proc = subprocess.run(
                        ["docker", "exec", container_id, "bash", "-c",
                         sentinel_cmd],
                        capture_output=True, text=True,
                        timeout=_SENTINEL_TIMEOUT_S,
                    )
                    sentinel_output = (
                        (sentinel_proc.stdout or "")
                        + (sentinel_proc.stderr or "")
                    )
                    sentinel_results, (s_passed, s_failed, s_errored,
                                       s_failing) = (
                        _parse_quick_test_output(sentinel_output, sentinel_ids)
                    )
                    sentinel_run = len(sentinel_ids)
                    sentinel_passed_count = s_passed
                    sentinel_failed_count = s_failed + s_errored
                    if sentinel_failed_count > 0:
                        regression_detected = True
                        regression_names = s_failing[:3]
                    print(f"    [quick-judge] sentinel executed={sentinel_run} "
                          f"passed={sentinel_passed_count} failed={sentinel_failed_count} "
                          f"regression={regression_detected}", flush=True)
            except Exception as _sentinel_exc:
                # Sentinel failure must not crash quick judge
                print(f"    [quick-judge] sentinel ERROR: {_sentinel_exc}", flush=True)

        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)

        result = QuickJudgeResult(
            step=step,
            target_test_id=primary_target,
            target_status=target_status,
            signal_kind=signal_kind,
            corrective=corrective,
            command_scope=scope,
            target_results=all_target_results,
            f2p_targeted=len(test_ids),
            f2p_passed=f2p_passed,
            f2p_failed=f2p_failed,
            f2p_coverage=f2p_coverage,
            tests_targeted=n_targeted,
            tests_passed=passed,
            tests_failed=failed,
            tests_error=errored,
            executed_test_ids=list(test_results.keys()),
            failing_test_names=failing_names[:3],
            elapsed_ms=elapsed_ms,
            direction="first_signal",  # placeholder, classified below
            sentinel_tests_run=sentinel_run,
            sentinel_tests_passed=sentinel_passed_count,
            sentinel_tests_failed=sentinel_failed_count,
            regression_detected=regression_detected,
            regression_test_names=regression_names,
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
    # Determine message based on signal_kind (accounts for multi-F2P + regression)
    if result.target_status == "passed" and result.regression_detected:
        header = "TARGET PASSED BUT REGRESSION DETECTED"
        hint = "Your fix breaks existing tests. Check that you preserve existing behavior."
    elif result.signal_kind == "target_partial":
        header = "PARTIAL"
        hint = "Your fix addresses the main issue but misses edge cases. Check the failing tests."
    elif result.target_status == "passed":
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

    # F2P coverage suffix for multi-target
    coverage_suffix = ""
    if result.f2p_targeted > 1:
        coverage_suffix = f" — {result.f2p_passed}/{result.f2p_targeted} F2P targets pass"

    lines = [
        f"[QUICK_CHECK step={result.step}] {header}{confidence}{coverage_suffix}",
    ]

    # Show target test identity
    if result.target_test_id:
        status_label = result.target_status.upper() if result.target_status != "unknown" else "UNKNOWN"
        lines.append(f"Target: {result.target_test_id} — {status_label}")

    # Show still-failing F2P tests for partial coverage
    if result.signal_kind == "target_partial" and result.target_results:
        still_failing = [
            tid for tid, s in result.target_results.items()
            if s in ("failed", "error") and tid != result.target_test_id
        ]
        if still_failing:
            for tid in still_failing[:3]:
                lines.append(f"Still failing: {tid}")

    # Show failing tests for failed/error (single-target case)
    elif result.target_status in ("failed", "error") and result.failing_test_names:
        names_str = ", ".join(result.failing_test_names[:3])
        lines.append(f"Failing: {names_str}")

    # Show regression sentinel results
    if result.regression_detected and result.regression_test_names:
        reg_str = ", ".join(result.regression_test_names[:3])
        lines.append(f"Regression: {reg_str}")

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
