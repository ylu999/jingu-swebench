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
