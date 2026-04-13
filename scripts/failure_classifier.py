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

# ── Failure Layer (semantic rootcause) ────────────────────────────────────────
# FailureType answers "what happened" (routing-level).
# FailureLayer answers "why it happened" (semantic rootcause).
#
# These two are orthogonal:
#   incomplete_fix + near_miss_semantic_insufficiency → almost right, edge cases uncovered
#   incomplete_fix + multi_site_fix_incomplete → right direction, didn't hit all change sites
#   verify_gap + target_only_success_with_regression → target passed but P2P broke
#   wrong_direction + insufficient_design_depth → jumped to patch without understanding
#
FailureLayer = Literal[
    "near_miss_semantic_insufficiency",      # patch almost correct, local F2P coverage gap
    "insufficient_design_depth",             # problem requires deeper analysis/design than attempted
    "multi_site_fix_incomplete",             # correct direction but only materialized partial change sites
    "target_only_success_with_regression",   # target F2P pass but P2P regression introduced
    "target_missing_due_to_test_resolution", # quick judge couldn't resolve target test name
    "wrong_direction",                       # fundamentally wrong approach
    "execution_error",                       # patch format / apply / docker failure
    "unknown",                               # insufficient signal to classify
]

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


# ── Failure Layer Classification ──────────────────────────────────────────────

def classify_failure_layer(
    cv_result: dict,
    quick_judge_history: list[dict] | None = None,
    failure_type: Optional[FailureType] = None,
) -> FailureLayer:
    """Classify the semantic rootcause layer for an unresolved instance.

    Combines controlled_verify signals with quick_judge target-aware signals
    to determine WHY the patch failed, not just WHAT happened.

    Args:
        cv_result: dict from controlled_verify with f2p/p2p counts.
        quick_judge_history: list of quick judge result dicts (target_status, signal_kind).
        failure_type: pre-classified FailureType from classify_failure().

    Returns:
        One of the FailureLayer values.

    Classification rules (evaluated in order of specificity):
        1. execution_error → "execution_error"
        2. F2P all pass + P2P regression → "target_only_success_with_regression"
        3. F2P partial pass (high ratio) + QJ target_passed → "near_miss_semantic_insufficiency"
        4. F2P partial pass (low ratio) + multi-file F2P → "multi_site_fix_incomplete"
        5. F2P zero pass + QJ target_missing → "target_missing_due_to_test_resolution"
        6. F2P zero pass + agent had patch → "insufficient_design_depth" or "wrong_direction"
        7. fallback → "unknown"
    """
    if not cv_result or not isinstance(cv_result, dict):
        return "unknown"

    # Shortcut: execution error
    if failure_type == "execution_error":
        return "execution_error"

    f2p_passed = cv_result.get("f2p_passed") or 0
    f2p_failed = cv_result.get("f2p_failed") or 0
    f2p_total = f2p_passed + f2p_failed
    p2p_passed = cv_result.get("p2p_passed") or 0
    p2p_failed = cv_result.get("p2p_failed") or 0

    # Collect quick judge signals
    qj_target_statuses = []
    qj_has_target_missing = False
    if quick_judge_history:
        qj_target_statuses = [qj.get("target_status", "unknown") for qj in quick_judge_history]
        qj_has_target_missing = "missing" in qj_target_statuses

    # Rule 1: All F2P pass but P2P regression → target_only_success_with_regression
    if f2p_total > 0 and f2p_failed == 0 and p2p_failed > 0:
        return "target_only_success_with_regression"

    # Rule 2: High F2P pass ratio (>= 50%) → near_miss or multi_site
    if f2p_total > 0 and f2p_passed > 0 and f2p_failed > 0:
        pass_ratio = f2p_passed / f2p_total
        if pass_ratio >= 0.5:
            # Most F2P pass — near miss (edge case coverage gap)
            return "near_miss_semantic_insufficiency"
        else:
            # Low ratio — likely missing change sites
            return "multi_site_fix_incomplete"

    # Rule 3: Zero F2P pass
    if f2p_passed == 0 and f2p_total > 0:
        # Did quick judge report target_missing? → test resolution problem
        if qj_has_target_missing:
            return "target_missing_due_to_test_resolution"
        # Agent wrote a patch but completely wrong direction
        return "wrong_direction"

    # Rule 4: verify_gap with all F2P passing but not resolved (no P2P regression detected)
    # This can happen when eval harness uses different criteria
    if failure_type == "verify_gap":
        return "near_miss_semantic_insufficiency"

    return "unknown"
