"""
analysis_root_cause.py — Single source of truth for analysis.root_cause contract.

This file is the ONLY place where the analysis.root_cause cognition contract
is defined. All consumers (subtype_contracts, phase_prompt, analysis_gate,
phase_schemas, phase_record) derive from this file.

Contract declare once, loader wires everywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field


# ── Contract identity ────────────────────────────────────────────────────────

PHASE = "ANALYZE"
SUBTYPE = "analysis.root_cause"


# ── Principals ───────────────────────────────────────────────────────────────
# Who: subtype_contracts.py, phase_prompt.py (principal guidance)

REQUIRED_PRINCIPALS: list[str] = [
    "causal_grounding",
    "evidence_linkage",
]

EXPECTED_PRINCIPALS: list[str] = [
    "ontology_alignment",
    "phase_boundary_discipline",
    "alternative_hypothesis_check",
    "uncertainty_honesty",
    "invariant_capture",
]

FORBIDDEN_PRINCIPALS: list[str] = [
    "action_grounding",
    "minimal_change",
]


# ── Phase transitions ────────────────────────────────────────────────────────
# Who: subtype_contracts.py

ALLOWED_NEXT: list[str] = ["DECIDE", "ANALYZE", "OBSERVE"]
REPAIR_TARGET: str = "OBSERVE"
HAS_EVIDENCE_BASIS_REQUIRED: bool = True


# ── PhaseRecord required fields ──────────────────────────────────────────────
# Who: subtype_contracts.py (required_fields), phase_record.py (field defs)
# Note: evidence_refs removed from required_fields (P16 fix) — checked via
# has_evidence_basis in principal_gate instead.

REQUIRED_RECORD_FIELDS: list[str] = []


# ── Gate field specifications ────────────────────────────────────────────────
# Who: analysis_gate.py (_ANALYZE_CONTRACT), gate_rejection.py (SDG)

@dataclass
class FieldSpec:
    """Specification for a single field in the contract."""
    name: str
    description: str
    required: bool
    min_length: int | None = None
    semantic_check: str | None = None


FIELD_SPECS: list[FieldSpec] = [
    FieldSpec(
        name="root_cause",
        description="Identified root cause with specific code reference (file/function/line)",
        required=True,
        min_length=10,
        semantic_check="grounded_in_code",
    ),
    FieldSpec(
        name="causal_chain",
        description="Causal chain: test failure -> condition -> code -> why it fails",
        required=True,
        min_length=20,
        semantic_check="connects_test_to_code",
    ),
    FieldSpec(
        name="evidence_refs",
        description="Code and test references supporting the analysis",
        required=True,
    ),
    FieldSpec(
        name="alternatives_considered",
        description="At least 2 hypotheses with rejection reasoning for non-chosen",
        required=False,
        semantic_check="multiple_distinct_hypotheses",
    ),
    FieldSpec(
        name="invariant_capture",
        description="Structural invariant: what delimiter/boundary must NOT appear and why",
        required=False,
        semantic_check="invariant_identified",
    ),
]

# Convenience: name -> FieldSpec lookup
FIELD_SPEC_MAP: dict[str, FieldSpec] = {fs.name: fs for fs in FIELD_SPECS}

# Required field names (for ContractView.required_fields)
GATE_REQUIRED_FIELDS: list[str] = [fs.name for fs in FIELD_SPECS if fs.required]


# ── Gate rules ───────────────────────────────────────────────────────────────
# Who: analysis_gate.py (evaluate_analysis)
# Each rule maps to a check function name, the field it evaluates, and the
# repair hint injected on failure.

@dataclass
class GateRule:
    """One evaluation rule in the analysis gate."""
    name: str
    field: str           # PhaseRecord field this rule evaluates
    repair_hint: str     # hint injected into SDG repair on failure
    threshold: float = 0.5


GATE_RULES: list[GateRule] = [
    GateRule(
        name="code_grounding",
        field="root_cause",
        repair_hint="Point to exact code location (file:line or function name) causing the issue",
    ),
    GateRule(
        name="alternative_hypothesis",
        field="alternatives_considered",
        repair_hint="Consider at least 2 hypotheses and explain why non-chosen ones were rejected",
    ),
    GateRule(
        name="causal_chain",
        field="causal_chain",
        repair_hint="Explain step-by-step: test failure -> condition -> code -> why it fails",
    ),
    GateRule(
        name="invariant_capture",
        field="root_cause",
        repair_hint=(
            "Identify the behavioral constraint your fix must preserve: "
            "what property, contract, or boundary must remain unchanged? "
            "What invalid behavior must still be rejected? "
            "What valid behavior must remain accepted?"
        ),
    ),
]

GATE_RULE_MAP: dict[str, GateRule] = {r.name: r for r in GATE_RULES}

# Default threshold for all rules
GATE_THRESHOLD: float = 0.5


# ── Prompt template sections ────────────────────────────────────────────────
# Who: phase_prompt.py (_ANALYZE_GUIDANCE)
# Defines the required output structure the agent must produce.

PROMPT_REQUIRED_SECTIONS: list[str] = [
    "ROOT_CAUSE",
    "EVIDENCE",
    "CAUSAL_CHAIN",
    "ALTERNATIVES",
    "UNCERTAINTY",
]

PROMPT_GUIDANCE = (
    "Identify the root cause with causal evidence. Do NOT write any fix yet.\n\n"
    "You MUST produce your analysis in this exact format:\n\n"
    "PHASE: analyze\n"
    f"PRINCIPALS: {', '.join(REQUIRED_PRINCIPALS)}\n\n"
    "ROOT_CAUSE:\n<one specific root cause — not vague>\n\n"
    "EVIDENCE:\n- file/path.py:line - what this shows\n- file/path.py:line - what this shows\n\n"
    "CAUSAL_CHAIN:\n<step-by-step reasoning from evidence to root cause>\n\n"
    "ALTERNATIVES:\n- <other hypothesis> — why ruled out\n\n"
    "UNCERTAINTY:\n<what you are NOT sure about — be honest>\n\n"
    "ROOT_CAUSE is MANDATORY. If you do not produce a ROOT_CAUSE: field with a specific "
    "file:line location, this analysis step is incomplete and you will be redirected back to ANALYZE.\n\n"
    "Rules: ROOT_CAUSE must be specific. EVIDENCE must reference real files. "
    "CAUSAL_CHAIN must connect evidence -> root cause. Do NOT propose fixes here.\n\n"
    "Required output structure (will be checked before advancing to EXECUTE):\n"
    "- ROOT_CAUSE: one sentence, grounded in specific file/function\n"
    "- CAUSAL_CHAIN: step-by-step from failing test -> condition -> code -> bug\n"
    "- ALTERNATIVES: at least one alternative hypothesis + why rejected\n\n"
    "If any field is missing, you will be returned to ANALYZE with specific feedback.\n"
    "Fix only the missing fields. Do not rewrite fields already present.\n"
)


# ── Structured output schema fields ─────────────────────────────────────────
# Who: phase_schemas.py (ANALYZE schema)
# JSON schema properties for structured output tool call.

SCHEMA_PROPERTIES: dict = {
    "phase": {
        "type": "string",
        "enum": [PHASE.lower()],
        "description": "Current reasoning phase.",
    },
    "subtype": {
        "type": "string",
        "enum": [SUBTYPE],
        "description": f"The subtype of output: '{SUBTYPE}'.",
    },
    "root_cause": {
        "type": "string",
        "minLength": 10,
        "description": FIELD_SPEC_MAP["root_cause"].description,
    },
    "causal_chain": {
        "type": "string",
        "minLength": 20,
        "description": FIELD_SPEC_MAP["causal_chain"].description,
    },
    "evidence": {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 1,
        "description": "List of evidence references (file:line, test name, etc.).",
    },
    "alternatives_considered": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "hypothesis": {"type": "string"},
                "why_rejected": {"type": "string"},
            },
            "required": ["hypothesis", "why_rejected"],
        },
        "minItems": 1,
        "description": FIELD_SPEC_MAP["alternatives_considered"].description,
    },
    "uncertainty": {
        "type": "string",
        "description": "What you are NOT sure about — be honest.",
    },
    "principals": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Principal atoms declared for this phase.",
    },
}

SCHEMA_REQUIRED: list[str] = [
    "phase", "subtype", "root_cause", "causal_chain",
    "evidence", "alternatives_considered", "principals",
]
