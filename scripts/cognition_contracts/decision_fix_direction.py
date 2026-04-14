"""
decision_fix_direction.py — Single source of truth for decision.fix_direction contract.

This file is the ONLY place where the decision.fix_direction cognition contract
is defined. All consumers (subtype_contracts, phase_prompt, decide_gate,
phase_schemas, phase_record) derive from this file.

Contract declare once, loader wires everywhere.
"""

from __future__ import annotations

from cognition_contracts._base import FieldSpec, GateRule


# -- Contract identity --------------------------------------------------------

PHASE = "DECIDE"
SUBTYPE = "decision.fix_direction"


# -- Principals ---------------------------------------------------------------
# Who: subtype_contracts.py, phase_prompt.py (principal guidance)

REQUIRED_PRINCIPALS: list[str] = [
    "option_comparison",
]

EXPECTED_PRINCIPALS: list[str] = [
    "constraint_satisfaction",
]

FORBIDDEN_PRINCIPALS: list[str] = []


# -- Phase transitions --------------------------------------------------------
# Who: subtype_contracts.py

ALLOWED_NEXT: list[str] = ["DESIGN", "EXECUTE", "DECIDE"]
REPAIR_TARGET: str = "DECIDE"
HAS_EVIDENCE_BASIS_REQUIRED: bool = False


# -- PhaseRecord required fields ----------------------------------------------
# Who: subtype_contracts.py (required_fields), phase_record.py (field defs)

REQUIRED_RECORD_FIELDS: list[str] = []


# -- Gate field specifications ------------------------------------------------
# Who: decide_gate.py, gate_rejection.py (SDG)

FIELD_SPECS: list[FieldSpec] = [
    FieldSpec(
        name="options",
        description="Fix direction options compared with pros and cons",
        required=True,
        semantic_check="multiple_options",
    ),
    FieldSpec(
        name="chosen",
        description="Name of the chosen option",
        required=True,
    ),
    FieldSpec(
        name="rationale",
        description="Why this option was chosen over alternatives",
        required=True,
        min_length=10,
    ),
]

# Convenience: name -> FieldSpec lookup
FIELD_SPEC_MAP: dict[str, FieldSpec] = {fs.name: fs for fs in FIELD_SPECS}

# Required field names (for ContractView.required_fields)
GATE_REQUIRED_FIELDS: list[str] = [fs.name for fs in FIELD_SPECS if fs.required]


# -- Gate rules ---------------------------------------------------------------
# Who: decide_gate.py (evaluate_decide)
# Each rule maps to a check function name, the field it evaluates, and the
# repair hint injected on failure.

GATE_RULES: list[GateRule] = [
    GateRule(
        name="option_comparison",
        field="options",
        repair_hint="List at least 2 options with pros and cons",
    ),
    GateRule(
        name="selection_justified",
        field="rationale",
        repair_hint="Explain why the chosen option is best",
    ),
    GateRule(
        name="chosen_matches_option",
        field="chosen",
        repair_hint="Your 'chosen' must match one of the listed options",
    ),
]

GATE_RULE_MAP: dict[str, GateRule] = {r.name: r for r in GATE_RULES}

# Default threshold for all rules
GATE_THRESHOLD: float = 0.5


# -- Prompt template sections -------------------------------------------------
# Who: phase_prompt.py

PROMPT_REQUIRED_SECTIONS: list[str] = [
    "OPTIONS",
    "CHOSEN",
    "RATIONALE",
]

PROMPT_GUIDANCE = (
    "Choose the best fix strategy based on your analysis.\n\n"
    "Rules:\n"
    "1. List at least 2 options with tradeoffs before choosing.\n"
    "2. Your selected option must reference a specific option by name.\n"
    "3. Do NOT start coding yet.\n"
)
# NOTE: Field descriptions are NOT listed here. They are rendered at
# runtime from the bundle schema by schema_field_guidance.render_schema_field_guidance().


# -- Structured output schema fields -----------------------------------------
# Who: phase_schemas.py (DECIDE schema)
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
    "options": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Option name/label."},
                "pros": {"type": "string", "description": "Advantages of this option."},
                "cons": {"type": "string", "description": "Disadvantages of this option."},
            },
            "required": ["name", "pros", "cons"],
        },
        "minItems": 2,
        "description": FIELD_SPEC_MAP["options"].description,
    },
    "chosen": {
        "type": "string",
        "description": FIELD_SPEC_MAP["chosen"].description,
    },
    "rationale": {
        "type": "string",
        "minLength": 10,
        "description": FIELD_SPEC_MAP["rationale"].description,
    },
}

SCHEMA_REQUIRED: list[str] = [
    "phase", "subtype", "principals", "options", "chosen", "rationale",
]
