"""
execution_code_patch.py — Single source of truth for execution.code_patch contract.

This file is the ONLY place where the execution.code_patch cognition contract
is defined. All consumers (subtype_contracts, phase_prompt, execute_gate,
phase_schemas, phase_record) derive from this file.

Contract declare once, loader wires everywhere.
"""

from __future__ import annotations

from cognition_contracts._base import FieldSpec, GateRule


# -- Contract identity --------------------------------------------------------

PHASE = "EXECUTE"
SUBTYPE = "execution.code_patch"


# -- Principals ---------------------------------------------------------------
# Who: subtype_contracts.py, phase_prompt.py (principal guidance)

REQUIRED_PRINCIPALS: list[str] = [
    "minimal_change",
    "action_grounding",
]

EXPECTED_PRINCIPALS: list[str] = []

FORBIDDEN_PRINCIPALS: list[str] = []


# -- Phase transitions --------------------------------------------------------
# Who: subtype_contracts.py

ALLOWED_NEXT: list[str] = ["JUDGE", "EXECUTE"]
REPAIR_TARGET: str = "EXECUTE"
HAS_EVIDENCE_BASIS_REQUIRED: bool = False


# -- PhaseRecord required fields ----------------------------------------------
# Who: subtype_contracts.py (required_fields), phase_record.py (field defs)

REQUIRED_RECORD_FIELDS: list[str] = []


# -- Gate field specifications ------------------------------------------------
# Who: execute_gate.py, gate_rejection.py (SDG)

FIELD_SPECS: list[FieldSpec] = [
    FieldSpec(
        name="patch_description",
        description="What the patch does and why",
        required=True,
        min_length=10,
    ),
    FieldSpec(
        name="files_modified",
        description="Files actually modified by the patch",
        required=False,
    ),
]

# Convenience: name -> FieldSpec lookup
FIELD_SPEC_MAP: dict[str, FieldSpec] = {fs.name: fs for fs in FIELD_SPECS}

# Required field names (for ContractView.required_fields)
GATE_REQUIRED_FIELDS: list[str] = [fs.name for fs in FIELD_SPECS if fs.required]


# -- Gate rules ---------------------------------------------------------------
# Who: execute_gate.py (evaluate_execute)
# Each rule maps to a check function name, the field it evaluates, and the
# repair hint injected on failure.

GATE_RULES: list[GateRule] = [
    GateRule(
        name="patch_described",
        field="patch_description",
        repair_hint="Describe what your patch does and why it fixes the root cause",
    ),
    GateRule(
        name="causal_grounding",
        field="patch_description",
        repair_hint="Reference the root cause from ANALYZE in your patch description",
    ),
    GateRule(
        name="scope_bounded",
        field="files_modified",
        repair_hint="List the files your patch modifies in files_modified",
    ),
]

GATE_RULE_MAP: dict[str, GateRule] = {r.name: r for r in GATE_RULES}

# Default threshold for all rules
GATE_THRESHOLD: float = 0.5


# -- Prompt template sections -------------------------------------------------
# Who: phase_prompt.py

PROMPT_REQUIRED_SECTIONS: list[str] = []

PROMPT_GUIDANCE = (
    "ACTION REQUIRED NOW. Write the patch. Follow the root cause from ANALYZE.\n\n"
    "Rules:\n"
    "1. Write the minimal patch to the location identified in ANALYZE.\n"
    "2. Do NOT re-analyze. Do NOT re-read files. You already know the root cause.\n"
    "3. If no code change is produced this step, the step counts as FAILED.\n"
    "4. Before editing, grep for ALL callers/importers of any function you change.\n"
    "5. Do NOT add backward-compatibility shims unless the issue explicitly requires it.\n"
)


# -- Structured output schema fields -----------------------------------------
# Who: phase_schemas.py (EXECUTE schema)
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
    "patch_description": {
        "type": "string",
        "minLength": 10,
        "description": FIELD_SPEC_MAP["patch_description"].description,
    },
    "files_modified": {
        "type": "array",
        "items": {"type": "string"},
        "description": FIELD_SPEC_MAP["files_modified"].description,
    },
}

SCHEMA_REQUIRED: list[str] = [
    "phase", "subtype", "principals", "patch_description",
]
