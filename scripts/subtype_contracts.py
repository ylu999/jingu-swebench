"""
subtype_contracts.py — canonical source for phase/subtype principal contracts (p193)

SUBTYPE_CONTRACTS is the single source of truth for:
  - required_principals per phase (consumed by principal_gate.py)
  - phase prompt guidance (consumed by phase_prompt.py)
  - repair_target routing (consumed by run_with_jingu_gate.py)

Adding a new subtype: edit SUBTYPE_CONTRACTS here — prompt / gate / routing auto-update.
"""

from __future__ import annotations

from typing import TypedDict


class SubtypeContract(TypedDict, total=False):
    """Contract definition for a phase subtype."""
    phase: str                      # Phase name (ANALYZE, EXECUTE, JUDGE, ...)
    required_principals: list[str]  # Principals the agent MUST declare
    forbidden_principals: list[str] # Principals the agent must NOT declare
    guidance: str                   # Prompt guidance text for this phase
    repair_target: str              # Phase to redirect to on violation


# Canonical subtype contracts — edit here to update prompt / gate / routing
SUBTYPE_CONTRACTS: dict[str, SubtypeContract] = {
    "analysis.root_cause": {
        "phase": "ANALYZE",
        # Gate enforces causal_grounding (minimum required by principal_gate).
        # evidence_linkage is recommended but not gate-enforced in this phase.
        "required_principals": ["causal_grounding"],
        "guidance": (
            "Form hypotheses. Identify root cause with causal evidence. Do NOT fix yet. "
            "You MUST declare PRINCIPALS: causal_grounding. "
            "Also recommended: evidence_linkage. "
            "Output: your diagnosis, the causal chain, and why."
        ),
        "repair_target": "OBSERVE",
    },
    "execution.code_patch": {
        "phase": "EXECUTE",
        # Gate enforces minimal_change (minimum required by principal_gate).
        # action_grounding is recommended but not gate-enforced in this phase.
        "required_principals": ["minimal_change"],
        "guidance": (
            "Write the minimal fix targeting the root cause identified in ANALYZE/DECIDE. "
            "You MUST declare PRINCIPALS: minimal_change. "
            "Also recommended: action_grounding. "
            "Output: the patch and why it addresses the root cause."
        ),
        "repair_target": "ANALYZE",
    },
    "judge.verification": {
        "phase": "JUDGE",
        "required_principals": ["invariant_preservation"],
        "guidance": (
            "Verify your fix. Run tests. Check invariants. "
            "You MUST declare PRINCIPALS: invariant_preservation. "
            "Output: FIX_TYPE declaration + evidence that tests pass."
        ),
        "repair_target": "EXECUTE",
    },
}

# Phase → subtype mapping (first matching subtype wins for each phase)
_PHASE_TO_SUBTYPE: dict[str, str] = {
    c["phase"]: subtype
    for subtype, c in SUBTYPE_CONTRACTS.items()
    if "phase" in c
}


def get_required_principals(phase: str) -> list[str]:
    """
    Return required principals for the given phase.

    Looks up the canonical subtype contract for the phase.
    Returns [] if phase has no contract (no enforcement = no crash).

    Args:
        phase: Phase name string (e.g. "ANALYZE", "EXECUTE"). Case-insensitive.
    """
    subtype = _PHASE_TO_SUBTYPE.get(phase.upper(), "")
    contract = SUBTYPE_CONTRACTS.get(subtype, {})
    return list(contract.get("required_principals", []))


def get_repair_target(phase: str) -> str:
    """
    Return the repair target phase for a violation in the given phase.

    Returns "" if no repair target is defined.

    Args:
        phase: Phase name string (e.g. "ANALYZE"). Case-insensitive.
    """
    subtype = _PHASE_TO_SUBTYPE.get(phase.upper(), "")
    contract = SUBTYPE_CONTRACTS.get(subtype, {})
    return contract.get("repair_target", "")


def build_phase_principal_guidance(phase: str) -> str:
    """
    Return the canonical principal guidance text for the given phase.

    Returns "" if no guidance is defined (safe — no injection on unknown phase).

    Args:
        phase: Phase name string (e.g. "ANALYZE"). Case-insensitive.
    """
    subtype = _PHASE_TO_SUBTYPE.get(phase.upper(), "")
    contract = SUBTYPE_CONTRACTS.get(subtype, {})
    return contract.get("guidance", "")
