"""
subtype_contracts.py — Python adapter for jingu-cognition v2.0 contracts (p193+)

This file is a CONSUMER/ADAPTER of the canonical ontology defined in:
  jingu-cognition/src/mappings.ts  (TypeScript source of truth)

It must NOT invent new principal names, subtype names, or phase names.
All taxonomy derives from jingu-cognition. Changes to contracts must
originate there and be reflected here.

v0.5 — aligned to jingu-cognition v2.0 COGNITION_CONTRACTS:
  - required_principals: now includes cross-phase ontology_alignment +
    phase_boundary_discipline on every subtype
  - forbidden_principals: per-subtype (observation/analysis forbid action_grounding)
  - DESIGN phase added: design.solution_shape
  - analysis: added evidence_linkage to required (was expected)
  - judge: replaced invariant_preservation with result_verification + uncertainty_honesty
  - decision: added option_comparison + constraint_satisfaction to required

Admission taxonomy:
  RETRYABLE — missing_required_principal, missing_required_field
              (right phase, incomplete material — redirect to repair)
  REJECTED  — forbidden_transition, forbidden_principal_declared
              (wrong phase position or boundary violation — stop, do not redirect)
"""

from __future__ import annotations

from typing import TypedDict

from cognition_contracts import analysis_root_cause as _arc


class SubtypeContract(TypedDict, total=False):
    """Contract definition for a phase subtype. Mirrors CognitionContract in jingu-cognition."""
    phase: str                       # Phase name (ANALYZE, EXECUTE, JUDGE, ...)
    required_principals: list[str]   # Principals the agent MUST declare (hard — gate enforces)
    expected_principals: list[str]   # Principals the agent SHOULD declare (soft — quality signal)
    forbidden_principals: list[str]  # Principals the agent must NOT declare (hard — fake principal)
    repair_target: str               # Phase to redirect to on RETRYABLE violation
    required_fields: list[str]       # PhaseRecord attributes that must be non-empty (RETRYABLE)
    allowed_next: list[str]          # Legal next phases; violation → REJECTED (boundary error)


# Subtype contracts — Python adapter aligned to jingu-cognition v2.0 COGNITION_CONTRACTS.
#
# DO NOT add new principal names here. All names must exist in jingu-cognition/src/principals.ts.
# required_principals: gate-enforced (hard) — missing → RETRYABLE → repair_target routing
# forbidden_principals: gate-enforced (hard) — declared → REJECTED (fake principal / phase violation)
# expected_principals: quality signal (soft) — missing → inference diff warning only
# required_fields:     PhaseRecord attribute non-empty check — missing → RETRYABLE
# allowed_next:        legal next phases — violation → REJECTED (wrong phase position)
# ── Principal lifecycle enforcement contract ──────────────────────────────────
#
# CC3 invariant: a principal may only appear in required_principals if it has
# an inference rule in principal_inference._RULE_REGISTRY with matching applies_to.
# Principals without inference rules are NOT fake-checkable (stage < fake_checkable)
# and must be in expected_principals (soft quality signal) only.
#
# Principals WITH inference rules (fake_checkable, stage 4):
#   causal_grounding      — applies_to: ["analysis.root_cause"]
#   evidence_linkage      — applies_to: None (all subtypes)
#   minimal_change        — applies_to: ["execution.code_patch"]
#   alternative_hypothesis_check — applies_to: ["analysis.root_cause"]
#   invariant_preservation       — applies_to: ["judge.verification"]
#
# Principals WITHOUT inference rules (required_enforced, stage 2 — expected only):
#   ontology_alignment, phase_boundary_discipline, evidence_completeness,
#   action_grounding, option_comparison, constraint_satisfaction,
#   scope_minimality, result_verification, uncertainty_honesty, residual_risk_detection,
#   invariant_capture, design_comparison, constraint_encoding_justification
#
# This table is the enforcement boundary. Update it when inference rules are added.
# ─────────────────────────────────────────────────────────────────────────────

SUBTYPE_CONTRACTS: dict[str, SubtypeContract] = {
    "observation.fact_gathering": {
        "phase": "OBSERVE",
        # No principals are fake_checkable for OBSERVE subtype.
        # evidence_linkage rule applies to all subtypes but requires evidence_refs +
        # from_steps — agent output at OBSERVE rarely has from_steps, so treat as expected.
        "required_principals": [],
        "expected_principals": [
            "ontology_alignment",         # stage=required_enforced, no inference rule
            "phase_boundary_discipline",  # stage=required_enforced, no inference rule
            "evidence_completeness",      # stage=required_enforced, no inference rule
        ],
        "forbidden_principals": ["action_grounding", "minimal_change"],
        # Y-lite fix: evidence_refs regex match is too strict for OBSERVE.
        # Agent uses Read/Grep/Search tools (implicit evidence basis) but may not
        # write explicit EVIDENCE: file.py:N text → evidence_refs=[] → RETRYABLE loop.
        # Solution: require has_evidence_basis (evidence_refs OR from_steps OR observe_tool_signal),
        # where observe_tool_signal=True when agent used any observation-class tool in this step.
        "required_fields": [],
        "has_evidence_basis_required": True,  # evaluated by principal_gate (not field-presence)
        "allowed_next": ["ANALYZE", "OBSERVE"],
        "repair_target": "OBSERVE",
    },
    "analysis.root_cause": {
        # Derived from cognition_contracts/analysis_root_cause.py (single source of truth).
        "phase": _arc.PHASE,
        "required_principals": list(_arc.REQUIRED_PRINCIPALS),
        "expected_principals": list(_arc.EXPECTED_PRINCIPALS),
        "forbidden_principals": list(_arc.FORBIDDEN_PRINCIPALS),
        "required_fields": list(_arc.REQUIRED_RECORD_FIELDS),
        "has_evidence_basis_required": _arc.HAS_EVIDENCE_BASIS_REQUIRED,
        "allowed_next": list(_arc.ALLOWED_NEXT),
        "repair_target": _arc.REPAIR_TARGET,
    },
    "decision.fix_direction": {
        "phase": "DECIDE",
        # No principals are fake_checkable for decision.fix_direction subtype.
        "required_principals": [],
        "expected_principals": [
            "ontology_alignment",         # stage=required_enforced, no inference rule
            "phase_boundary_discipline",  # stage=required_enforced, no inference rule
            "option_comparison",          # stage=required_enforced, no inference rule
            "constraint_satisfaction",    # stage=required_enforced, no inference rule
            "uncertainty_honesty",
        ],
        "forbidden_principals": [],
        "required_fields": [],
        "allowed_next": ["DESIGN", "DECIDE", "ANALYZE"],
        "repair_target": "ANALYZE",
    },
    "design.solution_shape": {
        "phase": "DESIGN",
        # invariant_preservation has an inference rule (applies_to: judge.verification),
        # but NOT for design.solution_shape — so not fake_checkable here either.
        "required_principals": [],
        "expected_principals": [
            "ontology_alignment",         # stage=required_enforced, no inference rule
            "phase_boundary_discipline",  # stage=required_enforced, no inference rule
            "invariant_preservation",     # inference rule exists but applies_to=judge.verification only
            "scope_minimality",           # stage=required_enforced, no inference rule
            "design_comparison",          # stage=required_enforced, no inference rule — constraint encoding
            "constraint_encoding_justification",  # stage=required_enforced, no inference rule — constraint encoding
        ],
        "forbidden_principals": ["minimal_change"],
        "required_fields": [],
        "allowed_next": ["EXECUTE", "DESIGN", "DECIDE"],
        "repair_target": "DECIDE",
    },
    "execution.code_patch": {
        "phase": "EXECUTE",
        # minimal_change is fake_checkable for execution.code_patch.
        # action_grounding has no inference rule → expected only.
        # ontology_alignment + phase_boundary_discipline have no inference rule → expected only.
        "required_principals": [
            "minimal_change",
        ],
        "expected_principals": [
            "ontology_alignment",         # stage=required_enforced, no inference rule
            "phase_boundary_discipline",  # stage=required_enforced, no inference rule
            "action_grounding",           # stage=required_enforced, no inference rule
            "invariant_preservation",
        ],
        "forbidden_principals": [],
        "required_fields": [],
        "allowed_next": ["JUDGE", "EXECUTE", "ANALYZE"],
        "repair_target": "ANALYZE",
    },
    "judge.verification": {
        "phase": "JUDGE",
        # invariant_preservation is fake_checkable for judge.verification.
        # result_verification + uncertainty_honesty have no inference rule → expected only.
        "required_principals": [
            "invariant_preservation",
        ],
        "expected_principals": [
            "ontology_alignment",         # stage=required_enforced, no inference rule
            "result_verification",        # stage=required_enforced, no inference rule
            "uncertainty_honesty",        # stage=required_enforced, no inference rule
            "residual_risk_detection",
        ],
        "forbidden_principals": [],
        "required_fields": [],
        "allowed_next": ["EXECUTE", "ANALYZE"],
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


def get_forbidden_principals(phase: str) -> list[str]:
    """
    Return forbidden principals for the given phase.

    Gate-enforced: declaring a forbidden principal → REJECTED (fake principal / phase violation).
    Returns [] if phase has no contract.

    Args:
        phase: Phase name string (e.g. "ANALYZE", "OBSERVE"). Case-insensitive.
    """
    subtype = _PHASE_TO_SUBTYPE.get(phase.upper(), "")
    contract = SUBTYPE_CONTRACTS.get(subtype, {})
    return list(contract.get("forbidden_principals", []))


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


def get_required_fields(phase: str) -> list[str]:
    """
    Return required PhaseRecord fields for the given phase.

    RETRYABLE if any field is empty/missing: agent is in the right phase but produced
    incomplete output. Redirect to repair_target to gather missing material.
    Returns [] if phase has no contract.

    Args:
        phase: Phase name string (e.g. "ANALYZE"). Case-insensitive.
    """
    subtype = _PHASE_TO_SUBTYPE.get(phase.upper(), "")
    contract = SUBTYPE_CONTRACTS.get(subtype, {})
    return list(contract.get("required_fields", []))


def get_allowed_next(phase: str) -> list[str]:
    """
    Return the list of legal next phases for the given phase.

    REJECTED if the agent attempts to transition to a phase not in this list:
    this is a phase boundary error (wrong position), not a missing-material error.
    Returns [] if phase has no contract (no enforcement on unknown phase).

    Args:
        phase: Phase name string (e.g. "ANALYZE"). Case-insensitive.
    """
    subtype = _PHASE_TO_SUBTYPE.get(phase.upper(), "")
    contract = SUBTYPE_CONTRACTS.get(subtype, {})
    return list(contract.get("allowed_next", []))


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
