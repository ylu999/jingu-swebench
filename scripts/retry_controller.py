"""
retry_controller.py — B3 retry controller for SWE-bench pipeline (p177).

Given attempt 1's patch + telemetry, produces a structured RetryPlan
that gives the agent specific diagnostic guidance for attempt 2.

p177 extensions:
  - ControlDecision: CONTINUE / ADJUST / STOP_NO_SIGNAL / STOP_FAIL
  - no-signal detection (p164 runner layer): absorbs steps_since_last_signal → STOP_NO_SIGNAL
  - enforced-principal hints: ENV_LEAKAGE_HARDCODE_PATH + PLAN_NO_FEEDBACK_LOOP
    violation signals from cognition gate → injected into next_attempt_prompt
  - declared-only planning principals (P_PLAN_BOTTLENECK_FIRST etc.) → hint only, no hard policy

Decision priority:
  1. tests passed → STOP_OK (caller should check this before calling)
  2. steps_since_last_signal >= NO_SIGNAL_THRESHOLD → STOP_NO_SIGNAL (P7)
  3. exec_feedback empty at attempt > 1 → STOP_FAIL (NBR)
  4. max_attempts reached → STOP_FAIL
  5. enforced-principal violations present → ADJUST (add violation hint)
  6. failure_class × same pattern twice → ADJUST (separation_of_concerns)
  7. failure_class == exploration_loop → ADJUST (bottleneck_first hint)
  8. otherwise → CONTINUE / ADJUST with standard hint
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

from strategy_logger import make_bucket_key

# P7 no-signal threshold (p164 runner layer)
NO_SIGNAL_THRESHOLD = 15  # consecutive steps without write/submit → STOP_NO_SIGNAL

# p178 ε-greedy exploration rate
EPSILON = 0.15  # 15% random exploration, 85% exploit best known hint

# Strategy table cache: (path, mtime, loaded_at, table)
_strategy_table_cache: tuple[str, float, float, dict] | None = None
_CACHE_TTL_SECONDS = 300  # reload table at most once every 5 minutes


def _load_strategy_table(table_path: str | Path | None) -> dict:
    """
    Load strategy_table.json with a 5-minute TTL cache.
    Returns {} if table_path is None or file does not exist.
    """
    global _strategy_table_cache
    if table_path is None:
        return {}
    table_path = str(table_path)
    try:
        mtime = Path(table_path).stat().st_mtime
    except OSError:
        return {}
    now = time.time()
    if (
        _strategy_table_cache is not None
        and _strategy_table_cache[0] == table_path
        and _strategy_table_cache[1] == mtime
        and now - _strategy_table_cache[3] < _CACHE_TTL_SECONDS  # type: ignore[index]
    ):
        return _strategy_table_cache[2]  # type: ignore[index]
    try:
        table = json.loads(Path(table_path).read_text())
        _strategy_table_cache = (table_path, mtime, table, now)  # type: ignore[assignment]
        return table
    except (OSError, json.JSONDecodeError):
        return {}

# Enforced-principal violation codes from jingu-policy-core (p175/p176)
# Only these drive hard ADJUST — declared-only principals are hint-only
ENFORCED_VIOLATION_CODES = frozenset({
    "ENV_LEAKAGE_HARDCODE_PATH",   # P_DEBUG_ENV_INDEPENDENCE (p175)
    "PLAN_NO_FEEDBACK_LOOP",       # P_PLAN_CLOSE_THE_LOOP (p176)
})

# ── Failure taxonomy (mirrors FAILURE_TAXONOMY.md in jingu-policy-core) ──────

FAILURE_TYPES = (
    "wrong_direction",      # FT1: right file, growing patch, no test improvement
    "exploration_loop",     # FT2: no patch produced, no tests ran
    "no_effect_patch",      # FT3: patch written, tests ran, failure unchanged
    "test_not_triggered",   # FT4: patch written, tests not run
    "environment_failure",  # FT5: non-zero exit, no test signal
    "unknown",
)


def classify_failure(
    jingu_body: dict,
    patch_fp: dict,
    prev_patch_fp: Optional[dict],
    exec_feedback: str,
) -> str:
    """
    Deterministic failure classification from observable signals.
    Detection priority: environment → test_not_triggered → exploration_loop
                        → wrong_direction → no_effect_patch → unknown.
    See FAILURE_TAXONOMY.md for full definitions.
    """
    exit_status = jingu_body.get("exit_status", "")
    test_results = jingu_body.get("test_results", {})
    tests_ran = test_results.get("ran_tests", False)
    test_passed = test_results.get("last_passed")
    files_written = jingu_body.get("files_written", [])

    patch_size = patch_fp.get("lines_added", 0) + patch_fp.get("lines_removed", 0)

    # FT5: environment failure — exit error, no test signal
    if exit_status not in ("Submitted", "") and not tests_ran and not files_written:
        return "environment_failure"

    # FT4: test not triggered — patch produced but tests not run
    if patch_size > 0 and not tests_ran:
        return "test_not_triggered"

    # FT2: exploration loop — no meaningful patch
    if patch_size == 0 or not files_written:
        return "exploration_loop"

    # FT1: wrong direction — same file, patch growing, tests still failing
    if (
        prev_patch_fp is not None
        and not test_passed
        and tests_ran
    ):
        prev_files = set(prev_patch_fp.get("files", []))
        curr_files = set(patch_fp.get("files", []))
        prev_size = prev_patch_fp.get("lines_added", 0) + prev_patch_fp.get("lines_removed", 0)
        if curr_files == prev_files and patch_size > prev_size + 10:
            return "wrong_direction"

    # FT3: no effect patch — patch written, tests ran, still failing
    if patch_size > 0 and tests_ran and not test_passed:
        return "no_effect_patch"

    return "unknown"


# Deterministic intervention mapping (no LLM needed for these)
_INTERVENTIONS: dict[str, dict] = {
    "wrong_direction": {
        "must_not_do": [
            "Do not expand the patch further in the same direction",
            "Do not add warning suppression or workarounds — fix the root algorithm",
        ],
        "must_do": [
            "Read the failing test carefully to understand what behavior is expected",
            "Fix the underlying logic, not the symptoms",
        ],
        "hint_prefix": (
            "Previous attempt modified the right file but the approach is incorrect — "
            "the patch kept growing without fixing the core logic. "
        ),
    },
    "exploration_loop": {
        "must_not_do": [
            "Do not continue reading files without committing to a fix",
        ],
        "must_do": [
            "Immediately identify the target file and function from the failing tests",
            "Make the minimal change and submit — do not over-explore",
        ],
        "hint_prefix": (
            "Previous attempt explored without producing a patch. "
            "Go directly to the fix: identify file → make minimal change → submit. "
        ),
    },
    "no_effect_patch": {
        "must_not_do": [
            "Do not modify code that is not exercised by the failing tests",
        ],
        "must_do": [
            "Trace the failing test to the exact function/line it exercises",
            "Modify only the code that directly affects the test outcome",
        ],
        "hint_prefix": (
            "Previous attempt changed code but had no effect on the failing tests. "
            "Find the exact code path the failing test exercises. "
        ),
    },
    "test_not_triggered": {
        "must_not_do": [
            "Do not submit without running the required tests first",
        ],
        "must_do": [
            "Run the FAIL_TO_PASS tests explicitly after your fix",
            "Only submit once you have seen the tests pass",
        ],
        "hint_prefix": (
            "Previous attempt did not run the required tests before submitting. "
        ),
    },
    "environment_failure": {
        "must_not_do": [
            "Do not modify application code before the environment is working",
        ],
        "must_do": [
            "Fix the environment or execution error first",
        ],
        "hint_prefix": "Previous attempt hit an environment error. Fix the execution environment first. ",
    },
    "unknown": {
        "must_not_do": [],
        "must_do": ["Re-read the failing tests and fix the root cause"],
        "hint_prefix": "Previous attempt did not fully solve the problem. ",
    },
}


@dataclass
class RetryPlan:
    root_causes: list[str]
    must_do: list[str]
    must_not_do: list[str]
    validation_requirement: str
    next_attempt_prompt: str
    raw_response: str = ""
    # p177 extensions
    control_action: Literal["CONTINUE", "ADJUST", "STOP_NO_SIGNAL", "STOP_FAIL"] = "CONTINUE"
    principal_violations: list[str] = field(default_factory=list)


# ── Enforced-principal hint templates (p175/p176) ────────────────────────────

_PRINCIPAL_VIOLATION_HINTS: dict[str, str] = {
    "ENV_LEAKAGE_HARDCODE_PATH": (
        "ENVIRONMENT ASSUMPTION VIOLATION: Your diagnosis or fix assumes a local path or "
        "environment variable (e.g. HOME, PATH, /root/, /Users/) that may not exist in the "
        "execution environment. Verify your fix works without machine-local assumptions. "
        "Use relative paths or explicitly detect the environment before relying on it. "
    ),
    "PLAN_NO_FEEDBACK_LOOP": (
        "PLANNING VIOLATION: Your plan has no verifiable feedback loop. "
        "Before submitting a plan or multi-step fix, state how you will confirm each step "
        "succeeded (e.g. 'run test X to verify', 'check output Y'). "
    ),
}


def build_retry_plan(
    problem_statement: str,
    patch_text: str,
    jingu_body: dict,
    fail_to_pass_tests: list[str],
    gate_admitted: bool,
    gate_reason_codes: list[str],
    instance_id: str = "",
    patch_fp: Optional[dict] = None,
    prev_patch_fp: Optional[dict] = None,
    exec_feedback: str = "",
    attempt: int = 1,
    steps_since_last_signal: int = 0,
    principal_violation_codes: Optional[list[str]] = None,
    strategy_table_path: Optional[str | Path] = None,
) -> RetryPlan:
    """
    Deterministic failure classification → intervention mapping → RetryPlan.

    p177 extension: also computes control_action based on:
    - steps_since_last_signal (P7 no-signal detector, p164 runner layer)
    - principal_violation_codes (enforced principals only: ENV_LEAKAGE + PLAN_LOOP)

    No LLM involved.
    """
    fp = patch_fp or {}
    failure_type = classify_failure(jingu_body, fp, prev_patch_fp, exec_feedback)
    intervention = _INTERVENTIONS.get(failure_type, _INTERVENTIONS["unknown"])

    # ── Compute control_action (decision priority order) ─────────────────────

    # P2: no-signal stop (P7 principle, p164 runner layer)
    if steps_since_last_signal >= NO_SIGNAL_THRESHOLD:
        control_action = "STOP_NO_SIGNAL"
        hint = (
            f"STOP: agent ran {steps_since_last_signal} consecutive steps without producing "
            f"any new signal (no file write, no submit). This is a no-signal exploration loop. "
            f"Terminating attempt — retry with explicit file target. "
        )
        return RetryPlan(
            root_causes=[f"no_signal_streak={steps_since_last_signal}"],
            must_do=["Immediately identify target file from failing test", "Make minimal change", "Submit"],
            must_not_do=["Do not continue reading without committing to a specific change"],
            validation_requirement="Run the required FAIL_TO_PASS tests and confirm they pass",
            next_attempt_prompt=hint[:400],
            control_action="STOP_NO_SIGNAL",
        )

    # P3: NBR — no blind retry (handled in run_with_jingu_gate.py as RuntimeError)
    # P8: enforced-principal violations → ADJUST with targeted hint
    viol_codes = [c for c in (principal_violation_codes or []) if c in ENFORCED_VIOLATION_CODES]
    principal_hints = [_PRINCIPAL_VIOLATION_HINTS[c] for c in viol_codes if c in _PRINCIPAL_VIOLATION_HINTS]

    # ── p178: ε-greedy hint selection from strategy table ─────────────────────
    bucket_key = make_bucket_key(failure_type, viol_codes)
    table = _load_strategy_table(strategy_table_path)
    bucket_data = table.get(bucket_key, {})
    trusted_hints = {h: s for h, s in bucket_data.items() if s.get("trusted", False)}

    if trusted_hints and random.random() >= EPSILON:
        # Exploit: use best known hint for this bucket
        selected_hint = max(trusted_hints, key=lambda h: trusted_hints[h]["win_rate"])
    else:
        # Explore (or cold-start): use deterministic intervention hint
        selected_hint = intervention["hint_prefix"]

    # ── Build hint ────────────────────────────────────────────────────────────
    hint_parts = []
    if principal_hints:
        hint_parts.extend(principal_hints)
    hint_parts.append(selected_hint)
    if exec_feedback:
        hint_parts.append(exec_feedback[:300])
    hint = " ".join(hint_parts).strip()

    control_action: Literal["CONTINUE", "ADJUST", "STOP_NO_SIGNAL", "STOP_FAIL"] = "CONTINUE"
    if viol_codes or failure_type != "unknown":
        control_action = "ADJUST"

    return RetryPlan(
        root_causes=[f"failure_type={failure_type}"] + [f"violation={c}" for c in viol_codes],
        must_do=intervention["must_do"],
        must_not_do=intervention["must_not_do"],
        validation_requirement="Run the required FAIL_TO_PASS tests and confirm they pass",
        next_attempt_prompt=hint[:600],
        control_action=control_action,
        principal_violations=viol_codes,
    )


if __name__ == "__main__":
    # Smoke test
    plan = build_retry_plan(
        problem_statement="Merging 3 or more media objects throws unnecessary MediaOrderConflictWarnings",
        patch_text="diff --git a/django/forms/widgets.py b/django/forms/widgets.py\n+++ b/django/forms/widgets.py\n@@ -140,6 +140,8 @@\n+    # added logging\n+    print('merging')\n",
        jingu_body={
            "exit_status": "Submitted",
            "files_written": ["django/forms/widgets.py"],
            "test_results": {"ran_tests": True, "last_passed": False, "excerpt": "FAILED (failures=16)"},
            "patch_summary": {"hunks": 1, "lines_added": 2, "lines_removed": 0},
        },
        fail_to_pass_tests=["test_merge (forms_tests.tests.test_media.FormsMediaTestCase)"],
        gate_admitted=True,
        gate_reason_codes=[],
        instance_id="django__django-11019",
    )
    print(f"failure_type: {plan.root_causes[0]}")
    print(f"must_do: {plan.must_do}")
    print(f"must_not_do: {plan.must_not_do}")
    print(f"next_attempt_prompt: {plan.next_attempt_prompt}")
