"""Failure Classification Engine for jingu-swebench.

Classifies controlled_verify results into 4 failure types
to enable phase-specific retry routing.

Classification operates on the structured cv_result dict returned by
run_controlled_verify — never on raw text. This is a rule-based engine
(no LLM) that maps f2p/p2p signals to typed failure categories.

This module is SEPARATE from retry_controller.classify_outcome (outcome engine).
- outcome engine: agent-visible signal for the agent
- failure classifier: system-level routing for the control loop
"""
from typing import Literal, Optional

FailureType = Literal["wrong_direction", "incomplete_fix", "verify_gap", "execution_error"]

# Routing rules: each failure type maps to a next_phase + repair_goal + required_principals.
# p209 will consume these for retry routing; this plan (p208) only classifies and logs.
FAILURE_ROUTING_RULES: dict = {
    "wrong_direction": {
        "next_phase": "analysis",
        "repair_goal": "Re-analyze the actual root cause before proposing any fix.",
        "required_principals": ["causal_grounding", "evidence_linkage"],
    },
    "incomplete_fix": {
        "next_phase": "design",
        "repair_goal": "Refine the design to cover remaining failing scenarios.",
        "required_principals": ["minimal_change", "evidence_linkage"],
    },
    "verify_gap": {
        "next_phase": "judge",
        "repair_goal": "Determine whether verification scope is insufficient or evidence is incomplete.",
        "required_principals": ["evidence_linkage"],
    },
    "execution_error": {
        "next_phase": "execution",
        "repair_goal": "Fix execution issues without changing solution direction.",
        "required_principals": ["minimal_change", "action_grounding"],
    },
}


def classify_failure(cv_result: dict) -> Optional[FailureType]:
    """Classify a controlled_verify result into a failure type.

    Args:
        cv_result: dict from run_controlled_verify or cv_flat (jingu_body["controlled_verify"]).
            Expected keys: verification_kind, f2p_passed, f2p_failed, eval_resolved.

    Returns:
        One of the 4 FailureType values, or None if the result indicates success.

    Classification rules (evaluated in order):
        1. verification_kind == "controlled_error" -> "execution_error"
        2. f2p_passed > 0 AND f2p_failed > 0 -> "incomplete_fix"
        3. f2p_passed == 0 AND f2p_failed > 0 -> "wrong_direction"
        4. f2p_passed > 0 AND f2p_failed == 0 -> "verify_gap" (if not resolved)
        5. eval_resolved == True -> None (success, no failure to classify)
        6. fallback -> "wrong_direction"
    """
    if not cv_result or not isinstance(cv_result, dict):
        return None

    # Rule 1: execution error (patch apply failure, docker error, etc.)
    vk = cv_result.get("verification_kind", "")
    if vk == "controlled_error":
        return "execution_error"

    # No tests available — cannot classify
    if vk == "controlled_no_tests":
        return None

    # Extract f2p counts (default to 0 for missing/None values)
    f2p_passed = cv_result.get("f2p_passed") or 0
    f2p_failed = cv_result.get("f2p_failed") or 0

    # Rule 2: partial fix — some f2p pass, some fail
    if f2p_passed > 0 and f2p_failed > 0:
        return "incomplete_fix"

    # Rule 3: wrong direction — no f2p tests pass
    if f2p_passed == 0 and f2p_failed > 0:
        return "wrong_direction"

    # Rule 4: verify gap — all f2p pass but not marked resolved
    # (p2p regression or other eval criteria not met)
    if f2p_passed > 0 and f2p_failed == 0:
        if cv_result.get("eval_resolved") is True:
            return None  # success
        return "verify_gap"

    # Rule 5: all zeros or no f2p data — check eval_resolved
    if cv_result.get("eval_resolved") is True:
        return None

    # Fallback: unknown state -> treat as wrong_direction
    return "wrong_direction"


def get_routing(failure_type: FailureType) -> dict:
    """Get the routing rule for a failure type.

    Args:
        failure_type: one of the 4 FailureType values.

    Returns:
        dict with next_phase, repair_goal, required_principals.

    Raises:
        KeyError: if failure_type is not a valid FailureType.
    """
    return FAILURE_ROUTING_RULES[failure_type]
