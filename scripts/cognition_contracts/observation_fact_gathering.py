"""
observation_fact_gathering.py — Single source of truth for observation.fact_gathering contract.

This file is the ONLY place where the observation.fact_gathering cognition contract
is defined. All consumers (subtype_contracts, phase_prompt, observation gates,
phase_schemas, phase_record) derive from this file.

Contract declare once, loader wires everywhere.
"""

from __future__ import annotations

from cognition_contracts._base import FieldSpec, GateRule


# -- Contract identity --------------------------------------------------------

PHASE = "OBSERVE"
SUBTYPE = "observation.fact_gathering"


# -- Principals ---------------------------------------------------------------
# Who: subtype_contracts.py, phase_prompt.py (principal guidance)

REQUIRED_PRINCIPALS: list[str] = []

EXPECTED_PRINCIPALS: list[str] = [
    "ontology_alignment",
    "phase_boundary_discipline",
    "evidence_completeness",
]

FORBIDDEN_PRINCIPALS: list[str] = [
    "action_grounding",
    "minimal_change",
]


# -- Phase transitions --------------------------------------------------------
# Who: subtype_contracts.py

ALLOWED_NEXT: list[str] = ["ANALYZE", "OBSERVE"]
REPAIR_TARGET: str = "OBSERVE"
HAS_EVIDENCE_BASIS_REQUIRED: bool = True


# -- PhaseRecord required fields ----------------------------------------------
# Who: subtype_contracts.py (required_fields), phase_record.py (field defs)

REQUIRED_RECORD_FIELDS: list[str] = []


# -- Gate field specifications ------------------------------------------------
# Who: observation gate (if any), gate_rejection.py (SDG)

FIELD_SPECS: list[FieldSpec] = [
    FieldSpec(
        name="observations",
        description="List of factual observations gathered from the codebase",
        required=True,
        min_length=1,
    ),
    FieldSpec(
        name="evidence_refs",
        description="References to specific code locations supporting observations",
        required=True,
        min_length=1,
    ),
    FieldSpec(
        name="source_coverage",
        description="Assessment of how thoroughly the relevant code was examined",
        required=False,
    ),
    FieldSpec(
        name="missing_evidence",
        description="Known gaps in evidence that could affect analysis",
        required=False,
    ),
]

# Convenience: name -> FieldSpec lookup
FIELD_SPEC_MAP: dict[str, FieldSpec] = {fs.name: fs for fs in FIELD_SPECS}

# Required field names (for ContractView.required_fields)
GATE_REQUIRED_FIELDS: list[str] = [fs.name for fs in FIELD_SPECS if fs.required]


# -- Gate rules ---------------------------------------------------------------
# Who: observation has no semantic gate rules (fact gathering only)

GATE_RULES: list[GateRule] = []

GATE_RULE_MAP: dict[str, GateRule] = {}

# Default threshold for all rules
GATE_THRESHOLD: float = 0.5


# -- Prompt template sections -------------------------------------------------
# Who: phase_prompt.py

PROMPT_REQUIRED_SECTIONS: list[str] = [
    "OBSERVATIONS",
    "EVIDENCE",
]

PROMPT_GUIDANCE = (
    "Gather facts from the codebase. Do NOT hypothesize or propose fixes yet.\n\n"
    "Rules:\n"
    "1. Read the relevant code and document what you observe.\n"
    "2. Reference specific files and line numbers.\n"
    "3. Do NOT jump to conclusions about root cause.\n"
)


# -- Structured output schema fields -----------------------------------------
# Who: phase_schemas.py (OBSERVE schema)
# JSON schema properties for structured output tool call.

SCHEMA_PROPERTIES: dict = {
    "phase": {
        "type": "string",
        "enum": ["OBSERVE"],
        "description": "Current reasoning phase.",
    },
    "subtype": {
        "type": "string",
        "enum": [SUBTYPE],
        "description": f"The subtype of output: '{SUBTYPE}'.",
    },
    "observations": {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 1,
        "description": FIELD_SPEC_MAP["observations"].description,
    },
    "evidence_refs": {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 1,
        "description": FIELD_SPEC_MAP["evidence_refs"].description,
    },
    "source_coverage": {
        "type": "string",
        "description": FIELD_SPEC_MAP["source_coverage"].description,
    },
    "missing_evidence": {
        "type": "string",
        "description": FIELD_SPEC_MAP["missing_evidence"].description,
    },
    "principals": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Principal atoms declared for this phase.",
    },
}

SCHEMA_REQUIRED: list[str] = [
    "phase", "subtype", "principals", "evidence_refs", "observations",
]

# Derived convenience attributes
REQUIRED_FIELDS: list[str] = [fs.name for fs in FIELD_SPECS if fs.required]
