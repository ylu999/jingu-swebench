"""
principal_gate.py — Phase-specific principal enforcement.

Each phase has a minimum set of required principals.
If the agent's PhaseRecord doesn't declare them, emit a violation.

Violation is a redirect hint injection, not a fatal error.
The main flow is always wrapped in try/except to ensure robustness.
"""

from __future__ import annotations

# Minimum required principals per phase
PHASE_REQUIRED_PRINCIPALS: dict[str, list[str]] = {
    "OBSERVE":  [],                      # no enforcement in observation phase
    "ANALYZE":  ["causal_grounding"],    # must declare causal analysis
    "EXECUTE":  ["minimal_change"],      # must declare scope discipline
    "JUDGE":    ["invariant_preservation"],  # must declare invariant checked
}

# Redirect target when violation detected
PHASE_VIOLATION_REDIRECT: dict[str, str] = {
    "ANALYZE":  "OBSERVE",   # missing causal_grounding -> back to observation
    "EXECUTE":  "ANALYZE",   # missing minimal_change -> back to analysis
    "JUDGE":    "EXECUTE",   # missing invariant_preservation -> back to execution
}

# Human-readable feedback for each violation
_FEEDBACK: dict[str, str] = {
    "missing_causal_grounding": (
        "Your analysis must identify a root cause with causal evidence. "
        "Declare PRINCIPALS: causal_grounding"
    ),
    "missing_minimal_change": (
        "Your patch must be scoped to the minimum change. "
        "Declare PRINCIPALS: minimal_change"
    ),
    "missing_invariant_preservation": (
        "Your judge output must verify an invariant was preserved. "
        "Declare PRINCIPALS: invariant_preservation"
    ),
}


def check_principal_gate(phase_record, phase: str) -> str | None:
    """
    Check if the PhaseRecord satisfies required principals for the given phase.

    Returns violation string (e.g. 'missing_causal_grounding') if violated,
    None if OK or no enforcement for this phase.

    Args:
        phase_record: PhaseRecord or any object with a .principals list attribute
        phase: Phase name string (e.g. 'ANALYZE', 'EXECUTE', 'JUDGE')
    """
    required = PHASE_REQUIRED_PRINCIPALS.get(phase.upper(), [])
    if not required:
        return None

    declared = [p.lower() for p in (getattr(phase_record, "principals", None) or [])]
    for req in required:
        if req not in declared:
            return f"missing_{req}"
    return None


def get_principal_feedback(violation: str) -> str:
    """Return human-readable feedback for a principal violation."""
    return _FEEDBACK.get(
        violation,
        f"Principal violation: {violation}. Declare required principals for this phase.",
    )
