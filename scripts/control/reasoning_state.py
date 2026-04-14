"""
control/reasoning_state.py — Python port of jingu-trust-gate control plane v0.3 core.

Mirrors (now in jingu-trust-gate/src/control/ — absorbed from jingu-control-plane p227-08):
  src/control/integrator/signal-integrator.ts   → initial_reasoning_state, update_reasoning_state
  src/control/controller/phase-controller.ts    → decide_next
  src/control/adapter/normalize.ts              → normalize_signals, DEFAULT_SIGNALS

Interface kept 1:1 with the TypeScript API (same field names, same defaults, same logic).

Design constraints (from architectural review):
  - I1: progress (evidence_gain>0 OR hypothesis_narrowing>0) resets no_progress_steps to 0
  - I2: no-progress increments no_progress_steps
  - I3: observation fields overwrite from signals (no accumulation)
  - I4: step_index is monotone increment
  - I5: phase is NEVER touched by update_reasoning_state (runtime-owned)
  - C5: files_written alone does NOT count as progress (only test count change or verify signal)
  - REDIRECT is an unconditional override (not gated on existing control_action)
  - actionability = pre-execution readiness (patch non-empty), NOT post-verify success
  - verify-level signals (task_success) must be applied in a SEPARATE update call

v0.4 — cognition-aware control (principal_violation):
  - principal_violation: str field on ReasoningState — set by phase boundary check
  - decide_next priority 2.5: principal_violation → REDIRECT(repair_target) before stagnation
  - Violation is cleared (reset to "") at every step via patch_principal_violation()
  - Separation: step-level signals (update_reasoning_state) never set principal_violation;
    only phase-boundary principal check sets it (set_principal_violation()).
    This preserves I3/I5 and the pure-function contract of update_reasoning_state.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

# ── Phase ─────────────────────────────────────────────────────────────────────

Phase = Literal["UNDERSTAND", "OBSERVE", "ANALYZE", "DECIDE", "DESIGN", "EXECUTE", "JUDGE"]

# ── Signals ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CognitionSignals:
    """
    7 domain-independent signals. Conservative defaults: assume no progress, fully uncertain.
    These are the same field names as TypeScript CognitionSignals.
    """
    evidence_gain:        int   = 0      # new information found (not just files written)
    hypothesis_narrowing: int   = 0      # solution space narrowed
    uncertainty:          float = 1.0   # 0=certain, 1=fully uncertain
    actionability:        int   = 0      # pre-execution readiness (patch non-empty)
    env_noise:            bool  = False  # environment issue detected
    pattern_matched:      bool  = False  # known pattern recognized
    task_success:         bool  = False  # terminal: verify passed

# Default signals — conservative baseline (same as DEFAULT_SIGNALS in normalize.ts)
DEFAULT_SIGNALS = CognitionSignals()

def normalize_signals(partial: dict) -> CognitionSignals:
    """
    Shallow merge partial dict with conservative defaults.
    Mirror of normalizeSignals() in adapter/normalize.ts.
    Absent keys get defaults — never raises.
    """
    return CognitionSignals(
        evidence_gain        = partial.get("evidence_gain",        DEFAULT_SIGNALS.evidence_gain),
        hypothesis_narrowing = partial.get("hypothesis_narrowing", DEFAULT_SIGNALS.hypothesis_narrowing),
        uncertainty          = partial.get("uncertainty",          DEFAULT_SIGNALS.uncertainty),
        actionability        = partial.get("actionability",        DEFAULT_SIGNALS.actionability),
        env_noise            = partial.get("env_noise",            DEFAULT_SIGNALS.env_noise),
        pattern_matched      = partial.get("pattern_matched",      DEFAULT_SIGNALS.pattern_matched),
        task_success         = partial.get("task_success",         DEFAULT_SIGNALS.task_success),
    )

# ── ReasoningState ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ReasoningState:
    """
    Accumulated reasoning state across steps in one attempt.
    phase is runtime-owned — NEVER modified by update_reasoning_state (I5).

    principal_violation: set at phase boundaries by set_principal_violation().
    NOT set by update_reasoning_state (step-level pure function contract preserved).
    Cleared to "" at every step start via patch_principal_violation("").
    """
    phase:                Phase
    step_index:           int   = 0
    no_progress_steps:    int   = 0
    # Observation fields — overwritten each step (I3)
    evidence_gain:        int   = 0
    hypothesis_narrowing: int   = 0
    uncertainty:          float = 1.0
    actionability:        int   = 0
    env_noise:            bool  = False
    pattern_matched:      bool  = False
    task_success:         bool  = False
    # Cognition correctness gate — set at phase boundary, cleared each step
    principal_violation:  str   = ""   # e.g. "missing_causal_grounding"; "" = clean
    # Per-phase step counter — incremented every step, reset on phase change
    phase_steps:          int   = 0

def initial_reasoning_state(phase: Phase) -> ReasoningState:
    """Create a fresh state at attempt start. Mirror of initialReasoningState()."""
    return ReasoningState(phase=phase)

def update_reasoning_state(
    prev: ReasoningState,
    signals: CognitionSignals,
    update_stagnation: bool = True,
) -> ReasoningState:
    """
    Pure function. Apply one step's signals to produce next state.

    I1: evidence_gain>0 OR hypothesis_narrowing>0 → no_progress_steps = 0
    I2: neither → no_progress_steps += 1
    I3: observation fields overwrite from signals (no accumulation)
    I4: step_index = prev.step_index + 1 (monotone)
    I5: phase = prev.phase (never touched here)

    C5: evidence_gain is set by the ADAPTER, not here.
        Files written alone do NOT reset stagnation — adapter must decide
        whether writes constituted real evidence (e.g., test count changed).

    B3.2 — update_stagnation:
        True  (default): verify-window calls — I1/I2 apply, no_progress_steps advances.
        False           : step-level calls   — no_progress_steps frozen at prev value.
        This separates "stagnation granularity" from "step granularity":
        stagnation is a verify-window concept (how many verify windows had no progress),
        not a step concept (how many individual steps had no test count change).
    """
    if update_stagnation:
        has_progress = signals.evidence_gain > 0 or signals.hypothesis_narrowing > 0
        no_progress_steps = 0 if has_progress else (prev.no_progress_steps + 1)
    else:
        no_progress_steps = prev.no_progress_steps  # frozen — step-level doesn't gate stagnation

    return ReasoningState(
        phase                = prev.phase,          # I5
        step_index           = prev.step_index + 1, # I4
        no_progress_steps    = no_progress_steps,
        # I3: overwrite from signals
        evidence_gain        = signals.evidence_gain,
        hypothesis_narrowing = signals.hypothesis_narrowing,
        uncertainty          = signals.uncertainty,
        actionability        = signals.actionability,
        env_noise            = signals.env_noise,
        pattern_matched      = signals.pattern_matched,
        task_success         = signals.task_success,
        # principal_violation cleared each step (phase-boundary field, not a signal)
        principal_violation  = "",
        # phase_steps incremented every step (reset externally on phase change)
        phase_steps          = prev.phase_steps + 1,
    )


def set_principal_violation(state: ReasoningState, violation: str) -> ReasoningState:
    """
    Set principal_violation on an existing ReasoningState (phase-boundary operation).

    Called AFTER update_reasoning_state, at VerdictAdvance time, when principal_gate
    detects a violation. Returns a new frozen state with only principal_violation changed.

    Design: separate from update_reasoning_state to preserve the pure-function contract
    (update_reasoning_state never sets principal_violation; principal_gate never touches
    step-level signals). The caller decides when to call decide_next again.
    """
    import dataclasses as _dc
    return _dc.replace(state, principal_violation=violation)


def reset_phase_steps(state: ReasoningState) -> ReasoningState:
    """Reset phase_steps to 0 when phase changes externally.

    Called by the runtime after advancing cp_state.phase to a new phase.
    Preserves I5 contract: update_reasoning_state never touches phase;
    phase change (and phase_steps reset) is runtime-owned.
    """
    import dataclasses as _dc
    return _dc.replace(state, phase_steps=0)

# ── ControlVerdict ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class VerdictAdvance:
    type: str = "ADVANCE"
    to: Optional[Phase] = None

@dataclass(frozen=True)
class VerdictRedirect:
    type: str = "REDIRECT"
    to: Phase = "ANALYZE"
    reason: str = ""

@dataclass(frozen=True)
class VerdictStop:
    type: str = "STOP"
    reason: Literal["task_success", "no_signal", "empty_patch", "phase_budget_exhausted"] = "no_signal"

@dataclass(frozen=True)
class VerdictContinue:
    type: str = "CONTINUE"

ControlVerdict = VerdictAdvance | VerdictRedirect | VerdictStop | VerdictContinue

# Phase advance table (mirrors phase_controller.ts)
_ADVANCE_TABLE: dict[Phase, Optional[Phase]] = {
    "UNDERSTAND": "OBSERVE",
    "OBSERVE":    "ANALYZE",
    "ANALYZE":    "DECIDE",
    "DECIDE":     "DESIGN",
    "DESIGN":     "EXECUTE",
    "EXECUTE":    "JUDGE",
    "JUDGE":      None,  # terminal
}

NO_PROGRESS_THRESHOLD = 4  # consecutive no-signal steps → forced advance or stop

# Per-phase step budget — hard limit on steps in any single phase.
# When phase_steps >= budget, decide_next() forces VerdictAdvance.
# Rationale: prevents unbounded exploration (e.g. 40 steps in OBSERVE).
PHASE_STEP_BUDGET: dict[str, int] = {
    "UNDERSTAND": 5,
    "OBSERVE":    10,
    "ANALYZE":    10,
    "DECIDE":     5,
    "DESIGN":     10,
    "EXECUTE":    25,
    "JUDGE":      5,
}

def decide_next(state: ReasoningState) -> ControlVerdict:
    """
    Pure function. Produces a ControlVerdict from current ReasoningState.
    Mirror of decideNext() in controller/phase_controller.ts.

    Priority order:
      1.   task_success         → STOP(task_success) regardless of phase
      2.   env_noise            → REDIRECT(ANALYZE)  [unconditional — architectural constraint]
      2.5  principal_violation  → REDIRECT(repair_target) [cognition correctness gate]
      3.   stagnation           → ADVANCE or STOP(no_signal) depending on phase
      4.   phase gates          → ADVANCE if phase-specific condition met
      5.   default              → CONTINUE

    principal_violation (2.5): fires when phase-boundary principal check fails.
    Redirects to the repair target for the violated phase (from PHASE_VIOLATION_REDIRECT).
    Lower priority than env_noise (env issues dominate over cognition quality).
    Higher priority than stagnation (cognition violation is a harder signal).
    """
    # 1. terminal success
    if state.task_success:
        return VerdictStop(reason="task_success")

    # 2. env_noise — unconditional redirect (not gated on anything else)
    if state.env_noise:
        return VerdictRedirect(to="ANALYZE", reason="env_noise detected")

    # 2.5. principal_violation — cognition correctness gate
    if state.principal_violation:
        # EF-5: Use subtype_contracts repair_target instead of deleted PHASE_VIOLATION_REDIRECT.
        # Fallback: ANALYZE (always valid repair target).
        _repair = "ANALYZE"
        try:
            from subtype_contracts import get_repair_target as _get_rt
            _rt = _get_rt(state.phase)
            if _rt:
                _repair = _rt
        except Exception:
            pass
        return VerdictRedirect(
            to=_repair,  # type: ignore[arg-type]
            reason=f"principal_violation:{state.principal_violation}",
        )

    # 2.75. phase budget exceeded — hard step limit per phase
    _budget = PHASE_STEP_BUDGET.get(state.phase, 15)
    if state.phase_steps >= _budget:
        next_phase = _ADVANCE_TABLE.get(state.phase)
        if next_phase is None:
            return VerdictStop(reason="phase_budget_exhausted")
        return VerdictAdvance(to=next_phase)

    # 3. stagnation
    if state.no_progress_steps >= NO_PROGRESS_THRESHOLD:
        # 改动5: EXECUTE no_progress → DECIDE (not JUDGE).
        # EXECUTE with no new patch means the agent doesn't know what to write;
        # advancing to JUDGE would verify nothing. Route back to DECIDE to re-plan.
        if state.phase == "EXECUTE":
            # Bug G fix (p22): only redirect if no patch exists.
            # If actionability > 0 (patch written), agent is actively trying to fix;
            # no-progress just means tests haven't passed yet. Redirecting to DECIDE
            # in that case is counterproductive — let inner_verify + max_attempts handle it.
            # execute_no_progress redirect is only meaningful when patch is absent
            # (agent is stuck in EXECUTE without producing any code).
            if state.actionability > 0:
                return VerdictContinue()
            return VerdictRedirect(to="DECIDE", reason="execute_no_progress")
        next_phase = _ADVANCE_TABLE.get(state.phase)
        if next_phase is None or state.phase == "JUDGE":
            return VerdictStop(reason="no_signal")
        return VerdictAdvance(to=next_phase)

    # 4. phase gates
    if state.phase == "OBSERVE" and state.hypothesis_narrowing > 0:
        return VerdictAdvance(to="ANALYZE")

    # P2: ANALYZE → DECIDE (not EXECUTE). DECIDE phase is mandatory —
    # agent must declare expected_outcome + testable_hypothesis before executing.
    # DECIDE → DESIGN → EXECUTE transition handled by stagnation path (_ADVANCE_TABLE).
    if state.phase == "ANALYZE" and state.actionability > 0:
        return VerdictAdvance(to="DECIDE")

    # 5. default
    return VerdictContinue()
