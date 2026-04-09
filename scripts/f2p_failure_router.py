"""
F2P Failure Router — SWE-bench specialized (jingu-swebench layer).

Detects F2P_ALL_FAIL and F2P_PARTIAL outcomes from controlled_verify signals
and injects phase-reroute directives into the retry plan.

Architecture note (p26 ADR):
  This module is SWE-bench specialized — uses F2P/P2P semantics, controlled_verify,
  pytest failure names. Do NOT lift to generic jingu layer until pattern is proven
  effective on the 35 F2P_ALL_FAIL cases.

Promotion path: proven effective → extract pattern → lift to jingu generic skeleton.
"""
from __future__ import annotations

from typing import Optional

from retry_controller import RetryPlan


# ── F2P outcome classification ────────────────────────────────────────────────

def classify_f2p_outcome(
    controlled_passed: int,
    controlled_failed: int,
) -> str:
    """
    Classify the F2P outcome from controlled_verify counts.

    Returns:
        "wrong_direction"   — all target tests still failing (controlled_passed == 0)
        "partial_progress"  — some pass, some still fail (controlled_passed > 0)
        "resolved"          — all target tests pass (controlled_failed == 0)
        "no_signal"         — counts unavailable (both -1 or None)
    """
    if controlled_passed < 0 or controlled_failed < 0:
        return "no_signal"
    if controlled_failed == 0:
        return "resolved"
    if controlled_passed == 0:
        return "wrong_direction"
    return "partial_progress"


# ── Reroute hint builder ───────────────────────────────────────────────────────

def build_f2p_reroute_hint(
    failed_tests: list[str],
    classification: str,
    attempt: int = 1,
) -> str:
    """
    Build a phase-reroute directive for the next attempt.

    For wrong_direction: force re-analysis from scratch, forbid patch expansion.
    For partial_progress: focus on residual failing tests, extend coverage.
    """
    test_names = ", ".join(failed_tests[:5])
    if len(failed_tests) > 5:
        test_names += f" ... (+{len(failed_tests) - 5} more)"

    if classification == "wrong_direction":
        return (
            f"[JINGU ROUTING] F2P_ALL_FAIL — attempt {attempt}: "
            f"ALL target FAIL_TO_PASS tests are still failing. "
            f"Your fix direction is incorrect. "
            f"DO NOT expand the current patch. "
            f"Re-analyze the root cause from scratch: "
            f"read the failing tests carefully to understand what behavior is expected, "
            f"then locate and fix the actual source of the bug. "
            f"Tests that must pass: {test_names}"
        )

    if classification == "partial_progress":
        return (
            f"[JINGU ROUTING] F2P_PARTIAL — attempt {attempt}: "
            f"Some target tests pass but not all. "
            f"Your fix is in the right direction but has insufficient coverage. "
            f"Identify which branches or cases are still uncovered. "
            f"Extend the patch to cover the residual failures. "
            f"Still failing: {test_names}"
        )

    return ""


# ── Main injection function ───────────────────────────────────────────────────

def apply_f2p_override(
    retry_plan: RetryPlan,
    controlled_passed: int,
    controlled_failed: int,
    fail_to_pass_tests: list[str],
    attempt: int = 1,
) -> tuple[RetryPlan, str]:
    """
    Override retry_plan with F2P-specific routing if applicable.

    Returns (updated_retry_plan, f2p_classification).

    Only overrides when classification is wrong_direction or partial_progress.
    Does not modify if resolved or no_signal.
    """
    classification = classify_f2p_outcome(controlled_passed, controlled_failed)

    if classification in ("resolved", "no_signal"):
        return retry_plan, classification

    hint = build_f2p_reroute_hint(fail_to_pass_tests, classification, attempt)
    if not hint:
        return retry_plan, classification

    if classification == "wrong_direction":
        updated = RetryPlan(
            root_causes=retry_plan.root_causes + [
                f"f2p_class=wrong_direction",
                f"controlled_failed={controlled_failed}",
                f"controlled_passed={controlled_passed}",
            ],
            must_do=[
                "Re-read the failing FAIL_TO_PASS tests to understand expected behavior",
                "Identify the correct source location for the bug",
                "Write a patch that targets the root cause, not symptoms",
            ],
            must_not_do=[
                "Do not expand or continue the current patch direction",
                "Do not add workarounds or suppress errors",
            ],
            validation_requirement=(
                f"Run FAIL_TO_PASS tests and confirm controlled_passed > 0"
            ),
            next_attempt_prompt=hint[:600],
            control_action="ADJUST",
            principal_violations=retry_plan.principal_violations,
        )
    else:  # partial_progress
        updated = RetryPlan(
            root_causes=retry_plan.root_causes + [
                f"f2p_class=partial_progress",
                f"controlled_failed={controlled_failed}",
                f"controlled_passed={controlled_passed}",
            ],
            must_do=[
                "Identify which test cases are still failing and why",
                "Extend the patch to cover the uncovered branches",
            ],
            must_not_do=retry_plan.must_not_do,
            validation_requirement=(
                f"Run all FAIL_TO_PASS tests and confirm controlled_failed == 0"
            ),
            next_attempt_prompt=hint[:600],
            control_action="ADJUST",
            principal_violations=retry_plan.principal_violations,
        )

    return updated, classification
