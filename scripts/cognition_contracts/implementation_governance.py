"""
implementation_governance.py — Single source of truth for implementation governance contracts.

This file is the ONLY place where the implementation governance cognition contracts
are defined. All consumers (subtype_contracts, bundle.json, CI scripts, replay gate)
derive from this file.

Implementation Governance is a compile-time meta-governance layer, NOT a runtime
agent phase. The 5 subtypes describe audit/verification activities that detect
implementation drift (shadow contracts, projection drift, injection gaps).

Principals are CI-level principals (stage 0: ontology_registered), not
agent-declared principals. No inference rules initially.

Contract declare once, loader wires everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict


# -- Contract identity --------------------------------------------------------

PHASE = "IMPLEMENTATION_GOVERNANCE"


# -- Subtypes -----------------------------------------------------------------
# 5 audit/verification activities for detecting implementation drift.

SUBTYPES: list[str] = [
    "implementation.source_of_truth_identification",
    "implementation.injection_path_audit",
    "implementation.projection_derivation",
    "implementation.cross_surface_consistency",
    "implementation.shadow_contract_detection",
]


# -- Principals ---------------------------------------------------------------
# CI-level principals. All at stage 0 (ontology_registered), fakeCheckEligible=false.
# No inference rules initially.

PRINCIPALS: list[str] = [
    "single_source_of_truth_preservation",
    "projection_not_definition",
    "injection_path_accountability",
    "cross_surface_consistency",
    "no_shadow_contracts",
    "compile_over_handwrite",
]


# -- Policies -----------------------------------------------------------------
# Each policy maps to one enforcement rule in the implementation governance gate.


class PolicyDef(TypedDict):
    """Definition of one implementation governance policy."""
    id: str
    rule: str
    description: str


POLICIES: list[PolicyDef] = [
    {
        "id": "canonical_source_required",
        "rule": "Every contract must have a declared canonical source file",
        "description": (
            "Without a declared canonical source, there is no way to determine "
            "which copy is authoritative when surfaces diverge."
        ),
    },
    {
        "id": "projection_files_cannot_define_contracts",
        "rule": "Files that project/render contracts cannot add new fields",
        "description": (
            "Projection files (renderers, prompt builders) must only transform "
            "existing fields from the canonical source. Adding fields in a "
            "projection file creates a shadow contract."
        ),
    },
    {
        "id": "injection_path_must_be_traceable",
        "rule": "From source to every surface, the path must be code-traceable",
        "description": (
            "If the injection path from canonical source to a surface cannot be "
            "traced through imports/function calls, changes to the source may "
            "not propagate to that surface."
        ),
    },
    {
        "id": "cross_surface_delta_zero",
        "rule": "All surfaces for the same contract must produce identical field sets",
        "description": (
            "When two surfaces (e.g., tool description and phase prompt) present "
            "different field sets for the same contract, the agent receives "
            "contradictory instructions."
        ),
    },
    {
        "id": "shadow_contract_ci_detection",
        "rule": "CI must detect when a new hardcoded contract appears",
        "description": (
            "Shadow contracts (hardcoded field definitions outside the canonical "
            "source) must be detected at CI time, not discovered during debugging."
        ),
    },
    {
        "id": "compile_time_projection_verification",
        "rule": "Projection chain must be verified at compile/test time (replay gate)",
        "description": (
            "The projection from canonical source through renderer to each surface "
            "must be verified by automated tests, not manual inspection."
        ),
    },
]

POLICY_MAP: dict[str, PolicyDef] = {p["id"]: p for p in POLICIES}


# -- Failure taxonomy ---------------------------------------------------------
# Each failure type maps to a trigger condition and a repair target.


class FailureEntry(TypedDict):
    """One entry in the failure taxonomy."""
    trigger: str
    repair_target: str


FAILURE_TAXONOMY: dict[str, FailureEntry] = {
    "shadow_contract_detected": {
        "trigger": "Surface defines fields not in canonical source",
        "repair_target": "Delete shadow, wire to source",
    },
    "projection_drift": {
        "trigger": "Surface has stale copy of source",
        "repair_target": "Re-derive from source",
    },
    "injection_gap": {
        "trigger": "Source exists but is not injected to a surface",
        "repair_target": "Fix injection path",
    },
    "cross_surface_inconsistency": {
        "trigger": "Two surfaces disagree on same contract",
        "repair_target": "Find which is stale, re-derive",
    },
    "missing_canonical_source": {
        "trigger": "Contract has no declared source",
        "repair_target": "Declare source, migrate copies",
    },
}


# -- Repair routing -----------------------------------------------------------
# Maps each failure type to the layer that must be fixed.

REPAIR_ROUTING: dict[str, str] = {
    "shadow_contract_detected": "consumer_surface",
    "projection_drift": "projection_renderer",
    "injection_gap": "injection_path",
    "cross_surface_inconsistency": "stale_surface",
    "missing_canonical_source": "canonical_source",
}


# -- Phase transitions --------------------------------------------------------
# Implementation governance subtypes only transition within the same phase.
# This is a meta-governance layer, not part of the main reasoning loop.

ALLOWED_NEXT: list[str] = ["IMPLEMENTATION_GOVERNANCE"]
REPAIR_TARGET: str = "IMPLEMENTATION_GOVERNANCE"


# -- Subtype contract helpers -------------------------------------------------
# Used by subtype_contracts.py to derive SubtypeContract entries.

def get_subtype_contract_entry(subtype: str) -> dict:
    """
    Build a SubtypeContract-compatible dict for the given implementation
    governance subtype.

    All implementation governance subtypes share the same structure:
    - No required principals (all at stage 0)
    - Expected principals mapped per subtype
    - No forbidden principals
    - No required fields (compile-time checks, not PhaseRecord)
    - Self-referencing transitions

    Args:
        subtype: One of the 5 implementation governance subtype strings.

    Returns:
        dict compatible with SubtypeContract TypedDict.

    Raises:
        ValueError: if subtype is not a known implementation governance subtype.
    """
    if subtype not in SUBTYPES:
        raise ValueError(
            f"Unknown implementation governance subtype: {subtype!r}. "
            f"Known: {SUBTYPES}"
        )

    # Map each subtype to its most relevant expected principals
    _SUBTYPE_EXPECTED: dict[str, list[str]] = {
        "implementation.source_of_truth_identification": [
            "single_source_of_truth_preservation",
        ],
        "implementation.injection_path_audit": [
            "injection_path_accountability",
        ],
        "implementation.projection_derivation": [
            "projection_not_definition",
            "compile_over_handwrite",
        ],
        "implementation.cross_surface_consistency": [
            "cross_surface_consistency",
        ],
        "implementation.shadow_contract_detection": [
            "no_shadow_contracts",
        ],
    }

    return {
        "phase": PHASE,
        "required_principals": [],
        "expected_principals": _SUBTYPE_EXPECTED.get(subtype, []),
        "forbidden_principals": [],
        "required_fields": [],
        "allowed_next": list(ALLOWED_NEXT),
        "repair_target": REPAIR_TARGET,
    }
