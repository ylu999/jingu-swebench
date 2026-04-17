"""
contract_registry.py — Unified access layer for all cognition contracts.

Single source of truth for:
- Looking up contracts by phase or subtype
- Getting required/expected/forbidden principals for any phase
- Getting required fields for any phase
- Getting field specs and gate rules for any phase
- Getting allowed next phases and repair targets

All data derives from cognition_contracts/*.py modules.
No consumer should hardcode contract data — import from here.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any

from canonical_symbols import ALL_PHASES, ALL_SUBTYPES, PHASE_TO_SUBTYPE
from cognition_contracts._base import FieldSpec, GateRule


# ── Contract view (read-only snapshot of a contract module) ──────────────────

@dataclass(frozen=True)
class ContractView:
    """Read-only view of a single cognition contract."""
    phase: str
    subtype: str

    required_principals: tuple[str, ...]
    expected_principals: tuple[str, ...]
    forbidden_principals: tuple[str, ...]

    allowed_next: tuple[str, ...]
    repair_target: str
    has_evidence_basis_required: bool

    required_record_fields: tuple[str, ...]
    field_specs: tuple[FieldSpec, ...]
    gate_rules: tuple[GateRule, ...]
    gate_threshold: float

    schema_properties: dict[str, Any] = field(default_factory=dict)
    schema_required: tuple[str, ...] = ()


# ── Module → subtype mapping ────────────────────────────────────────────────

_SUBTYPE_TO_MODULE: dict[str, str] = {
    "observation.fact_gathering": "cognition_contracts.observation_fact_gathering",
    "analysis.root_cause":       "cognition_contracts.analysis_root_cause",
    "decision.fix_direction":    "cognition_contracts.decision_fix_direction",
    "design.solution_shape":     "cognition_contracts.design_solution_shape",
    "execution.code_patch":      "cognition_contracts.execution_code_patch",
    "judge.verification":        "cognition_contracts.judge_verification",
}

# Cache loaded contracts
_CONTRACT_CACHE: dict[str, ContractView] = {}


def _load_contract(subtype: str) -> ContractView:
    """Load a contract module and build a ContractView."""
    if subtype in _CONTRACT_CACHE:
        return _CONTRACT_CACHE[subtype]

    module_name = _SUBTYPE_TO_MODULE.get(subtype)
    if not module_name:
        raise KeyError(f"No contract module for subtype '{subtype}'")

    mod = importlib.import_module(module_name)

    view = ContractView(
        phase=mod.PHASE,
        subtype=mod.SUBTYPE,
        required_principals=tuple(mod.REQUIRED_PRINCIPALS),
        expected_principals=tuple(mod.EXPECTED_PRINCIPALS),
        forbidden_principals=tuple(mod.FORBIDDEN_PRINCIPALS),
        allowed_next=tuple(mod.ALLOWED_NEXT),
        repair_target=mod.REPAIR_TARGET,
        has_evidence_basis_required=mod.HAS_EVIDENCE_BASIS_REQUIRED,
        required_record_fields=tuple(mod.REQUIRED_RECORD_FIELDS),
        field_specs=tuple(mod.FIELD_SPECS),
        gate_rules=tuple(mod.GATE_RULES),
        gate_threshold=mod.GATE_THRESHOLD,
        schema_properties=dict(mod.SCHEMA_PROPERTIES),
        schema_required=tuple(mod.SCHEMA_REQUIRED),
    )
    _CONTRACT_CACHE[subtype] = view
    return view


# ── Public API ──────────────────────────────────────────────────────────────

def get_contract_by_subtype(subtype: str) -> ContractView:
    """Get contract for a given subtype. Raises KeyError if unknown."""
    return _load_contract(subtype)


def get_contract_by_phase(phase: str) -> ContractView | None:
    """Get contract for a given canonical phase. Returns None if no contract."""
    subtype = PHASE_TO_SUBTYPE.get(phase)
    if not subtype:
        return None
    try:
        return _load_contract(subtype)
    except KeyError:
        return None


def get_required_principals(phase: str) -> tuple[str, ...]:
    """Get required principals for a phase."""
    c = get_contract_by_phase(phase)
    return c.required_principals if c else ()


def get_expected_principals(phase: str) -> tuple[str, ...]:
    """Get expected principals for a phase."""
    c = get_contract_by_phase(phase)
    return c.expected_principals if c else ()


def get_forbidden_principals(phase: str) -> tuple[str, ...]:
    """Get forbidden principals for a phase."""
    c = get_contract_by_phase(phase)
    return c.forbidden_principals if c else ()


def get_required_fields(phase: str) -> tuple[str, ...]:
    """Get required record fields for a phase."""
    c = get_contract_by_phase(phase)
    return c.required_record_fields if c else ()


def get_field_specs(phase: str) -> tuple[FieldSpec, ...]:
    """Get field specs for a phase."""
    c = get_contract_by_phase(phase)
    return c.field_specs if c else ()


def get_gate_rules(phase: str) -> tuple[GateRule, ...]:
    """Get gate rules for a phase."""
    c = get_contract_by_phase(phase)
    return c.gate_rules if c else ()


def get_allowed_next(phase: str) -> tuple[str, ...]:
    """Get allowed next phases from contract."""
    c = get_contract_by_phase(phase)
    return c.allowed_next if c else ()


def get_schema(phase: str) -> dict[str, Any]:
    """Get JSON schema properties for a phase."""
    c = get_contract_by_phase(phase)
    return c.schema_properties if c else {}


def get_schema_required(phase: str) -> tuple[str, ...]:
    """Get required schema fields for a phase."""
    c = get_contract_by_phase(phase)
    return c.schema_required if c else ()


def all_contracts() -> dict[str, ContractView]:
    """Load and return all contracts, keyed by subtype."""
    for subtype in _SUBTYPE_TO_MODULE:
        _load_contract(subtype)
    return dict(_CONTRACT_CACHE)


def clear_cache() -> None:
    """Clear the contract cache (for testing)."""
    _CONTRACT_CACHE.clear()
