"""
_base.py — Single source of truth for contract definition types.

Defines FieldSpec, GateRule, ContractDefinition Protocol, and
validate_contract_definition(). All cognition contract modules
(analysis_root_cause, observation_fact_gathering, etc.) import
FieldSpec and GateRule from here and implement ContractDefinition.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


# ── Data types ───────────────────────────────────────────────────────────────

@dataclass
class FieldSpec:
    """Specification for a single field in a cognition contract."""
    name: str
    description: str
    required: bool
    min_length: int | None = None
    semantic_check: str | None = None


@dataclass
class GateRule:
    """One evaluation rule in a cognition gate."""
    name: str
    field: str           # PhaseRecord field this rule evaluates
    repair_hint: str     # hint injected into SDG repair on failure
    threshold: float = 0.5


# ── ContractDefinition Protocol ──────────────────────────────────────────────

@runtime_checkable
class ContractDefinition(Protocol):
    """
    Protocol defining the 14 attribute groups every cognition contract
    module must export. Used by compile_contract(), drift_audit, and
    validate_contract_definition().
    """

    # 1. Contract identity
    PHASE: str
    SUBTYPE: str

    # 2. Principals
    REQUIRED_PRINCIPALS: list[str]
    EXPECTED_PRINCIPALS: list[str]
    FORBIDDEN_PRINCIPALS: list[str]

    # 3. Phase transitions
    ALLOWED_NEXT: list[str]
    REPAIR_TARGET: str
    HAS_EVIDENCE_BASIS_REQUIRED: bool

    # 4. PhaseRecord required fields
    REQUIRED_RECORD_FIELDS: list[str]

    # 5. Gate field specifications
    FIELD_SPECS: list[FieldSpec]

    # 6. Gate rules
    GATE_RULES: list[GateRule]
    GATE_THRESHOLD: float

    # 7. Prompt
    PROMPT_REQUIRED_SECTIONS: list[str]
    PROMPT_GUIDANCE: str

    # 8. Schema
    SCHEMA_PROPERTIES: dict
    SCHEMA_REQUIRED: list[str]


# ── Helper functions ─────────────────────────────────────────────────────────

def build_field_spec_map(specs: list[FieldSpec]) -> dict[str, FieldSpec]:
    """Build name -> FieldSpec lookup from a list of FieldSpecs."""
    return {fs.name: fs for fs in specs}


def build_gate_required_fields(specs: list[FieldSpec]) -> list[str]:
    """Extract names of required fields from a list of FieldSpecs."""
    return [fs.name for fs in specs if fs.required]


def build_gate_rule_map(rules: list[GateRule]) -> dict[str, GateRule]:
    """Build name -> GateRule lookup from a list of GateRules."""
    return {r.name: r for r in rules}


# ── Contract validation ──────────────────────────────────────────────────────

# The 14 attribute groups that ContractDefinition requires, with expected types.
_REQUIRED_ATTRS: list[tuple[str, type | None]] = [
    ("PHASE", str),
    ("SUBTYPE", str),
    ("REQUIRED_PRINCIPALS", list),
    ("EXPECTED_PRINCIPALS", list),
    ("FORBIDDEN_PRINCIPALS", list),
    ("ALLOWED_NEXT", list),
    ("REPAIR_TARGET", str),
    ("HAS_EVIDENCE_BASIS_REQUIRED", bool),
    ("REQUIRED_RECORD_FIELDS", list),
    ("FIELD_SPECS", list),
    ("GATE_RULES", list),
    ("GATE_THRESHOLD", float),
    ("PROMPT_REQUIRED_SECTIONS", list),
    ("PROMPT_GUIDANCE", str),
    ("SCHEMA_PROPERTIES", dict),
    ("SCHEMA_REQUIRED", list),
]

# Hardcoded field description patterns that should NOT appear in PROMPT_GUIDANCE.
# Presence indicates the prompt is duplicating field descriptions that should be
# rendered at runtime from the bundle schema.
_HARDCODED_FIELD_PATTERNS = [
    re.compile(r"root_cause:\s+.{20,}", re.IGNORECASE),
    re.compile(r"causal_chain:\s+.{20,}", re.IGNORECASE),
    re.compile(r"evidence_refs:\s+.{20,}", re.IGNORECASE),
]


def validate_contract_definition(module: Any) -> list[str]:
    """
    Validate that a module satisfies the ContractDefinition protocol.

    Returns a list of error strings. Empty list = valid contract.

    10 checks:
      1. All Protocol attributes present
      2. PHASE is non-empty string
      3. SUBTYPE matches <phase_lower>.<task_shape> pattern
      4. REQUIRED_PRINCIPALS and FORBIDDEN_PRINCIPALS are disjoint
      5. Every GateRule.field references a field in FIELD_SPECS
      6. SCHEMA_REQUIRED is subset of SCHEMA_PROPERTIES keys
      7. Every required FieldSpec has a corresponding key in SCHEMA_PROPERTIES
      8. GATE_THRESHOLD in (0.0, 1.0]
      9. ALLOWED_NEXT is non-empty
      10. PROMPT_GUIDANCE does not contain hardcoded field descriptions
    """
    errors: list[str] = []

    # Check 1: All Protocol attributes present
    for attr_name, expected_type in _REQUIRED_ATTRS:
        if not hasattr(module, attr_name):
            errors.append(f"[1] Missing attribute: {attr_name}")
            continue
        val = getattr(module, attr_name)
        if expected_type is not None and not isinstance(val, expected_type):
            # float check: also accept int (e.g. GATE_THRESHOLD = 1)
            if expected_type is float and isinstance(val, (int, float)):
                continue
            errors.append(
                f"[1] Attribute {attr_name} has type {type(val).__name__}, "
                f"expected {expected_type.__name__}"
            )

    # Early exit if critical attrs missing — remaining checks would fail
    critical = {"PHASE", "SUBTYPE", "FIELD_SPECS", "GATE_RULES",
                "SCHEMA_PROPERTIES", "SCHEMA_REQUIRED", "REQUIRED_PRINCIPALS",
                "FORBIDDEN_PRINCIPALS", "ALLOWED_NEXT", "PROMPT_GUIDANCE",
                "GATE_THRESHOLD"}
    missing_critical = [a for a in critical if not hasattr(module, a)]
    if missing_critical:
        return errors  # can't run remaining checks

    phase = getattr(module, "PHASE")
    subtype = getattr(module, "SUBTYPE")
    field_specs: list[FieldSpec] = getattr(module, "FIELD_SPECS")
    gate_rules: list[GateRule] = getattr(module, "GATE_RULES")
    schema_props: dict = getattr(module, "SCHEMA_PROPERTIES")
    schema_req: list[str] = getattr(module, "SCHEMA_REQUIRED")
    req_principals: list[str] = getattr(module, "REQUIRED_PRINCIPALS")
    forbidden_principals: list[str] = getattr(module, "FORBIDDEN_PRINCIPALS")
    allowed_next: list[str] = getattr(module, "ALLOWED_NEXT")
    prompt_guidance: str = getattr(module, "PROMPT_GUIDANCE")
    gate_threshold: float = getattr(module, "GATE_THRESHOLD")

    # Check 2: PHASE is non-empty string
    if not isinstance(phase, str) or not phase.strip():
        errors.append("[2] PHASE must be a non-empty string")

    # Check 3: SUBTYPE matches <category>.<task_shape> pattern
    # Note: the category prefix is phase-related but not always PHASE.lower()
    # (e.g., PHASE="ANALYZE" -> subtype="analysis.root_cause", not "analyze.root_cause")
    if isinstance(phase, str) and isinstance(subtype, str):
        if not re.match(r"^[a-z]+\.[a-z_]+$", subtype):
            errors.append(
                f"[3] SUBTYPE '{subtype}' must match pattern '<category>.<task_shape>' "
                f"(lowercase letters, single dot separator, underscores in task_shape)"
            )

    # Check 4: REQUIRED_PRINCIPALS and FORBIDDEN_PRINCIPALS are disjoint
    overlap = set(req_principals) & set(forbidden_principals)
    if overlap:
        errors.append(
            f"[4] REQUIRED_PRINCIPALS and FORBIDDEN_PRINCIPALS overlap: {sorted(overlap)}"
        )

    # Check 5: Every GateRule.field references a field in FIELD_SPECS
    field_names = {fs.name for fs in field_specs}
    for rule in gate_rules:
        if rule.field not in field_names:
            errors.append(
                f"[5] GateRule '{rule.name}' references field '{rule.field}' "
                f"not in FIELD_SPECS (available: {sorted(field_names)})"
            )

    # Check 6: SCHEMA_REQUIRED is subset of SCHEMA_PROPERTIES keys
    prop_keys = set(schema_props.keys())
    for req_key in schema_req:
        if req_key not in prop_keys:
            errors.append(
                f"[6] SCHEMA_REQUIRED key '{req_key}' not in SCHEMA_PROPERTIES"
            )

    # Check 7: Every required FieldSpec has a corresponding key in SCHEMA_PROPERTIES
    for fs in field_specs:
        if fs.required and fs.name not in prop_keys:
            errors.append(
                f"[7] Required FieldSpec '{fs.name}' has no key in SCHEMA_PROPERTIES"
            )

    # Check 8: GATE_THRESHOLD in (0.0, 1.0]
    if not (0.0 < gate_threshold <= 1.0):
        errors.append(
            f"[8] GATE_THRESHOLD must be in (0.0, 1.0], got {gate_threshold}"
        )

    # Check 9: ALLOWED_NEXT is non-empty
    if not allowed_next:
        errors.append("[9] ALLOWED_NEXT must be non-empty")

    # Check 10: PROMPT_GUIDANCE does not contain hardcoded field descriptions
    for pattern in _HARDCODED_FIELD_PATTERNS:
        if pattern.search(prompt_guidance):
            errors.append(
                f"[10] PROMPT_GUIDANCE contains hardcoded field description "
                f"matching pattern '{pattern.pattern}' — field descriptions "
                f"should be rendered at runtime from the bundle schema"
            )

    return errors
