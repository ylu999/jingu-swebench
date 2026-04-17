"""
phase_lifecycle.py — Phase Lifecycle: typed phase completion + protocol-driven routing.

L4 of the protocol enforcement stack:
  L1: compile-time (protocol_compiler.py)
  L2: admission gate (admit_phase_record in step_sections.py)
  L3: prompt + read path + cross-attempt routing
  L4: phase lifecycle — system can ONLY advance via admitted protocol record

Core types:
  PhaseResult   — typed result of a phase execution (admitted record + routing decision)
  RoutingDecision — where to go next, derived ONLY from protocol fields

Core function:
  route_from_phase_result() — derives next phase from PhaseResult using ONLY protocol fields.
  No heuristics, no step counts, no text pattern matching.

ANALYZE-only (Step 1): only ANALYZE has protocol-driven routing.
Other phases fall through to existing decide_next() heuristic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# Phase advance table — canonical (mirrors reasoning_state.py _ADVANCE_TABLE)
_PHASE_ORDER = ["UNDERSTAND", "OBSERVE", "ANALYZE", "DECIDE", "DESIGN", "EXECUTE", "JUDGE"]

_DEFAULT_ADVANCE: dict[str, str | None] = {
    "UNDERSTAND": "OBSERVE",
    "OBSERVE": "ANALYZE",
    "ANALYZE": "DECIDE",
    "DECIDE": "DESIGN",
    "DESIGN": "EXECUTE",
    "EXECUTE": "JUDGE",
    "JUDGE": None,
}


@dataclass
class RoutingDecision:
    """Where the system should go after a phase completes.

    Derived ONLY from protocol fields in the admitted record.
    No heuristics. No step counts. No text matching.
    """
    next_phase: str | None  # None = terminal (JUDGE complete)
    reason: str = ""
    source: Literal["protocol", "default_advance", "incomplete_record"] = "default_advance"

    # If True, the phase did NOT complete — agent must retry current phase
    retry_current: bool = False
    retry_hint: str = ""


@dataclass
class PhaseResult:
    """Typed result of a phase execution.

    Created when admit_phase_record() succeeds for a phase.
    Contains the admitted record and the protocol-derived routing decision.
    """
    phase: str
    completed: bool = False

    # The admitted record (dict from tool submission)
    admitted_record: dict[str, Any] | None = None

    # Protocol-derived routing (set by route_from_phase_result)
    routing: RoutingDecision | None = None

    # Telemetry
    admission_source: str = ""
    protocol_fields_present: list[str] = field(default_factory=list)
    protocol_fields_missing: list[str] = field(default_factory=list)


def route_from_phase_result(result: PhaseResult) -> RoutingDecision:
    """Derive routing decision from PhaseResult using ONLY protocol fields.

    ANALYZE-specific routing (Step 1):
    - If repair_strategy_type is present → route based on strategy
    - If record is complete (all protocol fields present) → default advance to DECIDE
    - If record is incomplete → retry ANALYZE with specific hints

    Other phases: default advance (fallback to existing heuristic in caller).

    Returns RoutingDecision. Never raises.
    """
    phase = result.phase.upper()

    if not result.completed or result.admitted_record is None:
        return RoutingDecision(
            next_phase=phase,
            reason="phase not completed — no admitted record",
            source="incomplete_record",
            retry_current=True,
            retry_hint=f"You must submit a complete {phase} record to proceed.",
        )

    # ── ANALYZE: protocol-driven routing ──────────────────────────────
    if phase == "ANALYZE":
        return _route_analyze(result)

    # ── Other phases: default advance (caller can override with heuristic) ──
    next_phase = _DEFAULT_ADVANCE.get(phase)
    return RoutingDecision(
        next_phase=next_phase,
        reason=f"default advance from {phase}",
        source="default_advance",
    )


def _route_analyze(result: PhaseResult) -> RoutingDecision:
    """ANALYZE-specific protocol-driven routing.

    Uses repair_strategy_type (control field) to determine next phase:
    - Any valid strategy → advance to DECIDE (normal flow)
    - Missing strategy → retry ANALYZE (protocol violation already caught by L2,
      but this is the L4 safety net)

    Future extensions:
    - repair_strategy_type == "reframe" → route back to OBSERVE
    - repair_strategy_type == "escalate" → route to JUDGE
    - invariant_capture.confidence < threshold → route to more observation
    """
    record = result.admitted_record or {}

    # Control field: repair_strategy_type
    strategy = record.get("repair_strategy_type", "")
    if not strategy or not isinstance(strategy, str) or not strategy.strip():
        # Should not reach here if L2 protocol validation is working
        return RoutingDecision(
            next_phase="ANALYZE",
            reason="repair_strategy_type missing — protocol safety net",
            source="protocol",
            retry_current=True,
            retry_hint=(
                "Your ANALYZE record was admitted but repair_strategy_type is missing. "
                "This is a protocol violation. Resubmit with a valid repair_strategy_type."
            ),
        )

    strategy_normalized = strategy.strip().upper()

    # Validate against known strategies
    try:
        from cognition_contracts.analysis_root_cause import REPAIR_STRATEGY_TYPES
        valid_strategies = {s.upper() for s in REPAIR_STRATEGY_TYPES}
    except ImportError:
        valid_strategies = {
            "REGEX_FIX", "PARSER_REWRITE", "DATAFLOW_FIX", "STATE_COPY_FIX",
            "INVARIANT_FIX", "MISSING_SECONDARY_FIX", "API_CONTRACT_FIX",
        }

    if strategy_normalized not in valid_strategies:
        return RoutingDecision(
            next_phase="ANALYZE",
            reason=f"repair_strategy_type '{strategy_normalized}' not in valid set",
            source="protocol",
            retry_current=True,
            retry_hint=(
                f"repair_strategy_type '{strategy_normalized}' is not valid. "
                f"Choose one of: {', '.join(sorted(valid_strategies))}"
            ),
        )

    # Protocol-driven advance: ANALYZE → DECIDE
    # The strategy type is valid — advance to DECIDE for fix direction selection
    return RoutingDecision(
        next_phase="DECIDE",
        reason=f"ANALYZE complete with strategy={strategy_normalized}",
        source="protocol",
    )


def build_phase_result_from_admission(
    phase: str,
    admitted_record: dict[str, Any] | None,
    admission_source: str = "",
) -> PhaseResult:
    """Build a PhaseResult from an admitted record.

    Called after admit_phase_record() succeeds.
    Checks protocol field completeness and routes.
    """
    result = PhaseResult(
        phase=phase.upper(),
        completed=admitted_record is not None,
        admitted_record=admitted_record,
        admission_source=admission_source,
    )

    # Check protocol field completeness
    if admitted_record is not None:
        try:
            from protocol_compiler import _get_protocol_specs
            specs = _get_protocol_specs()
            phase_specs = [s for s in specs if s.phase == phase.upper()]
            for spec in phase_specs:
                val = admitted_record.get(spec.name)
                if val is not None and (not isinstance(val, str) or val.strip()):
                    result.protocol_fields_present.append(spec.name)
                elif spec.protocol_required:
                    result.protocol_fields_missing.append(spec.name)
        except ImportError:
            pass

    # Derive routing
    result.routing = route_from_phase_result(result)

    return result
