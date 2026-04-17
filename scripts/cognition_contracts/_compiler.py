"""
_compiler.py — Compile a ContractDefinition module into a BundleContractOutput.

Transforms the 14 attribute groups of a ContractDefinition into the 8-section
format consumed by the runtime bundle (bundle.json). This is the single
compilation path: contract module -> BundleContractOutput -> bundle JSON.

Usage:
    from cognition_contracts._compiler import compile_contract
    from cognition_contracts import analysis_root_cause as arc
    output = compile_contract(arc)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

from cognition_contracts._base import (
    FieldSpec,
    GateRule,
    validate_contract_definition,
)


# ── Output types ─────────────────────────────────────────────────────────────

@dataclass
class BundleContractOutput:
    """The 8-section output format matching bundle.json contract entries."""
    phase_spec: dict = field(default_factory=dict)
    cognition_spec: dict = field(default_factory=dict)
    principals: list[dict] = field(default_factory=list)
    policy: dict = field(default_factory=dict)
    schema: dict = field(default_factory=dict)
    prompt: str = ""
    repair_templates: dict = field(default_factory=dict)
    routing: dict = field(default_factory=dict)


class CompilationError(Exception):
    """Raised when a contract module fails validation before compilation."""
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(f"Contract compilation failed: {errors}")


# ── Default registries (overridable via kwargs) ─────────────────────────────

_DEFAULT_PHASE_GOALS: dict[str, str] = {
    "UNDERSTAND": "Understand the problem statement and constraints.",
    "OBSERVE": "Gather evidence from the environment: read files, traces, logs, tests.",
    "ANALYZE": "Root-cause analysis: connect evidence to a causal hypothesis.",
    "DECIDE": "Choose the fix direction based on analysis.",
    "DESIGN": "Design the solution shape before writing code.",
    "EXECUTE": "Write the minimal code patch that fixes the issue.",
    "JUDGE": "Verify the fix: run tests, check correctness, assess completeness.",
}

_DEFAULT_PHASE_FORBIDDEN_MOVES: dict[str, list[str]] = {
    "UNDERSTAND": [
        "do not gather evidence yet (that is OBSERVE)",
        "do not write code",
    ],
    "OBSERVE": [
        "do not analyze or hypothesize (that is ANALYZE)",
        "do not propose solutions",
        "do not write code",
    ],
    "ANALYZE": [
        "do not write code",
        "do not skip evidence",
        "do not jump to a fix without causal reasoning",
    ],
    "DECIDE": [
        "do not write code yet",
        "do not skip analysis",
    ],
    "DESIGN": [
        "do not write code yet (that is EXECUTE)",
        "do not skip the decision",
    ],
    "EXECUTE": [
        "do not re-analyze (that is ANALYZE)",
        "do not make changes unrelated to the fix",
    ],
    "JUDGE": [
        "do not write new code (that is EXECUTE)",
        "do not skip test verification",
    ],
}


# ── Section builders ─────────────────────────────────────────────────────────

def _build_phase_spec(
    definition: ModuleType,
    phase_goals: dict[str, str],
    phase_forbidden_moves: dict[str, list[str]],
) -> dict:
    """Build the phase_spec section."""
    phase = definition.PHASE
    return {
        "name": phase,
        "goal": phase_goals.get(phase, f"Phase {phase}"),
        "forbidden_moves": phase_forbidden_moves.get(phase, []),
        "allowed_next_phases": list(definition.ALLOWED_NEXT),
        "default_schema": definition.SUBTYPE,
    }


def _build_cognition_spec(definition: ModuleType) -> dict:
    """Build the cognition_spec section."""
    subtype = definition.SUBTYPE
    task_shape = subtype.split(".")[-1]
    field_specs: list[FieldSpec] = definition.FIELD_SPECS

    success_criteria = []
    for fs in field_specs:
        if fs.required:
            success_criteria.append(
                f"{fs.name} provided"
                if not fs.semantic_check
                else f"{fs.name}: {fs.semantic_check}"
            )

    required_evidence_kinds = []
    for fs in field_specs:
        if fs.required and fs.semantic_check:
            required_evidence_kinds.append(fs.semantic_check)

    return {
        "type": subtype,
        "phase": definition.PHASE,
        "task_shape": task_shape,
        "success_criteria": success_criteria,
        "required_evidence_kinds": required_evidence_kinds,
        "schema_ref": subtype,
    }


def _build_principals(
    definition: ModuleType,
    principal_registry: dict | None,
) -> list[dict]:
    """Build the principals section from required + expected principals."""
    principals: list[dict] = []
    subtype = definition.SUBTYPE

    all_principals: list[tuple[str, str]] = []
    for p in definition.REQUIRED_PRINCIPALS:
        all_principals.append((p, "required"))
    for p in definition.EXPECTED_PRINCIPALS:
        all_principals.append((p, "expected"))

    for name, role in all_principals:
        entry: dict[str, Any] = {
            "name": name,
            "role": role,
            "applies_to": [subtype],
        }

        # Enrich from registry if available
        if principal_registry and name in principal_registry:
            reg = principal_registry[name]
            if "applies_to" in reg:
                entry["applies_to"] = reg["applies_to"]
            if "requires_fields" in reg:
                entry["requires_fields"] = reg["requires_fields"]
            if "semantic_checks" in reg:
                entry["semantic_checks"] = reg["semantic_checks"]
            if "repair_hint" in reg:
                entry["repair_hint"] = reg["repair_hint"]
            entry["inference_rule_exists"] = reg.get("inference_rule_exists", False)
            entry["fake_check_eligible"] = reg.get("fake_check_eligible", False)

        principals.append(entry)

    return principals


def _build_policy(definition: ModuleType) -> dict:
    """Build the policy section."""
    field_specs: list[FieldSpec] = definition.FIELD_SPECS
    return {
        "id": f"policy:{definition.SUBTYPE}",
        "phase": definition.PHASE,
        "subtype": definition.SUBTYPE,
        "required_fields": [fs.name for fs in field_specs if fs.required],
        "required_principals": list(definition.REQUIRED_PRINCIPALS),
        "forbidden_principals": list(definition.FORBIDDEN_PRINCIPALS),
        "gate_threshold": float(definition.GATE_THRESHOLD),
        "schema_ref": definition.SUBTYPE,
    }


def _build_schema(definition: ModuleType) -> dict:
    """Build the schema section."""
    return {
        "type": "object",
        "properties": dict(definition.SCHEMA_PROPERTIES),
        "required": list(definition.SCHEMA_REQUIRED),
    }


def _build_prompt(
    definition: ModuleType,
    phase_goals: dict[str, str],
    phase_forbidden_moves: dict[str, list[str]],
) -> str:
    """Build the prompt section from contract attributes."""
    phase = definition.PHASE
    subtype = definition.SUBTYPE
    task_shape = subtype.split(".")[-1]
    field_specs: list[FieldSpec] = definition.FIELD_SPECS

    lines: list[str] = []

    # Header
    lines.append(f"## Phase: {phase}")
    goal = phase_goals.get(phase, f"Phase {phase}")
    lines.append(f"Goal: {goal}")
    lines.append(f"Subtype: {subtype} ({task_shape})")
    lines.append("")

    # Required fields
    required_fields = [fs.name for fs in field_specs if fs.required]
    if required_fields:
        lines.append("## Required Fields")
        for f in required_fields:
            lines.append(f"- {f}")
        lines.append("")

    # Forbidden moves
    forbidden = phase_forbidden_moves.get(phase, [])
    if forbidden:
        lines.append("## Forbidden Moves")
        for fm in forbidden:
            lines.append(f"- {fm}")
        lines.append("")

    # Required principals
    if definition.REQUIRED_PRINCIPALS:
        lines.append("## Required Principals")
        names = ", ".join(definition.REQUIRED_PRINCIPALS)
        lines.append(f"You MUST declare: {names}")
        lines.append("")

    # Forbidden principals
    if definition.FORBIDDEN_PRINCIPALS:
        lines.append("## Forbidden Principals")
        names = ", ".join(definition.FORBIDDEN_PRINCIPALS)
        lines.append(f"Do NOT declare: {names}")
        lines.append("")

    # Custom guidance
    if definition.PROMPT_GUIDANCE:
        lines.append("## Guidance")
        lines.append(definition.PROMPT_GUIDANCE.strip())

    return "\n".join(lines)


def _build_repair_templates(definition: ModuleType) -> dict:
    """Build the repair_templates section from gate rules."""
    gate_rules: list[GateRule] = definition.GATE_RULES
    templates: dict[str, dict] = {}
    for rule in gate_rules:
        templates[rule.name] = {
            "hint": rule.repair_hint,
            "field": rule.field,
            "threshold": rule.threshold,
        }
    return templates


def _build_routing(definition: ModuleType) -> dict:
    """Build the routing section.

    principal_routes must cover ALL required + expected principals (S4 check).
    Each entry maps principal_name -> {next_phase, strategy}.
    Gate rules provide strategy hints; principals without a gate rule get
    a generic strategy derived from the repair target.
    """
    gate_rules: list[GateRule] = definition.GATE_RULES
    repair_target = getattr(definition, "REPAIR_TARGET", definition.PHASE)

    # Build gate rule lookup: rule.name -> repair_hint
    gate_hint: dict[str, str] = {}
    for rule in gate_rules:
        gate_hint[rule.name] = rule.repair_hint

    # All principals that need routing entries
    all_principals: list[str] = list(definition.REQUIRED_PRINCIPALS) + list(definition.EXPECTED_PRINCIPALS)

    principal_routes: dict[str, dict] = {}
    for p in all_principals:
        hint = gate_hint.get(p)
        principal_routes[p] = {
            "next_phase": repair_target,
            "strategy": hint if hint else f"redirect to {repair_target} for repair",
        }

    return {
        "default_route": {
            "next_phase": repair_target,
            "strategy": f"redirect to {repair_target} for repair",
        },
        "principal_routes": principal_routes,
    }


# ── Main compiler ────────────────────────────────────────────────────────────

def compile_contract(
    definition: ModuleType,
    *,
    principal_registry: dict | None = None,
    phase_goals: dict[str, str] | None = None,
    phase_forbidden_moves: dict[str, list[str]] | None = None,
) -> BundleContractOutput:
    """
    Transform a ContractDefinition module into a BundleContractOutput.

    Args:
        definition: A module implementing the ContractDefinition protocol
            (e.g., analysis_root_cause).
        principal_registry: Optional dict of principal metadata for enrichment.
        phase_goals: Optional phase -> goal string mapping.
            Defaults to _DEFAULT_PHASE_GOALS.
        phase_forbidden_moves: Optional phase -> forbidden moves mapping.
            Defaults to _DEFAULT_PHASE_FORBIDDEN_MOVES.

    Returns:
        BundleContractOutput with all 8 sections populated.

    Raises:
        CompilationError: If the module fails validate_contract_definition().
    """
    # 1. Validate first — reject invalid contracts before compilation
    errors = validate_contract_definition(definition)
    if errors:
        raise CompilationError(errors)

    # 2. Resolve registries
    goals = phase_goals if phase_goals is not None else _DEFAULT_PHASE_GOALS
    forbidden = (
        phase_forbidden_moves
        if phase_forbidden_moves is not None
        else _DEFAULT_PHASE_FORBIDDEN_MOVES
    )

    # 3. Build each section
    return BundleContractOutput(
        phase_spec=_build_phase_spec(definition, goals, forbidden),
        cognition_spec=_build_cognition_spec(definition),
        principals=_build_principals(definition, principal_registry),
        policy=_build_policy(definition),
        schema=_build_schema(definition),
        prompt=_build_prompt(definition, goals, forbidden),
        repair_templates=_build_repair_templates(definition),
        routing=_build_routing(definition),
    )


# ── Round-trip validation ────────────────────────────────────────────────────

def validate_round_trip(
    definition: ModuleType,
    bundle_section: dict,
) -> list[str]:
    """
    Compare compile_contract() output against an existing bundle section.

    Returns list of mismatch descriptions. Empty list = equivalent.
    This is used to detect drift between the contract source and the
    deployed bundle.
    """
    mismatches: list[str] = []

    compiled = compile_contract(definition)

    # Compare schema.required
    compiled_req = set(compiled.schema.get("required", []))
    bundle_req = set(bundle_section.get("schema", {}).get("required", []))
    if compiled_req != bundle_req:
        only_compiled = compiled_req - bundle_req
        only_bundle = bundle_req - compiled_req
        mismatches.append(
            f"schema.required mismatch: "
            f"only_in_compiled={sorted(only_compiled)}, "
            f"only_in_bundle={sorted(only_bundle)}"
        )

    # Compare schema.properties keys
    compiled_props = set(compiled.schema.get("properties", {}).keys())
    bundle_props = set(
        bundle_section.get("schema", {}).get("properties", {}).keys()
    )
    if compiled_props != bundle_props:
        only_compiled = compiled_props - bundle_props
        only_bundle = bundle_props - compiled_props
        mismatches.append(
            f"schema.properties keys mismatch: "
            f"only_in_compiled={sorted(only_compiled)}, "
            f"only_in_bundle={sorted(only_bundle)}"
        )

    # Compare policy.required_fields
    compiled_policy_req = set(compiled.policy.get("required_fields", []))
    bundle_policy_req = set(
        bundle_section.get("policy", {}).get("required_fields", [])
    )
    if compiled_policy_req != bundle_policy_req:
        mismatches.append(
            f"policy.required_fields mismatch: "
            f"compiled={sorted(compiled_policy_req)}, "
            f"bundle={sorted(bundle_policy_req)}"
        )

    # Compare policy.required_principals
    compiled_principals = set(compiled.policy.get("required_principals", []))
    bundle_principals = set(
        bundle_section.get("policy", {}).get("required_principals", [])
    )
    if compiled_principals != bundle_principals:
        mismatches.append(
            f"policy.required_principals mismatch: "
            f"compiled={sorted(compiled_principals)}, "
            f"bundle={sorted(bundle_principals)}"
        )

    # Compare phase_spec.allowed_next_phases
    compiled_next = set(
        compiled.phase_spec.get("allowed_next_phases", [])
    )
    bundle_next = set(
        bundle_section.get("phase_spec", {}).get("allowed_next_phases", [])
    )
    if compiled_next != bundle_next:
        mismatches.append(
            f"phase_spec.allowed_next_phases mismatch: "
            f"compiled={sorted(compiled_next)}, "
            f"bundle={sorted(bundle_next)}"
        )

    return mismatches
