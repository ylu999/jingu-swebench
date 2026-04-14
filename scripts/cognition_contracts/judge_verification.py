"""
judge_verification.py — Single source of truth for judge.verification contract.

This file is the ONLY place where the judge.verification cognition contract
is defined. All consumers (subtype_contracts, phase_prompt, judge_gate,
phase_schemas, phase_record) derive from this file.

Contract declare once, loader wires everywhere.
"""

from __future__ import annotations

from cognition_contracts._base import FieldSpec, GateRule


# -- Contract identity --------------------------------------------------------

PHASE = "JUDGE"
SUBTYPE = "judge.verification"


# -- Principals ---------------------------------------------------------------
# Who: subtype_contracts.py, phase_prompt.py (principal guidance)

REQUIRED_PRINCIPALS: list[str] = [
    "result_verification",
]

EXPECTED_PRINCIPALS: list[str] = [
    "uncertainty_honesty",
]

FORBIDDEN_PRINCIPALS: list[str] = []


# -- Phase transitions --------------------------------------------------------
# Who: subtype_contracts.py

ALLOWED_NEXT: list[str] = ["EXECUTE", "ANALYZE", "JUDGE"]
REPAIR_TARGET: str = "JUDGE"
HAS_EVIDENCE_BASIS_REQUIRED: bool = True


# -- PhaseRecord required fields ----------------------------------------------
# Who: subtype_contracts.py (required_fields), phase_record.py (field defs)

REQUIRED_RECORD_FIELDS: list[str] = []


# -- Gate field specifications ------------------------------------------------
# Who: judge_gate.py (evaluate_judge), gate_rejection.py (SDG)

FIELD_SPECS: list[FieldSpec] = [
    FieldSpec(
        name="test_results",
        description="Test execution results with passed boolean",
        required=True,
        semantic_check="has_passed_field",
    ),
    FieldSpec(
        name="success_criteria_met",
        description="Each success criterion and whether it was met",
        required=True,
    ),
    FieldSpec(
        name="residual_risks",
        description="Known remaining risks after the fix",
        required=False,
    ),
]

# Convenience: name -> FieldSpec lookup
FIELD_SPEC_MAP: dict[str, FieldSpec] = {fs.name: fs for fs in FIELD_SPECS}

# Required field names (for ContractView.required_fields)
GATE_REQUIRED_FIELDS: list[str] = [fs.name for fs in FIELD_SPECS if fs.required]


# -- Gate rules ---------------------------------------------------------------
# Who: judge_gate.py (evaluate_judge)
# Each rule maps to a check function name, the field it evaluates, and the
# repair hint injected on failure.

GATE_RULES: list[GateRule] = [
    GateRule(
        name="test_results_present",
        field="test_results",
        repair_hint="Run tests and report results with a passed boolean field",
    ),
    GateRule(
        name="criteria_verified",
        field="success_criteria_met",
        repair_hint="List success criteria and whether each was met",
    ),
    GateRule(
        name="risks_acknowledged",
        field="residual_risks",
        repair_hint="Acknowledge residual risks or state none",
    ),
    GateRule(
        name="verdict_consistent",
        field="test_results",
        repair_hint="Verdict must be consistent with test results",
    ),
]

GATE_RULE_MAP: dict[str, GateRule] = {r.name: r for r in GATE_RULES}

# Default threshold for all rules
GATE_THRESHOLD: float = 0.5


# -- Prompt template sections -------------------------------------------------
# Who: phase_prompt.py (_JUDGE_GUIDANCE)

PROMPT_REQUIRED_SECTIONS: list[str] = [
    "TEST_RESULTS",
    "SUCCESS_CRITERIA",
    "RESIDUAL_RISKS",
]

PROMPT_GUIDANCE = (
    "Verify your fix. Run tests. Check that invariants are preserved.\n\n"
    "Rules:\n"
    "1. You MUST run at least the failing test.\n"
    "2. Your verdict must be based on test results, not on reading code.\n"
    "3. If uncertain, say so.\n"
    "4. Check scope_completeness: were ALL callers of modified functions checked?\n"
)


# -- Structured output schema fields -----------------------------------------
# Who: phase_schemas.py (JUDGE schema)
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
    "test_results": {
        "type": "object",
        "properties": {
            "passed": {
                "type": "boolean",
                "description": "Whether all relevant tests passed.",
            },
            "total": {
                "type": "integer",
                "description": "Total number of tests run.",
            },
            "failed": {
                "type": "integer",
                "description": "Number of failed tests.",
            },
            "details": {
                "type": "string",
                "description": "Additional details about test execution.",
            },
        },
        "required": ["passed"],
        "description": FIELD_SPEC_MAP["test_results"].description,
    },
    "success_criteria_met": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "criterion": {"type": "string"},
                "met": {"type": "boolean"},
            },
            "required": ["criterion", "met"],
        },
        "minItems": 1,
        "description": FIELD_SPEC_MAP["success_criteria_met"].description,
    },
    "residual_risks": {
        "type": "array",
        "items": {"type": "string"},
        "description": FIELD_SPEC_MAP["residual_risks"].description,
    },
}

SCHEMA_REQUIRED: list[str] = [
    "phase", "subtype", "principals", "test_results", "success_criteria_met",
]

# Derived convenience attributes
REQUIRED_FIELDS: list[str] = [fs.name for fs in FIELD_SPECS if fs.required]
