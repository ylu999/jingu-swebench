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

from strategy_logger import make_bucket_key, make_bucket_key_v2

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


def classify_failure_v2(
    jingu_body: dict,
    patch_fp: dict,
    tests_delta: Optional[int],
    tests_passed_after: int = -1,
) -> str:
    """
    p179 taxonomy v1: signal-contract-aware failure classification.

    Uses tests_delta as primary discriminator.
    tests_passed_after=-1 means count is unknown (signal_missing when tests ran).
    tests_delta=None means baseline is unknown — cannot compute delta.

    Buckets:
      no_patch_or_invalid       — no patch, or tests couldn't run at all
      verified_pass             — controlled_verify / local tests passed (strong signal)
      no_test_progress          — tests ran, delta ≤ 0 (stagnant or regression)
      positive_delta_unresolved — delta > 0 but still failing (agent is on track)
      signal_missing            — tests ran but no count info available
    """
    test_results = jingu_body.get("test_results", {})
    tests_ran = test_results.get("ran_tests", False)
    patch_size = patch_fp.get("lines_added", 0) + patch_fp.get("lines_removed", 0)

    # No patch or environment didn't allow tests to run
    if patch_size == 0 or not tests_ran:
        return "no_patch_or_invalid"

    # controlled_verify passed (tests_failed == 0 from orchestrator-controlled run)
    cv = jingu_body.get("controlled_verify", {})
    # BUG-10: use eval_resolved (F2P+P2P aligned) as primary signal
    if cv.get("eval_resolved") is True:
        return "verified_pass"
    # Fallback: old behavior for pre-BUG-10 data
    cv_failed = cv.get("tests_failed", -1)
    if cv_failed == 0 and cv.get("eval_resolved") is None:
        return "verified_pass"

    # tests ran and still failing — check if we have count signal
    if tests_passed_after < 0 or tests_delta is None:
        # No count information available — delta is meaningless
        return "signal_missing"

    if tests_delta > 0:
        return "positive_delta_unresolved"
    # tests_delta == 0: stagnant; tests_delta < 0: regression — both = no progress
    return "no_test_progress"


# ── Outcome classification (BUG-10: F2P/P2P-based) ──────────────────────────

OUTCOME_TYPES = (
    "resolved",          # All F2P pass, no P2P regression
    "partial_fix",       # Some F2P pass, no P2P regression
    "wrong_direction",   # No F2P pass
    "regression",        # P2P tests broken
    "no_signal",         # Can't classify (no CV data or no F2P tests)
)


def classify_outcome(controlled_verify: dict) -> str:
    """
    Outcome classification based on F2P/P2P decomposition from controlled_verify.

    Uses eval-aligned fields (BUG-10 fix) as primary signal.
    Returns one of OUTCOME_TYPES.
    """
    if not controlled_verify:
        return "no_signal"

    f2p_passed = controlled_verify.get("f2p_passed")
    f2p_failed = controlled_verify.get("f2p_failed")
    p2p_passed = controlled_verify.get("p2p_passed")
    p2p_failed = controlled_verify.get("p2p_failed")

    # No F2P data available — can't classify
    if f2p_passed is None or f2p_failed is None:
        return "no_signal"

    total_f2p = f2p_passed + f2p_failed
    if total_f2p == 0:
        return "no_signal"

    # P2P regression — highest priority
    if p2p_failed is not None and p2p_failed > 0:
        return "regression"

    # All F2P pass — resolved
    if f2p_passed == total_f2p:
        return "resolved"

    # Some F2P pass — partial fix
    if f2p_passed > 0:
        return "partial_fix"

    # No F2P pass — wrong direction
    return "wrong_direction"


# ── Outcome classification v2 (agent-visible signals only, no oracle) ────────
#
# Uses inner-verify (apply_test_patch=False) which runs F2P tests against the
# EXISTING test suite. ~90% of F2P tests are pre-existing (not added by test_patch),
# so f2p_passed/f2p_failed from inner-verify is a legitimate agent-visible signal.
# Tests added by test_patch will be counted as "failed" (not found in output),
# which is conservative but correct — the agent cannot see those tests.

OUTCOME_V2_TYPES = (
    "good_progress",     # All F2P tests pass (agent-visible)
    "partial_progress",  # Some F2P pass, no regression
    "wrong_direction",   # No F2P pass
    "regression",        # New failures introduced (P2P broke)
    "no_signal",         # Can't classify (tests didn't run)
    "no_patch",          # No patch produced
)


def classify_outcome_v2(
    f2p_passed: int,
    f2p_total: int,
    new_failures: int,
    patch_exists: bool,
) -> str:
    """
    Outcome classification using only agent-visible signals (no test_patch / no oracle).

    f2p_passed: FAIL_TO_PASS tests that passed in inner-verify (apply_test_patch=False).
        These are pre-existing tests that the agent could also run.
    f2p_total: total FAIL_TO_PASS tests checked.
    new_failures: tests that newly failed compared to baseline (regression signal).
    patch_exists: whether a non-empty patch was produced.

    Signal legality:
    - F2P test names: injected into agent prompt (benchmark-provided, agent-visible)
    - inner-verify: runs against existing test suite (no test_patch = no oracle)
    - new_failures: baseline comparison (agent could do this themselves)
    """
    # No patch → exploration loop
    if not patch_exists:
        return "no_patch"

    # No F2P data → can't classify
    if f2p_total == 0:
        return "no_signal"

    # Regression: highest priority (broke existing tests)
    if new_failures > 0:
        return "regression"

    # No F2P tests fixed
    if f2p_passed == 0:
        return "wrong_direction"

    # Some but not all F2P fixed
    if f2p_passed < f2p_total:
        return "partial_progress"

    # All F2P pass (agent-visible, not eval_resolved)
    return "good_progress"


# ── Outcome v2 interventions (agent-visible signal based) ────────────────────

_OUTCOME_V2_INTERVENTIONS: dict[str, dict] = {
    "good_progress": {
        "must_not_do": [
            "Do NOT make unnecessary changes — target tests are passing",
        ],
        "must_do": [
            "Double-check for edge cases and regressions",
            "Run the full test suite to confirm no breakage",
        ],
        "hint_prefix": (
            "GOOD PROGRESS: Target failing tests appear to be passing. "
            "Verify there are no regressions, then submit. "
        ),
    },
    "partial_progress": {
        "must_not_do": [
            "Do NOT rewrite the entire fix — your approach is partially correct",
            "Do NOT change code paths that are already making tests pass",
        ],
        "must_do": [
            "Identify which FAIL_TO_PASS tests still fail and focus on those",
            "Extend the existing fix incrementally — do not start over",
            "Run tests after each change to confirm progress",
        ],
        "hint_prefix": (
            "PARTIAL PROGRESS: Some failing tests are fixed, but not all. "
            "Your direction is correct — extend the fix to cover remaining cases. "
            "Do NOT rewrite from scratch. "
        ),
    },
    "wrong_direction": {
        "must_not_do": [
            "Do NOT continue with the same approach — it fixed zero failing tests",
            "Do NOT expand the previous patch",
        ],
        "must_do": [
            "Re-read the failing tests carefully to understand expected behavior",
            "Consider a completely different root cause hypothesis",
            "Start with the simplest possible fix for the first failing test",
        ],
        "hint_prefix": (
            "WRONG DIRECTION: Your previous patch did not fix ANY of the target failing tests. "
            "Your approach is likely incorrect. Re-analyze the problem from scratch. "
            "Read the test expectations carefully before writing code. "
        ),
    },
    "regression": {
        "must_not_do": [
            "Do NOT continue with the same approach — it introduced new failures",
            "Do NOT weaken or remove existing constraints",
        ],
        "must_do": [
            "Revert your approach — you broke existing behavior",
            "Understand which existing tests broke and why",
            "Find a fix that preserves all existing passing tests",
        ],
        "hint_prefix": (
            "REGRESSION DETECTED: Your patch introduced new failing tests. "
            "You MUST preserve existing behavior. Revert and redesign your approach. "
        ),
    },
    "no_signal": {
        "must_not_do": [
            "Do NOT submit without verifying test results",
        ],
        "must_do": [
            "Check test setup and ensure correct test targeting",
            "Run the FAIL_TO_PASS tests explicitly",
        ],
        "hint_prefix": (
            "NO SIGNAL: Test execution did not produce useful results. "
            "Check test setup and ensure correct test targeting. "
        ),
    },
    "no_patch": {
        "must_not_do": [
            "Do NOT continue reading files without committing to a fix",
        ],
        "must_do": [
            "Immediately identify the target file and function from the failing tests",
            "Make the minimal change and submit — do not over-explore",
        ],
        "hint_prefix": (
            "NO PATCH: Previous attempt did not produce a patch. "
            "Go directly to the fix: identify file, make minimal change, submit. "
        ),
    },
}


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

# ── Outcome-based interventions (BUG-10: uses F2P/P2P signal) ────────────────

_OUTCOME_INTERVENTIONS: dict[str, dict] = {
    "partial_fix": {
        "must_not_do": [
            "Do NOT rewrite the entire fix — your approach is partially correct",
            "Do NOT change code paths that are already making tests pass",
        ],
        "must_do": [
            "Identify which FAIL_TO_PASS tests still fail and focus on those",
            "Extend the existing fix incrementally — do not start over",
            "Run tests after each change to confirm progress",
        ],
        "hint_prefix": (
            "PARTIAL FIX: Your previous patch fixed some failing tests but not all. "
            "Your direction is correct — extend the fix to cover remaining cases. "
            "Do NOT rewrite from scratch. "
        ),
    },
    "wrong_direction": {
        "must_not_do": [
            "Do NOT continue with the same approach — it fixed zero failing tests",
            "Do NOT expand the previous patch",
        ],
        "must_do": [
            "Re-read the failing tests carefully to understand expected behavior",
            "Consider a completely different root cause hypothesis",
            "Start with the simplest possible fix for the first failing test",
        ],
        "hint_prefix": (
            "WRONG DIRECTION: Your previous patch did not fix ANY of the failing tests. "
            "Your approach is likely incorrect. Re-analyze the problem from scratch. "
            "Read the test expectations carefully before writing code. "
        ),
    },
    "regression": {
        "must_not_do": [
            "Do NOT weaken or remove existing constraints",
            "Do NOT ignore tests that were passing before your change",
        ],
        "must_do": [
            "Revert your approach — you broke existing behavior",
            "Understand which existing tests broke and why",
            "Find a fix that preserves all existing passing tests",
        ],
        "hint_prefix": (
            "REGRESSION: Your previous patch broke existing tests that were passing. "
            "You MUST preserve all existing behavior. Revert your approach and find "
            "a fix that does not break any currently-passing tests. "
        ),
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
    tests_delta: Optional[int] = None,
    tests_passed_after: int = -1,
    controlled_verify: Optional[dict] = None,
    # v2 (no-oracle) signals — from inner-verify (apply_test_patch=False)
    patch_exists: bool = False,
    inner_f2p_passed: int = -1,
    inner_f2p_total: int = 0,
    inner_new_failures: int = 0,
) -> RetryPlan:
    """
    Deterministic failure classification → intervention mapping → RetryPlan.

    p177 extension: also computes control_action based on:
    - steps_since_last_signal (P7 no-signal detector, p164 runner layer)
    - principal_violation_codes (enforced principals only: ENV_LEAKAGE + PLAN_LOOP)

    p179 extension: tests_delta used in classify_failure_v2 for signal-aware bucketing.

    No LLM involved.
    """
    fp = patch_fp or {}
    failure_type = classify_failure(jingu_body, fp, prev_patch_fp, exec_feedback)
    failure_type_v2 = classify_failure_v2(jingu_body, fp, tests_delta, tests_passed_after)

    # Outcome classification: try v1 (oracle/F2P-based) first, fall back to v2 (agent-visible)
    outcome = classify_outcome(controlled_verify or {})
    outcome_intervention = _OUTCOME_INTERVENTIONS.get(outcome)
    outcome_version = "v1"

    # v2 (no-oracle): when v1 returns no_signal (inner-verify has no f2p/p2p), use agent-visible signals
    cv = controlled_verify or {}
    outcome_v2 = classify_outcome_v2(
        f2p_passed=inner_f2p_passed if inner_f2p_passed >= 0 else 0,
        f2p_total=inner_f2p_total,
        new_failures=inner_new_failures,
        patch_exists=patch_exists,
    )

    if outcome != "no_signal":
        # v1 has signal (F2P/P2P available — final-verify or oracle-assisted mode)
        print(f"    [outcome-engine] outcome={outcome} (v1/oracle)  f2p={cv.get('f2p_passed', '?')}/{(cv.get('f2p_passed', 0) or 0) + (cv.get('f2p_failed', 0) or 0)}  p2p_failed={cv.get('p2p_failed', '?')}", flush=True)
    else:
        # v2: use agent-visible F2P signal from inner-verify (no test_patch)
        outcome = outcome_v2
        outcome_version = "v2"
        outcome_intervention = _OUTCOME_V2_INTERVENTIONS.get(outcome)
        print(f"    [outcome-engine] outcome={outcome} (v2/agent-visible)  f2p_passed={inner_f2p_passed}/{inner_f2p_total}  new_failures={inner_new_failures}  patch={patch_exists}", flush=True)

    # Prefer outcome-based intervention when available
    if outcome_intervention:
        intervention = outcome_intervention
    else:
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

    # ── p178/p179: ε-greedy hint selection from strategy table ───────────────
    # Use v2 bucket key (signal-aware) as primary; fall back to v1 if v2 bucket empty
    bucket_key_v2 = make_bucket_key_v2(failure_type_v2, viol_codes)
    bucket_key = make_bucket_key(failure_type, viol_codes)
    table = _load_strategy_table(strategy_table_path)
    bucket_data = table.get(bucket_key_v2) or table.get(bucket_key, {})
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
        root_causes=[f"outcome={outcome}", f"outcome_version={outcome_version}", f"failure_type={failure_type}", f"failure_type_v2={failure_type_v2}"]
                    + [f"violation={c}" for c in viol_codes],
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
