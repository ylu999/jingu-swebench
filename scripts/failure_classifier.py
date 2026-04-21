"""Failure Classification Engine for jingu-swebench.

Two layers of failure classification:
  1. FailureType (routing-level): WHAT happened — 4 categories for phase-specific retry routing.
  2. FailureRecord (semantic rootcause): WHY it happened — structured record with phase_of_failure,
     signal_quality, confidence, and recommended_actions for the control plane.

FailureType is consumed by repair_prompts.py for retry routing.
FailureRecord is consumed by the control plane for failure-aware cognition control.
"""
from dataclasses import dataclass, field, asdict
from typing import Literal, Optional

# ── FailureType (routing-level, unchanged) ────────────────────────────────────

FailureType = Literal["wrong_direction", "incomplete_fix", "verify_gap", "execution_error", "near_miss"]

# ── v0.3: Near-Miss Repair State ──────────────────────────────────────────────

RepairMode = Literal[
    "broad_repair",           # wrong_direction / incomplete_fix — full re-analysis
    "incremental_extension",  # incomplete_fix with partial progress
    "residual_gap_repair",    # near_miss — constrained surgical fix
    "redesign_required",      # verify_gap — all f2p pass but p2p regression
]


@dataclass
class NearMissState:
    """v0.3: Extended near-miss classification with stall/backslide detection.

    Consumed by repair_prompts.py for structured 3-step repair protocol
    and by jingu_agent.py for dynamic routing (stall → DESIGN).
    """
    failure_type: FailureType
    repair_mode: RepairMode
    likely_correct_direction: bool  # True if f2p_passed > 0 (some progress)
    residual_gap_size: int         # f2p_failed count
    f2p_passed: int
    f2p_total: int
    p2p_failed: int

    # Stall detection (same f2p across attempts)
    same_patch_suspected: bool = False
    stall_consecutive: int = 0     # how many consecutive attempts with same f2p

    # Backslide detection (f2p decreased vs best)
    backslide_detected: bool = False
    best_f2p_passed: int = 0       # best f2p_passed seen across all attempts
    best_attempt: int = 0          # which attempt achieved best f2p

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def needs_redesign(self) -> bool:
        """True if stall or backslide warrants routing to DESIGN instead of EXECUTE."""
        return self.same_patch_suspected or self.backslide_detected


def classify_near_miss_state(
    cv_result: dict,
    attempt: int,
    f2p_history: list[tuple[int, int]] | None = None,
) -> NearMissState | None:
    """Build NearMissState from CV result + attempt history.

    Args:
        cv_result: controlled_verify result dict.
        attempt: current attempt number (1-based).
        f2p_history: list of (f2p_passed, f2p_total) tuples from previous attempts.
            Index 0 = attempt 1, etc. None if no history.

    Returns:
        NearMissState if failure_type is near_miss, None otherwise.
    """
    ft = classify_failure(cv_result)
    if ft != "near_miss":
        return None

    f2p_passed = cv_result.get("f2p_passed") or 0
    f2p_failed = cv_result.get("f2p_failed") or 0
    f2p_total = f2p_passed + f2p_failed
    p2p_failed = cv_result.get("p2p_failed") or 0

    # Stall detection: same f2p_passed as previous attempt
    same_patch_suspected = False
    stall_consecutive = 0
    if f2p_history:
        # Count consecutive same-f2p from most recent backward
        for prev_passed, _prev_total in reversed(f2p_history):
            if prev_passed == f2p_passed:
                stall_consecutive += 1
            else:
                break
        same_patch_suspected = stall_consecutive >= 1  # 1+ prior attempt with same f2p

    # Backslide detection: f2p_passed < best seen
    backslide_detected = False
    best_f2p_passed = f2p_passed
    best_attempt = attempt
    if f2p_history:
        for idx, (prev_passed, _prev_total) in enumerate(f2p_history):
            if prev_passed > best_f2p_passed:
                best_f2p_passed = prev_passed
                best_attempt = idx + 1  # 1-based attempt number
        backslide_detected = f2p_passed < best_f2p_passed

    return NearMissState(
        failure_type="near_miss",
        repair_mode="residual_gap_repair",
        likely_correct_direction=True,
        residual_gap_size=f2p_failed,
        f2p_passed=f2p_passed,
        f2p_total=f2p_total,
        p2p_failed=p2p_failed,
        same_patch_suspected=same_patch_suspected,
        stall_consecutive=stall_consecutive,
        backslide_detected=backslide_detected,
        best_f2p_passed=best_f2p_passed,
        best_attempt=best_attempt,
    )


def get_near_miss_routing(nm_state: NearMissState) -> dict:
    """Dynamic routing for near_miss based on stall/backslide state.

    Normal near_miss → EXECUTE (surgical fix).
    Stall (same f2p 2+ attempts) → DESIGN (micro-redesign).
    Backslide (f2p decreased) → DESIGN (micro-redesign).
    """
    if nm_state.needs_redesign:
        return {
            "next_phase": "DESIGN",
            "repair_goal": (
                "Your near-miss repair is STALLED or BACKSLIDING. "
                "The previous surgical approach is not working. "
                "You must MICRO-REDESIGN: identify WHY the residual tests resist "
                "your current approach, then design a different mechanism for "
                "the remaining gap ONLY."
            ),
            "required_principals": _principals_for_phase("DESIGN"),
        }
    return FAILURE_ROUTING_RULES["near_miss"]


def _principals_for_phase(phase: str) -> list[str]:
    """Derive required_principals from contract_registry (SST: no hardcoded copy)."""
    try:
        from contract_registry import get_required_principals
        return list(get_required_principals(phase))
    except Exception:
        return []  # SST2: empty fallback, not stale copy

FAILURE_ROUTING_RULES: dict = {
    "wrong_direction": {
        "next_phase": "ANALYZE",
        "repair_goal": "Re-analyze the actual root cause before proposing any fix.",
        "required_principals": _principals_for_phase("ANALYZE"),
    },
    "incomplete_fix": {
        "next_phase": "DESIGN",
        "repair_goal": "Refine the design to cover remaining failing scenarios.",
        "required_principals": _principals_for_phase("DESIGN"),
    },
    "verify_gap": {
        "next_phase": "DESIGN",
        "repair_goal": "Your fix WORKS (target tests pass) but BROKE an existing test. You must REDESIGN your approach — the previous patch direction causes an unavoidable regression. Analyze the failing test to understand what constraint your fix violates, then design a fundamentally different fix strategy that satisfies BOTH the target tests AND the existing tests.",
        "required_principals": _principals_for_phase("DESIGN"),
    },
    "execution_error": {
        "next_phase": "EXECUTE",
        "repair_goal": "Fix execution issues without changing solution direction.",
        "required_principals": _principals_for_phase("EXECUTE"),
    },
    "near_miss": {
        "next_phase": "EXECUTE",
        "repair_goal": (
            "Your fix is ALMOST correct — most target tests pass. "
            "Only a few tests remain failing. DO NOT change direction. "
            "Make the SMALLEST possible patch to fix the remaining failures."
        ),
        "required_principals": _principals_for_phase("EXECUTE"),
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

    vk = cv_result.get("verification_kind", "")
    if vk == "controlled_error":
        return "execution_error"

    if vk == "controlled_no_tests":
        return None

    f2p_passed = cv_result.get("f2p_passed") or 0
    f2p_failed = cv_result.get("f2p_failed") or 0
    f2p_total = f2p_passed + f2p_failed
    p2p_failed = cv_result.get("p2p_failed") or 0

    # near_miss: high pass rate (>=80%), few remaining failures (<=5), no regression
    if (f2p_total > 0 and f2p_passed > 0 and f2p_failed > 0
            and f2p_passed / f2p_total >= 0.80
            and f2p_failed <= 5
            and p2p_failed == 0):
        return "near_miss"

    if f2p_passed > 0 and f2p_failed > 0:
        return "incomplete_fix"

    if f2p_passed == 0 and f2p_failed > 0:
        return "wrong_direction"

    if f2p_passed > 0 and f2p_failed == 0:
        if cv_result.get("eval_resolved") is True:
            return None
        return "verify_gap"

    if cv_result.get("eval_resolved") is True:
        return None

    return "wrong_direction"


def get_repair_mode(failure_type: FailureType) -> RepairMode:
    """Map FailureType to RepairMode (v0.3)."""
    _FT_TO_RM: dict[str, RepairMode] = {
        "wrong_direction": "broad_repair",
        "incomplete_fix": "incremental_extension",
        "verify_gap": "redesign_required",
        "execution_error": "broad_repair",
        "near_miss": "residual_gap_repair",
    }
    return _FT_TO_RM.get(failure_type, "broad_repair")


def get_routing(failure_type: FailureType) -> dict:
    """Get the routing rule for a failure type."""
    return FAILURE_ROUTING_RULES[failure_type]


def get_routing_decision(failure_type: FailureType) -> "RoutingDecision | None":
    """Get a typed RoutingDecision for a failure type (EF-5).

    Returns RoutingDecision or None if import fails.
    """
    rule = FAILURE_ROUTING_RULES.get(failure_type)
    if not rule:
        return None
    try:
        from routing_decision import RoutingDecision
        return RoutingDecision(
            next_phase=rule["next_phase"],
            strategy=failure_type,
            repair_hints=[rule["repair_goal"]],
            source="failure_type_route",
        )
    except Exception:
        return None


# ── FailureLayer (semantic rootcause) ─────────────────────────────────────────

FailureLayer = Literal[
    "near_miss_semantic_insufficiency",
    "insufficient_design_depth",
    "multi_site_fix_incomplete",
    "target_only_success_with_regression",
    "target_missing_due_to_test_resolution",
    "wrong_direction",
    "execution_error",
    "unknown",
]

PhaseOfFailure = Literal["ANALYZE", "DECIDE", "DESIGN", "EXECUTE", "JUDGE"]

SignalQuality = Literal[
    "true_positive",   # signal correctly indicated success
    "true_negative",   # signal correctly indicated failure
    "false_positive",  # signal indicated success but actually failed
    "false_negative",  # signal indicated failure but actually succeeded
    "no_signal",       # signal not available
]

ActionType = Literal[
    "retry_phase",
    "enforce_principals",
    "require_design_expansion",
    "require_affected_surface_enumeration",
    "require_regression_sentinel",
    "require_test_resolution_fix",
    "block_submission",
    "increase_analysis_budget",
]


@dataclass
class GateAction:
    """A recommended action the control plane should take."""
    type: ActionType
    phase: Optional[str] = None          # for retry_phase
    principals: list[str] = field(default_factory=list)  # for enforce_principals
    reason: str = ""

    def to_dict(self) -> dict:
        d: dict = {"type": self.type}
        if self.phase:
            d["phase"] = self.phase
        if self.principals:
            d["principals"] = self.principals
        if self.reason:
            d["reason"] = self.reason
        return d


@dataclass
class SignalQualityRecord:
    """Quality assessment of each signal source for this instance."""
    quick_judge_target: SignalQuality = "no_signal"
    quick_judge_overall: SignalQuality = "no_signal"
    controlled_verify: SignalQuality = "no_signal"

    def to_dict(self) -> dict:
        return {
            "quick_judge_target": self.quick_judge_target,
            "quick_judge_overall": self.quick_judge_overall,
            "controlled_verify": self.controlled_verify,
        }


@dataclass
class FailureRecord:
    """Full semantic rootcause record for an unresolved instance.

    This is the structured object that enters the control plane —
    not just a label, but a complete diagnostic with routing instructions.
    """
    instance_id: str
    failure_layer: FailureLayer
    phase_of_failure: PhaseOfFailure
    signal_quality: SignalQualityRecord
    confidence: float
    reasoning: str
    recommended_actions: list[GateAction]

    # Raw signals that drove the classification
    signals: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "failure_layer": self.failure_layer,
            "phase_of_failure": self.phase_of_failure,
            "signal_quality": self.signal_quality.to_dict(),
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "recommended_actions": [a.to_dict() for a in self.recommended_actions],
            "signals": self.signals,
        }


# ── FailureRecord Classification ─────────────────────────────────────────────

def classify_failure_layer(
    cv_result: dict,
    quick_judge_history: list[dict] | None = None,
    failure_type: Optional[FailureType] = None,
    instance_id: str = "",
) -> FailureRecord:
    """Classify the full semantic rootcause for an unresolved instance.

    Combines controlled_verify signals with quick_judge target-aware signals
    to produce a FailureRecord with phase, signal quality, and recommended actions.

    Args:
        cv_result: dict from controlled_verify with f2p/p2p counts.
        quick_judge_history: list of quick judge result dicts.
        failure_type: pre-classified FailureType from classify_failure().
        instance_id: instance identifier for the record.

    Returns:
        FailureRecord with full diagnostic.
    """
    if not cv_result or not isinstance(cv_result, dict):
        return FailureRecord(
            instance_id=instance_id,
            failure_layer="unknown",
            phase_of_failure="ANALYZE",
            signal_quality=SignalQualityRecord(),
            confidence=0.0,
            reasoning="No controlled_verify data available",
            recommended_actions=[],
        )

    f2p_passed = cv_result.get("f2p_passed") or 0
    f2p_failed = cv_result.get("f2p_failed") or 0
    f2p_total = f2p_passed + f2p_failed
    p2p_passed = cv_result.get("p2p_passed") or 0
    p2p_failed = cv_result.get("p2p_failed") or 0
    p2p_total = p2p_passed + p2p_failed

    # Collect quick judge signals
    qj_target_statuses = []
    qj_has_target_missing = False
    qj_has_target_passed = False
    qj_has_target_error = False
    if quick_judge_history:
        qj_target_statuses = [qj.get("target_status", "unknown") for qj in quick_judge_history]
        qj_has_target_missing = "missing" in qj_target_statuses
        qj_has_target_passed = "passed" in qj_target_statuses
        qj_has_target_error = "error" in qj_target_statuses or "failed" in qj_target_statuses

    raw_signals = {
        "f2p_passed": f2p_passed,
        "f2p_failed": f2p_failed,
        "f2p_total": f2p_total,
        "p2p_passed": p2p_passed,
        "p2p_failed": p2p_failed,
        "p2p_total": p2p_total,
        "qj_target_statuses": qj_target_statuses,
        "failure_type": failure_type,
    }

    # ── Rule 0: execution_error ───────────────────────────────────────────
    if failure_type == "execution_error":
        return FailureRecord(
            instance_id=instance_id,
            failure_layer="execution_error",
            phase_of_failure="EXECUTE",
            signal_quality=SignalQualityRecord(
                controlled_verify="true_negative",
            ),
            confidence=0.95,
            reasoning="Patch apply or docker execution failure",
            recommended_actions=[
                GateAction(type="retry_phase", phase="EXECUTE",
                           reason="Fix execution issues without changing direction"),
            ],
            signals=raw_signals,
        )

    # ── Rule 1: F2P all pass + P2P regression → regression with false success ─
    if f2p_total > 0 and f2p_failed == 0 and p2p_failed > 0:
        sq = SignalQualityRecord(
            quick_judge_target="true_positive" if qj_has_target_passed else "no_signal",
            quick_judge_overall="false_positive",  # QJ said "ok" but patch is broken
            controlled_verify="true_negative",     # CV correctly caught the regression
        )
        return FailureRecord(
            instance_id=instance_id,
            failure_layer="target_only_success_with_regression",
            phase_of_failure="JUDGE",
            signal_quality=sq,
            confidence=0.95,
            reasoning=(
                f"Target F2P all passed ({f2p_passed}/{f2p_total}) but P2P regression "
                f"detected ({p2p_failed}/{p2p_total} failed). Patch likely weakens a "
                f"condition or mutates shared state instead of preserving invariants."
            ),
            recommended_actions=[
                GateAction(type="require_regression_sentinel",
                           reason="Quick judge must check P2P sentinel tests, not just target"),
                GateAction(type="retry_phase", phase="DESIGN",
                           reason="Redesign fix to preserve invariants (clone vs mutate)"),
                GateAction(type="enforce_principals",
                           principals=["invariant_preservation", "minimal_change"],
                           reason="Enforce that fix does not weaken existing guards"),
            ],
            signals=raw_signals,
        )

    # ── Rule 2: Partial F2P pass, high ratio → near miss ─────────────────
    if f2p_total > 0 and f2p_passed > 0 and f2p_failed > 0:
        pass_ratio = f2p_passed / f2p_total

        if pass_ratio >= 0.5:
            # Near miss — most F2P pass, edge cases uncovered
            sq = SignalQualityRecord(
                quick_judge_target="true_positive" if qj_has_target_passed else "no_signal",
                quick_judge_overall="true_positive" if qj_has_target_passed else "no_signal",
                controlled_verify="true_negative",
            )
            return FailureRecord(
                instance_id=instance_id,
                failure_layer="near_miss_semantic_insufficiency",
                phase_of_failure="EXECUTE",
                signal_quality=sq,
                confidence=0.85,
                reasoning=(
                    f"Patch covers {f2p_passed}/{f2p_total} F2P tests ({pass_ratio:.0%}). "
                    f"Remaining {f2p_failed} failures are likely edge cases in the same fix family. "
                    f"No P2P regression."
                ),
                recommended_actions=[
                    GateAction(type="retry_phase", phase="EXECUTE",
                               reason="Patch is close — fix remaining edge cases"),
                    GateAction(type="require_design_expansion",
                               reason="Check uncovered F2P cases for missing semantic coverage"),
                ],
                signals=raw_signals,
            )
        else:
            # Low pass ratio — likely missing change sites
            sq = SignalQualityRecord(
                quick_judge_target="true_positive" if qj_has_target_passed else
                                   ("true_negative" if qj_has_target_error else "no_signal"),
                quick_judge_overall="true_negative" if qj_has_target_error else "no_signal",
                controlled_verify="true_negative",
            )
            return FailureRecord(
                instance_id=instance_id,
                failure_layer="multi_site_fix_incomplete",
                phase_of_failure="DESIGN",
                signal_quality=sq,
                confidence=0.80,
                reasoning=(
                    f"Only {f2p_passed}/{f2p_total} F2P pass ({pass_ratio:.0%}). "
                    f"Low coverage suggests multiple change sites required but only "
                    f"partial sites materialized."
                ),
                recommended_actions=[
                    GateAction(type="require_affected_surface_enumeration",
                               reason="Design must enumerate all affected files/components"),
                    GateAction(type="retry_phase", phase="DESIGN",
                               reason="Expand design to cover all required change sites"),
                    GateAction(type="enforce_principals",
                               principals=["evidence_linkage", "minimal_change"],
                               reason="Ensure all change sites are evidence-grounded"),
                ],
                signals=raw_signals,
            )

    # ── Rule 3: Zero F2P pass ────────────────────────────────────────────
    if f2p_passed == 0 and f2p_total > 0:
        # Sub-rule: quick judge couldn't resolve target → test resolution issue
        if qj_has_target_missing:
            return FailureRecord(
                instance_id=instance_id,
                failure_layer="target_missing_due_to_test_resolution",
                phase_of_failure="JUDGE",
                signal_quality=SignalQualityRecord(
                    quick_judge_target="no_signal",
                    quick_judge_overall="no_signal",
                    controlled_verify="true_negative",
                ),
                confidence=0.85,
                reasoning=(
                    f"All {f2p_total} F2P tests failed. Quick judge reported target_missing — "
                    f"test name resolution failed (possibly docstring-based test name)."
                ),
                recommended_actions=[
                    GateAction(type="require_test_resolution_fix",
                               reason="Quick judge cannot resolve target test — fix canonicalization"),
                    GateAction(type="retry_phase", phase="ANALYZE",
                               reason="Re-analyze with correct test identity"),
                ],
                signals=raw_signals,
            )

        # Sub-rule: QJ saw target error/failed AND zero F2P → insufficient design depth
        # Agent tried something but fundamentally wrong approach — needs deeper analysis
        if qj_has_target_error:
            return FailureRecord(
                instance_id=instance_id,
                failure_layer="insufficient_design_depth",
                phase_of_failure="ANALYZE",
                signal_quality=SignalQualityRecord(
                    quick_judge_target="true_negative",
                    quick_judge_overall="true_negative",
                    controlled_verify="true_negative",
                ),
                confidence=0.75,
                reasoning=(
                    f"Zero F2P progress ({f2p_total} tests failed). Quick judge confirmed "
                    f"target error — patch direction may be correct but implementation "
                    f"lacks sufficient design depth."
                ),
                recommended_actions=[
                    GateAction(type="increase_analysis_budget",
                               reason="Problem requires deeper mechanism understanding"),
                    GateAction(type="require_design_expansion",
                               reason="Must enumerate internal components and invariants before patching"),
                    GateAction(type="retry_phase", phase="ANALYZE",
                               reason="Return to analysis — current design insufficient"),
                    GateAction(type="enforce_principals",
                               principals=["causal_grounding", "evidence_linkage"],
                               reason="Require evidence-grounded root cause before execution"),
                ],
                signals=raw_signals,
            )

        # Fallback: zero F2P, no QJ signal → wrong direction
        return FailureRecord(
            instance_id=instance_id,
            failure_layer="wrong_direction",
            phase_of_failure="ANALYZE",
            signal_quality=SignalQualityRecord(
                controlled_verify="true_negative",
            ),
            confidence=0.70,
            reasoning=f"Zero F2P progress ({f2p_total} tests failed). Fundamentally wrong approach.",
            recommended_actions=[
                GateAction(type="retry_phase", phase="ANALYZE",
                           reason="Completely re-analyze the problem"),
                GateAction(type="enforce_principals",
                           principals=["causal_grounding"],
                           reason="Must identify actual root cause"),
            ],
            signals=raw_signals,
        )

    # ── Rule 4: verify_gap (F2P all pass, no P2P failure detected, but not resolved) ─
    if failure_type == "verify_gap":
        return FailureRecord(
            instance_id=instance_id,
            failure_layer="near_miss_semantic_insufficiency",
            phase_of_failure="JUDGE",
            signal_quality=SignalQualityRecord(
                quick_judge_target="true_positive" if qj_has_target_passed else "no_signal",
                controlled_verify="true_negative",
            ),
            confidence=0.65,
            reasoning="All F2P pass, no P2P regression detected, but eval says not resolved. Verify gap.",
            recommended_actions=[
                GateAction(type="retry_phase", phase="JUDGE",
                           reason="Verification scope may be insufficient"),
            ],
            signals=raw_signals,
        )

    # ── Fallback ─────────────────────────────────────────────────────────
    return FailureRecord(
        instance_id=instance_id,
        failure_layer="unknown",
        phase_of_failure="ANALYZE",
        signal_quality=SignalQualityRecord(),
        confidence=0.0,
        reasoning="Insufficient signal to classify failure layer",
        recommended_actions=[],
        signals=raw_signals,
    )


# ── Behavioral Failure Mode (full coverage) ──────────────────────────────────

FailureMode = Literal[
    "no_patch",
    "no_test_run",
    "patch_no_progress",
    "environment_failure",
    "unknown_failure",
]


def derive_failure_mode(jingu_body: dict) -> FailureMode:
    """Derive behavioral failure mode from jingu_body signals.

    This runs for ALL attempts (with or without CV) to ensure full coverage.
    Classification is based on observable execution behavior, not semantic
    diagnosis. Priority order avoids ambiguity.

    Returns one of FailureMode values.
    """
    exit_status = jingu_body.get("exit_status", "")
    test_results = jingu_body.get("test_results", {})
    tests_ran = test_results.get("ran_tests", False)
    files_written = jingu_body.get("files_written", [])
    patch_summary = jingu_body.get("patch_summary", {})
    patch_size = (patch_summary.get("lines_added", 0) or 0) + (patch_summary.get("lines_removed", 0) or 0)

    # P1: environment failure — abnormal exit, no patch, no tests
    if exit_status not in ("Submitted", "") and not tests_ran and not files_written:
        return "environment_failure"

    # P2: no patch — agent didn't produce any code change
    if patch_size == 0 or not files_written:
        return "no_patch"

    # P3: patch exists but tests never ran
    if not tests_ran:
        return "no_test_run"

    # P4: patch exists, tests ran — coarse bucket (not semantic diagnosis)
    return "patch_no_progress"


# ── Routing from FailureMode (behavioral fallback) ───────────────────────────

FAILURE_MODE_ROUTING: dict[str, dict] = {
    "no_patch": {
        "next_phase": "ANALYZE",
        "repair_goal": "No code changes were produced. Re-analyze the problem: identify the exact file and function to modify before writing any code.",
        "required_principals": _principals_for_phase("ANALYZE"),
    },
    "no_test_run": {
        "next_phase": "EXECUTE",
        "repair_goal": "A patch was written but tests were never executed. Ensure the patch compiles, run the failing tests, and verify the fix.",
        "required_principals": _principals_for_phase("EXECUTE"),
    },
    "patch_no_progress": {
        "next_phase": "DESIGN",
        "repair_goal": "A patch was written and tests ran but did not pass. The fix direction may be wrong or incomplete. Re-examine which code path the failing tests exercise.",
        "required_principals": _principals_for_phase("DESIGN"),
    },
    "environment_failure": {
        "next_phase": "EXECUTE",
        "repair_goal": "The execution environment failed (abnormal exit, no output). Focus on producing a clean patch and running tests.",
        "required_principals": _principals_for_phase("EXECUTE"),
    },
    "unknown_failure": {
        "next_phase": "ANALYZE",
        "repair_goal": "Insufficient signal to diagnose. Start from scratch: read the failing tests, identify the root cause, design a minimal fix.",
        "required_principals": _principals_for_phase("ANALYZE"),
    },
}


def route_from_failure_mode(failure_mode: FailureMode) -> dict:
    """Get routing for a behavioral failure mode.

    Used as FALLBACK when failure_type (CV-based) is not available.
    Returns dict with next_phase, repair_goal, required_principals.
    """
    return FAILURE_MODE_ROUTING.get(failure_mode, FAILURE_MODE_ROUTING["unknown_failure"])


# ── Routing from FailureRecord ───────────────────────────────────────────────

def route_from_failure(record: FailureRecord) -> dict:
    """Convert a FailureRecord into a control-plane routing decision.

    Returns dict with:
        next_phase: str — which phase to retry from
        instructions: list[str] — specific instructions for the agent
        enforce_principals: list[str] — principals to enforce on retry
        routing: RoutingDecision — typed routing decision (EF-5)
    """
    instructions: list[str] = []
    enforce_principals: list[str] = []
    next_phase = "ANALYZE"  # default (canonical name)

    for action in record.recommended_actions:
        if action.type == "retry_phase" and action.phase:
            next_phase = action.phase  # already canonical from FailureRecord
        elif action.type == "enforce_principals":
            enforce_principals.extend(action.principals)
        elif action.type == "require_design_expansion":
            instructions.append(
                "Expand design: enumerate ALL affected components, files, and invariants "
                "before writing any code."
            )
        elif action.type == "require_affected_surface_enumeration":
            instructions.append(
                "List ALL code locations that need changes. Do not submit until every "
                "required site has been modified."
            )
        elif action.type == "require_regression_sentinel":
            instructions.append(
                "Your previous fix broke existing tests. Ensure your fix preserves all "
                "existing behavior — prefer cloning/isolating over modifying conditions."
            )
        elif action.type == "require_test_resolution_fix":
            instructions.append(
                "The test runner could not resolve the target test. Verify the exact test "
                "class and method names before proceeding."
            )
        elif action.type == "increase_analysis_budget":
            instructions.append(
                "This problem requires deeper analysis. Trace through the full code path, "
                "understand the internal mechanisms, and form a complete design before "
                "attempting any fix."
            )
        elif action.type == "block_submission":
            instructions.append(
                "Submission blocked by control plane. Fix the identified issue first."
            )

    # EF-5: build RoutingDecision
    try:
        from routing_decision import RoutingDecision
        routing = RoutingDecision(
            next_phase=next_phase,
            strategy=record.failure_layer,
            repair_hints=instructions,
            source="failure_layer_route",
        )
    except Exception:
        routing = None

    return {
        "next_phase": next_phase,
        "instructions": instructions,
        "enforce_principals": enforce_principals,
        "failure_layer": record.failure_layer,
        "confidence": record.confidence,
        "routing": routing.to_dict() if routing else None,
    }
