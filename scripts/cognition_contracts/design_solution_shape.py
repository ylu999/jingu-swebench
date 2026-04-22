"""
design_solution_shape.py — Single source of truth for design.solution_shape contract.

This file is the ONLY place where the design.solution_shape cognition contract
is defined. All consumers (subtype_contracts, phase_prompt, design_gate,
phase_schemas, phase_record) derive from this file.

Contract declare once, loader wires everywhere.
"""

from __future__ import annotations

from cognition_contracts._base import FieldSpec, GateRule


# ── Contract identity ────────────────────────────────────────────────────────

PHASE = "DESIGN"
SUBTYPE = "design.solution_shape"


# ── Principals ───────────────────────────────────────────────────────────────
# Who: subtype_contracts.py, phase_prompt.py (principal guidance)

REQUIRED_PRINCIPALS: list[str] = [
    "ontology_alignment",
]

EXPECTED_PRINCIPALS: list[str] = [
    "constraint_satisfaction",
]

FORBIDDEN_PRINCIPALS: list[str] = []


# ── Phase transitions ────────────────────────────────────────────────────────
# Who: subtype_contracts.py

ALLOWED_NEXT: list[str] = ["EXECUTE", "DESIGN"]
REPAIR_TARGET: str = "DESIGN"
HAS_EVIDENCE_BASIS_REQUIRED: bool = False


# ── PhaseRecord required fields ──────────────────────────────────────────────
# Who: subtype_contracts.py (required_fields), phase_record.py (field defs)

REQUIRED_RECORD_FIELDS: list[str] = []


# ── Gate field specifications ────────────────────────────────────────────────
# Who: design_gate.py, gate_rejection.py (SDG)

FIELD_SPECS: list[FieldSpec] = [
    FieldSpec(
        name="files_to_modify",
        description="Files that will be modified",
        required=True,
    ),
    FieldSpec(
        name="scope_boundary",
        description="What is in/out of scope",
        required=True,
    ),
    FieldSpec(
        name="invariants",
        description="System invariants to preserve",
        required=False,
    ),
    FieldSpec(
        name="design_comparison",
        description="Comparative design options",
        required=False,
        semantic_check="options_structurally_distinct",
    ),
    FieldSpec(
        name="test_to_code_link",
        description="Mapping from failing test(s) to the code location that needs to change",
        required=True,
        min_length=10,
    ),
    FieldSpec(
        name="change_mechanism",
        description="How the fix works: what behavior changes and why it resolves the failing test",
        required=True,
        min_length=10,
    ),
    FieldSpec(
        name="rejected_alternative",
        description="At least one alternative approach considered and why it was rejected",
        required=True,
        min_length=10,
    ),
]

# Convenience: name -> FieldSpec lookup
FIELD_SPEC_MAP: dict[str, FieldSpec] = {fs.name: fs for fs in FIELD_SPECS}

# Required field names (for ContractView.required_fields)
GATE_REQUIRED_FIELDS: list[str] = [fs.name for fs in FIELD_SPECS if fs.required]


# ── Gate rules ───────────────────────────────────────────────────────────────
# Who: design_gate.py (evaluate_design)
# Each rule maps to a check function name, the field it evaluates, and the
# repair hint injected on failure.

GATE_RULES: list[GateRule] = [
    GateRule(
        name="scope_bounded",
        field="files_to_modify",
        repair_hint="Identify specific files to modify -- scope must be bounded",
    ),
    GateRule(
        name="invariants_identified",
        field="invariants",
        repair_hint="List system invariants your design must preserve",
    ),
    GateRule(
        name="constraint_encoding",
        field="scope_boundary",
        repair_hint="If using allowlist, justify completeness",
    ),
    GateRule(
        name="target_files_bounded",
        field="files_to_modify",
        repair_hint="Limit files_to_modify to at most 3 specific files. If you need more, re-analyze the problem scope.",
    ),
    GateRule(
        name="test_code_linked",
        field="test_to_code_link",
        repair_hint="Provide test_to_code_link: which failing test maps to which code location, and why changing that code fixes the test.",
    ),
    GateRule(
        name="alternative_considered",
        field="rejected_alternative",
        repair_hint="Provide rejected_alternative: name at least one other approach you considered and explain why you chose not to use it.",
    ),
    GateRule(
        name="change_mechanism_present",
        field="change_mechanism",
        repair_hint="Provide change_mechanism: explain what behavior changes (current → new) and why the failing tests should pass after this change.",
    ),
]

GATE_RULE_MAP: dict[str, GateRule] = {r.name: r for r in GATE_RULES}

# Default threshold for all rules
GATE_THRESHOLD: float = 0.5


# ── Prompt template sections ────────────────────────────────────────────────
# Who: phase_prompt.py (_DESIGN_GUIDANCE)

PROMPT_REQUIRED_SECTIONS: list[str] = []

PROMPT_GUIDANCE = (
    "Define the solution shape before writing code.\n\n"
    "Rules:\n"
    "1. Identify which files will be modified and bound the scope (max 3 files).\n"
    "2. Link failing test(s) to the specific code that needs to change (test_to_code_link).\n"
    "3. Explain HOW the fix works — what behavior changes and why (change_mechanism).\n"
    "4. Name at least one alternative approach you considered and why you rejected it (rejected_alternative).\n"
    "5. List invariants that the fix must preserve.\n"
    "6. Do NOT write production code yet.\n"
)
# NOTE: Field descriptions are NO LONGER listed here. They are rendered at
# runtime from the bundle schema by schema_field_guidance.render_schema_field_guidance().


# ── Structured output schema fields ─────────────────────────────────────────
# Who: phase_schemas.py (DESIGN schema)
# JSON schema properties for structured output tool call.

SCHEMA_PROPERTIES: dict = {
    "phase": {
        "type": "string",
        "enum": [PHASE],
        "description": "Current reasoning phase.",
    },
    "subtype": {
        "type": "string",
        "enum": [SUBTYPE],
        "description": f"The subtype of output: '{SUBTYPE}'.",
    },
    "principals": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Principal atoms declared for this phase.",
    },
    "files_to_modify": {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 1,
        "description": FIELD_SPEC_MAP["files_to_modify"].description,
    },
    "scope_boundary": {
        "type": "string",
        "minLength": 1,
        "description": FIELD_SPEC_MAP["scope_boundary"].description,
    },
    "invariants": {
        "type": "array",
        "items": {"type": "string"},
        "description": FIELD_SPEC_MAP["invariants"].description,
    },
    "design_comparison": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "approach": {"type": "string"},
                "pros": {"type": "array", "items": {"type": "string"}},
                "cons": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["approach"],
        },
        "description": FIELD_SPEC_MAP["design_comparison"].description,
    },
    "test_to_code_link": {
        "type": "string",
        "minLength": 10,
        "description": FIELD_SPEC_MAP["test_to_code_link"].description,
    },
    "change_mechanism": {
        "type": "string",
        "minLength": 10,
        "description": FIELD_SPEC_MAP["change_mechanism"].description,
    },
    "rejected_alternative": {
        "type": "string",
        "minLength": 10,
        "description": FIELD_SPEC_MAP["rejected_alternative"].description,
    },
}

SCHEMA_REQUIRED: list[str] = [
    "phase", "subtype", "principals", "files_to_modify", "scope_boundary",
    "test_to_code_link", "change_mechanism", "rejected_alternative",
]
