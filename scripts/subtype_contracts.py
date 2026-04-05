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
    phase: str                       # Phase name (ANALYZE, EXECUTE, JUDGE, ...)
    required_principals: list[str]   # Principals the agent MUST declare (hard — gate enforces)
    expected_principals: list[str]   # Principals the agent SHOULD declare (soft — quality signal)
    forbidden_principals: list[str]  # Principals the agent must NOT declare
    repair_target: str               # Phase to redirect to on violation


# Canonical subtype contracts — edit here to update prompt / gate / routing
#
# required_principals: gate-enforced (hard) — missing → REJECT, triggers repair_target routing
# expected_principals: quality signal (soft) — missing → warning/inference diff, no hard reject
SUBTYPE_CONTRACTS: dict[str, SubtypeContract] = {
    "analysis.root_cause": {
        "phase": "ANALYZE",
        "required_principals": ["causal_grounding"],
        "expected_principals": ["evidence_linkage", "alternative_hypothesis_check"],
        "repair_target": "OBSERVE",
    },
    "execution.code_patch": {
        "phase": "EXECUTE",
        "required_principals": ["minimal_change"],
        "expected_principals": ["action_grounding"],
        "repair_target": "ANALYZE",
    },
    "judge.verification": {
        "phase": "JUDGE",
        "required_principals": ["invariant_preservation"],
        "expected_principals": [],
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
    Return required (hard) principals for the given phase.

    Gate-enforced: missing required principal → REJECT + repair_target routing.
    Returns [] if phase has no contract (no enforcement = no crash).

    Args:
        phase: Phase name string (e.g. "ANALYZE", "EXECUTE"). Case-insensitive.
    """
    subtype = _PHASE_TO_SUBTYPE.get(phase.upper(), "")
    contract = SUBTYPE_CONTRACTS.get(subtype, {})
    return list(contract.get("required_principals", []))


def get_expected_principals(phase: str) -> list[str]:
    """
    Return expected (soft) principals for the given phase.

    Quality signal only: missing expected principal → inference diff warning, no hard reject.
    Returns [] if phase has no contract or no expected principals.

    Args:
        phase: Phase name string (e.g. "ANALYZE", "EXECUTE"). Case-insensitive.
    """
    subtype = _PHASE_TO_SUBTYPE.get(phase.upper(), "")
    contract = SUBTYPE_CONTRACTS.get(subtype, {})
    return list(contract.get("expected_principals", []))


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
    Build the canonical principal guidance text for the given phase.

    Generates MUST/SHOULD lines from required_principals and expected_principals.
    Returns "" if phase has no contract (safe — no injection on unknown phase).

    Args:
        phase: Phase name string (e.g. "ANALYZE"). Case-insensitive.
    """
    required = get_required_principals(phase)
    expected = get_expected_principals(phase)
    if not required and not expected:
        return ""
    parts: list[str] = []
    if required:
        parts.append(f"You MUST declare PRINCIPALS: {', '.join(required)}.")
    if expected:
        parts.append(f"You SHOULD also declare: {', '.join(expected)}.")
    return " ".join(parts)
