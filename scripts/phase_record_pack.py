"""
Phase Record GovernancePack — detects stall/gap patterns from phase_records.

Detects 3 patterns:
  1. Execution stall:  EXECUTE phase entered but files_written==0
  2. Analysis gap:     Only ANALYZE phases, no ROOT_CAUSE produced
  3. OBSERVE loop:     3+ consecutive OBSERVE phases without advancing

Architecture (p207 P14):
  - Reads phase_records from jingu_body (populated before governance packs run)
  - Returns REROUTE with targeted hint for each pattern
  - Patterns are mutually exclusive; first match wins (execution_stall > analysis_gap > observe_loop)
"""
from __future__ import annotations

from governance_pack import (
    ExecutionContext,
    FailureSignal,
    GovernancePack,
    RecognitionResult,
    RouteDecision,
)


# ── Step 3: parse_failure ──────────────────────────────────────────────────────

def _parse_failure(ctx: ExecutionContext) -> FailureSignal | None:
    """
    Extract phase-record-based failure signal from execution context.

    Reads jingu_body["phase_records"] (list of dicts) and detects
    structural patterns that indicate agent stall.
    Returns None if no stall pattern detected.
    """
    phase_records = ctx.jingu_body.get("phase_records", [])
    if not phase_records:
        return None

    files_written = ctx.jingu_body.get("files_written", [])

    # Pattern 1: Execution stall — EXECUTE phase entered but no files written
    has_execute = any(r.get("phase") == "EXECUTE" for r in phase_records)
    if has_execute and len(files_written) == 0:
        return FailureSignal(
            failure_type="EXECUTION_STALL",
            controlled_passed=0,
            controlled_failed=0,
            failing_tests=[],
            raw_excerpt="EXECUTE phase present but files_written==0",
        )

    # Pattern 2: Analysis gap — only ANALYZE phases, none with root_cause
    analyze_records = [r for r in phase_records if r.get("phase") == "ANALYZE"]
    if analyze_records and not any(r.get("phase") == "EXECUTE" for r in phase_records):
        has_root_cause = any(r.get("root_cause") for r in analyze_records)
        if not has_root_cause:
            return FailureSignal(
                failure_type="ANALYSIS_GAP",
                controlled_passed=0,
                controlled_failed=0,
                failing_tests=[],
                raw_excerpt=f"{len(analyze_records)} ANALYZE records, none with root_cause",
            )

    # Pattern 3: OBSERVE loop — 3+ consecutive OBSERVE phases at the tail
    phases = [r.get("phase") for r in phase_records]
    consecutive_observe = 0
    for p in reversed(phases):
        if p == "OBSERVE":
            consecutive_observe += 1
        else:
            break
    if consecutive_observe >= 3:
        return FailureSignal(
            failure_type="OBSERVE_LOOP",
            controlled_passed=0,
            controlled_failed=0,
            failing_tests=[],
            raw_excerpt=f"{consecutive_observe} consecutive OBSERVE phases at tail",
        )

    return None


# ── Step 4: recognize ─────────────────────────────────────────────────────────

def _recognize(signal: FailureSignal) -> RecognitionResult | None:
    """
    Map phase-record FailureSignal to behavioral state and next phase.
    """
    if signal.failure_type == "EXECUTION_STALL":
        return RecognitionResult(
            state="execution_stall",
            confidence=0.9,
            next_phase="ANALYSIS",
            reason="Agent entered EXECUTE but wrote no code. Needs concrete plan.",
        )

    if signal.failure_type == "ANALYSIS_GAP":
        return RecognitionResult(
            state="analysis_gap",
            confidence=0.85,
            next_phase="ANALYSIS",
            reason="Multiple analysis steps without ROOT_CAUSE. Needs focused hypothesis.",
        )

    if signal.failure_type == "OBSERVE_LOOP":
        return RecognitionResult(
            state="observe_loop",
            confidence=0.9,
            next_phase="ANALYSIS",
            reason="3+ consecutive OBSERVE phases without advancing. Stuck in observation.",
        )

    return None


# ── Step 5: route ──────────────────────────────────────────────────────────────

def _route(recog: RecognitionResult, ctx: ExecutionContext) -> RouteDecision | None:
    """Route recognition result to control decision with targeted hint."""

    if recog.state == "execution_stall":
        return RouteDecision(
            action="REROUTE",
            target_phase="ANALYSIS",
            hint=(
                f"[JINGU ROUTING] EXECUTION_STALL (attempt {ctx.attempt}): "
                f"You entered EXECUTE but wrote no code. "
                f"Return to ANALYZE and form a concrete plan before executing. "
                f"Identify the exact file and function to modify, then make the change."
            ),
        )

    if recog.state == "analysis_gap":
        return RouteDecision(
            action="REROUTE",
            target_phase="ANALYSIS",
            hint=(
                f"[JINGU ROUTING] ANALYSIS_GAP (attempt {ctx.attempt}): "
                f"Multiple analysis steps without ROOT_CAUSE. "
                f"Focus on one specific hypothesis and trace it to a file:line location. "
                f"State your root cause explicitly before proceeding."
            ),
        )

    if recog.state == "observe_loop":
        return RouteDecision(
            action="REROUTE",
            target_phase="ANALYSIS",
            hint=(
                f"[JINGU ROUTING] OBSERVE_LOOP (attempt {ctx.attempt}): "
                f"You are stuck in observation. "
                f"State your current hypothesis and move to ANALYZE. "
                f"Pick one theory, find the code, and test it."
            ),
        )

    return RouteDecision(action="CONTINUE")


# ── Pack definition ────────────────────────────────────────────────────────────

PHASE_RECORD_PACK = GovernancePack(
    name="phase_record_stall_detector_v0",

    # Step 1: response/state fields this pack requires
    required_state_fields=[
        "phase_records",
        "files_written",
    ],

    # Step 2: prompt extension (injected when pack is installed)
    prompt_extensions=[
        (
            "The system monitors your reasoning phases. "
            "If you enter EXECUTE without writing code, repeat OBSERVE without advancing, "
            "or analyze without producing a ROOT_CAUSE, you will be redirected."
        )
    ],

    # Steps 3-5: functional chain
    parse_failure=_parse_failure,
    recognize=_recognize,
    route=_route,
)
