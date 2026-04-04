"""
control/reasoning_state.py — Python port of jingu-control-plane v0.3 core.

Mirrors:
  src/integrator/signal_integrator.ts   → initial_reasoning_state, update_reasoning_state
  src/controller/phase_controller.ts    → decide_next
  src/adapter/normalize.ts              → normalize_signals, DEFAULT_SIGNALS

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
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

# ── Phase ─────────────────────────────────────────────────────────────────────

Phase = Literal["UNDERSTAND", "OBSERVE", "ANALYZE", "DECIDE", "EXECUTE", "JUDGE"]

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
    )

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
    reason: Literal["task_success", "no_signal"] = "no_signal"

@dataclass(frozen=True)
class VerdictContinue:
    type: str = "CONTINUE"

ControlVerdict = VerdictAdvance | VerdictRedirect | VerdictStop | VerdictContinue

# Phase advance table (mirrors phase_controller.ts)
_ADVANCE_TABLE: dict[Phase, Optional[Phase]] = {
    "UNDERSTAND": "OBSERVE",
    "OBSERVE":    "ANALYZE",
    "ANALYZE":    "DECIDE",
    "DECIDE":     "EXECUTE",
    "EXECUTE":    "JUDGE",
    "JUDGE":      None,  # terminal
}

NO_PROGRESS_THRESHOLD = 2  # consecutive no-signal steps → forced advance or stop

def decide_next(state: ReasoningState) -> ControlVerdict:
    """
    Pure function. Produces a ControlVerdict from current ReasoningState.
    Mirror of decideNext() in controller/phase_controller.ts.

    Priority order (same as TS):
      1. task_success → STOP(task_success) regardless of phase
      2. env_noise    → REDIRECT(ANALYZE)  [unconditional — architectural constraint]
      3. stagnation   → ADVANCE or STOP(no_signal) depending on phase
      4. phase gates  → ADVANCE if phase-specific condition met
      5. default      → CONTINUE
    """
    # 1. terminal success
    if state.task_success:
        return VerdictStop(reason="task_success")

    # 2. env_noise — unconditional redirect (not gated on anything else)
    if state.env_noise:
        return VerdictRedirect(to="ANALYZE", reason="env_noise detected")

    # 3. stagnation
    if state.no_progress_steps >= NO_PROGRESS_THRESHOLD:
        next_phase = _ADVANCE_TABLE.get(state.phase)
        if next_phase is None or state.phase == "JUDGE":
            return VerdictStop(reason="no_signal")
        return VerdictAdvance(to=next_phase)

    # 4. phase gates
    if state.phase == "OBSERVE" and state.hypothesis_narrowing > 0:
        return VerdictAdvance(to="ANALYZE")

    if state.phase == "ANALYZE" and state.actionability > 0:
        return VerdictAdvance(to="EXECUTE")

    # 5. default
    return VerdictContinue()
