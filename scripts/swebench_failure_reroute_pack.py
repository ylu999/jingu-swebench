"""
SWE-bench Failure Reroute Pack — first GovernancePack (SWE-bench specialized).

Covers: FailureSignal → Recognition → Reroute for F2P_ALL_FAIL and F2P_PARTIAL.

Architecture (p26 + p27 ADR):
  - SWE-bench specialized: uses F2P/P2P semantics, controlled_verify counts
  - Do NOT lift to generic jingu layer until proven effective on p25/p26 data
  - Promotion path: F2P_ALL_FAIL count visibly declining → extract → generic

This is the Minimum Viable GovernancePack v0 (p27 ADR priority 1).
"""
from __future__ import annotations

from governance_pack import (
    ExecutionContext,
    FailureSignal,
    GovernancePack,
    RecognitionResult,
    RouteDecision,
)
from unresolved_case_classifier import classify_unresolved


# ── Step 3: parse_failure ──────────────────────────────────────────────────────

def _parse_failure(ctx: ExecutionContext) -> FailureSignal | None:
    """
    Extract FailureSignal from controlled_verify counts.

    Uses trust=100 controlled_passed/controlled_failed as primary signal.
    Returns None if controlled_verify not available (no signal to parse).
    """
    if not ctx.controlled_verify_available:
        return None

    cp = ctx.controlled_passed
    cf = ctx.controlled_failed

    if cf == 0:
        # All target tests pass — no failure signal
        return None

    # Classify failure type from counts
    if cp == 0:
        failure_type = "F2P_ALL_FAIL"
    else:
        failure_type = "F2P_PARTIAL"

    # Failing tests: the full FAIL_TO_PASS list when all fail, else unknown subset
    # (controlled_verify gives counts, not names — use full list as proxy)
    failing_tests = ctx.fail_to_pass if cp == 0 else ctx.fail_to_pass

    return FailureSignal(
        failure_type=failure_type,
        controlled_passed=cp,
        controlled_failed=cf,
        failing_tests=failing_tests,
        raw_excerpt=ctx.excerpt,
    )


# ── Step 4: recognize ─────────────────────────────────────────────────────────

def _recognize(signal: FailureSignal) -> RecognitionResult | None:
    """
    Map FailureSignal → RecognitionResult (behavioral state + next phase).

    F2P_ALL_FAIL → wrong_direction → ANALYZE
      Rationale: if ALL target tests fail, the fix direction is incorrect.
      Agent must re-analyze from scratch, not expand current patch.

    F2P_PARTIAL → insufficient_coverage → EXECUTE
      Rationale: some tests pass, direction is correct but coverage insufficient.
      Agent should extend the patch, not restart analysis.
    """
    if signal.failure_type == "F2P_ALL_FAIL":
        return RecognitionResult(
            state="wrong_direction",
            confidence=0.9,
            next_phase="ANALYZE",
            reason=(
                f"All {signal.controlled_failed} target FAIL_TO_PASS tests still failing "
                f"(controlled_passed={signal.controlled_passed}). "
                f"Fix direction is likely incorrect."
            ),
        )

    if signal.failure_type == "F2P_PARTIAL":
        return RecognitionResult(
            state="insufficient_coverage",
            confidence=0.8,
            next_phase="EXECUTE",
            reason=(
                f"{signal.controlled_passed} target tests pass, "
                f"{signal.controlled_failed} still failing. "
                f"Direction correct but coverage insufficient."
            ),
        )

    return None


# ── Step 5: route ──────────────────────────────────────────────────────────────

def _build_wrong_direction_hint(signal: FailureSignal, ctx: ExecutionContext) -> str:
    # p207-P13: use unresolved case classifier for targeted hints
    classification = classify_unresolved(ctx.jingu_body, signal.failing_tests)
    category = classification["category"]
    confidence = classification["confidence"]
    signals_str = "; ".join(classification["signals"][:3])
    print(
        f"    [unresolved_classifier] category={category} confidence={confidence:.2f} "
        f"signals=[{signals_str}]"
    )
    # Use the classifier's targeted hint instead of generic one
    return classification["hint"]


def _build_coverage_hint(signal: FailureSignal, ctx: ExecutionContext) -> str:
    test_names = ", ".join(signal.failing_tests[:5])
    if len(signal.failing_tests) > 5:
        test_names += f" ... (+{len(signal.failing_tests) - 5} more)"
    return (
        f"[JINGU ROUTING] F2P_PARTIAL (attempt {ctx.attempt}): "
        f"{signal.controlled_passed} target tests pass, {signal.controlled_failed} still failing. "
        f"Your direction is correct but coverage is insufficient. "
        f"Identify which branches or cases are uncovered, then extend the patch. "
        f"Still failing: {test_names}"
    )


def _route(recog: RecognitionResult, ctx: ExecutionContext) -> RouteDecision | None:
    # Need the original signal to build the hint — reconstruct from recog context
    # (In v0: re-parse to get signal. v1: pass signal through.)
    signal = _parse_failure(ctx)
    if signal is None:
        return None

    if recog.state == "wrong_direction":
        return RouteDecision(
            action="REROUTE",
            target_phase="ANALYZE",
            hint=_build_wrong_direction_hint(signal, ctx),
        )

    if recog.state == "insufficient_coverage":
        return RouteDecision(
            action="REROUTE",
            target_phase="EXECUTE",
            hint=_build_coverage_hint(signal, ctx),
        )

    return RouteDecision(action="CONTINUE")


# ── Pack definition ────────────────────────────────────────────────────────────

SWEBENCH_FAILURE_REROUTE_PACK = GovernancePack(
    name="swebench_failure_reroute_v0",

    # Step 1: response/state fields this pack requires
    required_state_fields=[
        "test_results.controlled_passed",
        "test_results.controlled_failed",
        "test_results.excerpt",
    ],

    # Step 2: prompt extension (injected when pack is installed)
    prompt_extensions=[
        (
            "After executing your fix, the system will run official FAIL_TO_PASS tests. "
            "If all target tests fail, you will be routed back to analysis — "
            "do NOT continue patching in the same direction. "
            "If some tests pass, extend your patch to cover the remaining failures."
        )
    ],

    # Steps 3–5: functional chain
    parse_failure=_parse_failure,
    recognize=_recognize,
    route=_route,
)
