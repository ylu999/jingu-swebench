"""Canonical symbols for jingu-swebench.

These values MUST match jingu-protocol/src/symbols/ exactly.
This file is the ONLY place these constants are defined in Python.
All consumers import from here. No aliases, no normalization.

Non-canonical values are bugs, not data to be coerced.
"""
from typing import Literal

# -- Phase --------------------------------------------------------------------
Phase = Literal[
    "UNDERSTAND", "OBSERVE", "ANALYZE", "DECIDE", "DESIGN", "EXECUTE", "JUDGE"
]
ALL_PHASES: tuple[str, ...] = (
    "UNDERSTAND", "OBSERVE", "ANALYZE", "DECIDE", "DESIGN", "EXECUTE", "JUDGE"
)

def assert_phase(value: str) -> str:
    """Validate that value is a canonical Phase. Raises TypeError if not."""
    if value not in ALL_PHASES:
        raise TypeError(f'Invalid Phase: "{value}". Must be one of: {", ".join(ALL_PHASES)}')
    return value

# Phase advance table — the ONE place phase ordering is defined.
# All consumers (reasoning_state, phase_lifecycle, step_sections) derive from here.
PHASE_ADVANCE: dict[str, str | None] = {
    "UNDERSTAND": "OBSERVE",
    "OBSERVE":    "ANALYZE",
    "ANALYZE":    "DECIDE",
    "DECIDE":     "DESIGN",
    "DESIGN":     "EXECUTE",
    "EXECUTE":    "JUDGE",
    "JUDGE":      None,
}

# Phase normalization — agent-declared variants → canonical Phase.
# The ONE place all alias→canonical mappings live.
# Consumers: declaration_extractor, cognition_schema, step_sections.
_PHASE_ALIASES: dict[str, str] = {
    # Gerund/noun forms (LLM output)
    "OBSERVATION": "OBSERVE",
    "ANALYSIS":    "ANALYZE",
    "DECISION":    "DECIDE",
    "EXECUTION":   "EXECUTE",
    "JUDGEMENT":   "JUDGE",
    "JUDGMENT":    "JUDGE",
    # Lowercase forms (cognition_schema legacy)
    "observation": "OBSERVE",
    "analysis":    "ANALYZE",
    "decision":    "DECIDE",
    "execution":   "EXECUTE",
    "design":      "DESIGN",
    "judge":       "JUDGE",
    "observe":     "OBSERVE",
    "analyze":     "ANALYZE",
    "decide":      "DECIDE",
    "execute":     "EXECUTE",
    "validation":  "JUDGE",
    "planning":    "DESIGN",
}

def normalize_phase(value: str) -> str:
    """Normalize agent-declared phase to canonical Phase.

    Returns canonical phase if value is already canonical or is a known alias.
    Raises TypeError if value is not recognized.
    """
    if value in ALL_PHASES:
        return value
    upper = value.upper()
    if upper in ALL_PHASES:
        return upper
    canonical = _PHASE_ALIASES.get(value) or _PHASE_ALIASES.get(upper)
    if canonical:
        return canonical
    raise TypeError(f'Cannot normalize phase: "{value}". Not a known alias.')

def default_next_phase(phase: str) -> str | None:
    """Return the default next phase, or None if terminal. Raises TypeError if invalid."""
    assert_phase(phase)
    return PHASE_ADVANCE[phase]

def is_valid_phase(value: str) -> bool:
    """Check if value is a canonical Phase (no normalization)."""
    return value in ALL_PHASES

# Phase → Subtype mapping (canonical)
PHASE_TO_SUBTYPE: dict[str, str] = {
    "OBSERVE":  "observation.fact_gathering",
    "ANALYZE":  "analysis.root_cause",
    "DECIDE":   "decision.fix_direction",
    "DESIGN":   "design.solution_shape",
    "EXECUTE":  "execution.code_patch",
    "JUDGE":    "judge.verification",
}

# -- Principal ----------------------------------------------------------------
Principal = Literal[
    "ontology_alignment", "phase_boundary_discipline", "evidence_completeness",
    "causal_grounding", "evidence_linkage", "alternative_hypothesis_check",
    "uncertainty_honesty", "option_comparison", "constraint_satisfaction",
    "invariant_preservation", "scope_minimality", "action_grounding",
    "minimal_change", "result_verification", "residual_risk_detection",
]
ALL_PRINCIPALS: tuple[str, ...] = (
    "ontology_alignment", "phase_boundary_discipline", "evidence_completeness",
    "causal_grounding", "evidence_linkage", "alternative_hypothesis_check",
    "uncertainty_honesty", "option_comparison", "constraint_satisfaction",
    "invariant_preservation", "scope_minimality", "action_grounding",
    "minimal_change", "result_verification", "residual_risk_detection",
)

def assert_principal(value: str) -> str:
    """Validate that value is a canonical Principal. Raises TypeError if not."""
    if value not in ALL_PRINCIPALS:
        raise TypeError(f'Invalid Principal: "{value}". Must be one of: {", ".join(ALL_PRINCIPALS)}')
    return value

# -- Subtype ------------------------------------------------------------------
Subtype = Literal[
    "observation.fact_gathering", "analysis.root_cause", "decision.fix_direction",
    "design.solution_shape", "execution.code_patch", "judge.verification",
]
ALL_SUBTYPES: tuple[str, ...] = (
    "observation.fact_gathering", "analysis.root_cause", "decision.fix_direction",
    "design.solution_shape", "execution.code_patch", "judge.verification",
)

def assert_subtype(value: str) -> str:
    """Validate that value is a canonical Subtype. Raises TypeError if not."""
    if value not in ALL_SUBTYPES:
        raise TypeError(f'Invalid Subtype: "{value}". Must be one of: {", ".join(ALL_SUBTYPES)}')
    return value

# -- Verdict ------------------------------------------------------------------
Verdict = Literal["ADMITTED", "RETRYABLE", "REJECTED"]
ALL_VERDICTS: tuple[str, ...] = ("ADMITTED", "RETRYABLE", "REJECTED")

def assert_verdict(value: str) -> str:
    """Validate that value is a canonical Verdict. Raises TypeError if not."""
    if value not in ALL_VERDICTS:
        raise TypeError(f'Invalid Verdict: "{value}". Must be one of: {", ".join(ALL_VERDICTS)}')
    return value
