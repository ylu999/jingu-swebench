"""
analysis_root_cause.py — Single source of truth for analysis.root_cause contract.

This file is the ONLY place where the analysis.root_cause cognition contract
is defined. All consumers (subtype_contracts, phase_prompt, analysis_gate,
phase_schemas, phase_record) derive from this file.

Contract declare once, loader wires everywhere.
"""

from __future__ import annotations

from cognition_contracts._base import FieldSpec, GateRule


# ── Contract identity ────────────────────────────────────────────────────────

PHASE = "ANALYZE"
SUBTYPE = "analysis.root_cause"


# ── Repair strategy taxonomy ──────────────────────────────────────────────────
# Who: SCHEMA_PROPERTIES (enum), jingu_agent.py (dual-cause strategy ban)
# Agent declares this in structured output; system reads it as declared contract.

REPAIR_STRATEGY_TYPES: list[str] = [
    "REGEX_FIX",
    "PARSER_REWRITE",
    "DATAFLOW_FIX",
    "STATE_COPY_FIX",
    "INVARIANT_FIX",
    "MISSING_SECONDARY_FIX",
    "API_CONTRACT_FIX",
]


# ── Principals ───────────────────────────────────────────────────────────────
# Who: subtype_contracts.py, phase_prompt.py (principal guidance)

REQUIRED_PRINCIPALS: list[str] = [
    "causal_grounding",
    "evidence_linkage",
    "alternative_hypothesis_check",
]

EXPECTED_PRINCIPALS: list[str] = [
    "ontology_alignment",
    "phase_boundary_discipline",
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
        name="alternative_hypotheses",
        description="At least 1 alternative hypothesis with rejection reasoning — prevents premature trajectory collapse",
        required=True,
        semantic_check="multiple_distinct_hypotheses",
    ),
    FieldSpec(
        name="invariant_capture",
        description="Structural invariant: what delimiter/boundary must NOT appear and why",
        required=False,
        semantic_check="invariant_identified",
    ),
    FieldSpec(
        name="mechanism_path",
        description="Code path from symptom to mechanism (e.g. caller -> resolver -> matcher)",
        required=False,
        semantic_check="traces_to_mechanism",
    ),
    FieldSpec(
        name="rejected_nearby_files",
        description="Files considered but rejected as root cause, with reasons",
        required=False,
        semantic_check="justifies_scope_choice",
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

GATE_RULES: list[GateRule] = [
    GateRule(
        name="code_grounding",
        field="root_cause",
        repair_hint="Point to exact code location (file:line or function name) causing the issue",
    ),
    GateRule(
        name="alternative_hypothesis",
        field="alternative_hypotheses",
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
    GateRule(
        name="scope_justification",
        field="rejected_nearby_files",
        repair_hint=(
            "You identified a single root-cause file but did not justify why it is "
            "the mechanism, not just the symptom. Provide: (1) mechanism_path tracing "
            "from symptom to actual mechanism, (2) at least 1 rejected_nearby_files "
            "entry explaining why a nearby file (caller/wrapper) is NOT the root cause."
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
    "Investigate the code, then call submit_phase_record with your findings.\n\n"
    "Rules: Do NOT propose fixes here. Focus on understanding why the test fails.\n\n"
    "If any required field is missing or empty, you will be returned to ANALYZE.\n\n"
    "You MUST explicitly declare your repair strategy type. Choose exactly one:\n"
    "  REPAIR_STRATEGY_TYPE: REGEX_FIX | PARSER_REWRITE | DATAFLOW_FIX | "
    "STATE_COPY_FIX | INVARIANT_FIX | MISSING_SECONDARY_FIX | API_CONTRACT_FIX\n"
    "This field is REQUIRED. If missing, your analysis will be REJECTED.\n\n"
    "You MUST declare root_cause_location_files: the file(s) where the bug lives.\n"
    "Downstream DECIDE/EXECUTE phases are CONSTRAINED to this file scope.\n"
    "If the patch later targets different files, it will be REJECTED unless\n"
    "you update the analysis with new evidence justifying the scope change.\n\n"
    "ROOT-CAUSE SCOPE JUSTIFICATION (P3):\n"
    "If you identify a SINGLE root cause file, you MUST also provide:\n"
    "  - mechanism_path: the code path from symptom to mechanism\n"
    "    (e.g. ['translate_url()', 'reverse()', 'RegexPattern.match()'])\n"
    "  - rejected_nearby_files: at least 1 file you considered but rejected,\n"
    "    with reason (e.g. 'caller/symptom layer only, delegates to X')\n"
    "This proves you traced past the symptom layer to the actual mechanism.\n"
    "If missing, your analysis will be returned for deeper investigation.\n"
)
# NOTE: Field descriptions are NO LONGER listed here. They are rendered at
# runtime from the bundle schema by schema_field_guidance.render_schema_field_guidance().
# phase_prompt.build_phase_prefix() calls the renderer to inject field guidance.
# This eliminates the second copy of field descriptions that previously drifted.


# ── Structured output schema fields ─────────────────────────────────────────
# Who: phase_schemas.py (ANALYZE schema)
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
    "evidence_refs": {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 1,
        "description": "List of evidence references (file:line, test name, etc.).",
    },
    "alternative_hypotheses": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "hypothesis": {"type": "string"},
                "ruled_out_reason": {"type": "string"},
            },
            "required": ["hypothesis", "ruled_out_reason"],
        },
        "minItems": 1,
        "description": FIELD_SPEC_MAP["alternative_hypotheses"].description,
    },
    "invariant_capture": {
        "type": "object",
        "properties": {
            "identified_invariants": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Behavioral invariants the fix must preserve.",
            },
            "risk_if_violated": {
                "type": "string",
                "description": "What breaks if these invariants are not preserved.",
            },
        },
        "required": ["identified_invariants", "risk_if_violated"],
        "description": (
            "Behavioral constraints the fix must preserve. "
            "What must remain true after the fix? "
            "What invalid behavior must still be rejected? "
            "What valid behavior must remain accepted?"
        ),
    },
    "root_cause_location_files": {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 1,
        "description": (
            "Files where the root cause lives (e.g. ['django/urls/resolvers.py']). "
            "Downstream phases (DECIDE/EXECUTE) must keep patches consistent with "
            "this scope. If your patch targets different files, you must justify "
            "the deviation with new evidence."
        ),
    },
    "root_cause_scope_summary": {
        "type": "string",
        "minLength": 10,
        "description": (
            "One-sentence summary of where the bug lives and what layer it belongs to. "
            "Used to ground downstream DECIDE/EXECUTE phases."
        ),
    },
    "mechanism_path": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "The code path through which the error occurs, from symptom to mechanism. "
            "e.g. ['translate_url()', 'reverse()', 'RegexPattern.match()']. "
            "Must trace from the observable symptom down to the actual mechanism "
            "that produces incorrect behavior."
        ),
    },
    "rejected_nearby_files": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "file": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["file", "reason"],
        },
        "description": (
            "Files considered but rejected as root cause location. "
            "For each: the file path and why it is NOT the root cause "
            "(e.g. 'caller/symptom layer only', 'wrapper that delegates to X'). "
            "At least 1 required when root_cause_location_files has a single file."
        ),
    },
    "repair_strategy_type": {
        "type": "string",
        "enum": REPAIR_STRATEGY_TYPES,
        "description": (
            "The repair strategy category for the identified root cause. "
            "Choose the one that best describes how the fix should work: "
            "REGEX_FIX (adjust regex/pattern logic), "
            "PARSER_REWRITE (restructure parsing/extraction), "
            "DATAFLOW_FIX (fix value propagation between components), "
            "STATE_COPY_FIX (ensure internal state properly copied/preserved), "
            "INVARIANT_FIX (enforce constraints/boundary checks), "
            "MISSING_SECONDARY_FIX (primary fix incomplete, second change needed), "
            "API_CONTRACT_FIX (fix function return value/signature usage)."
        ),
    },
    "principals": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Principal atoms declared for this phase.",
    },
}

SCHEMA_REQUIRED: list[str] = [
    "phase", "subtype", "root_cause", "causal_chain",
    "evidence_refs", "alternative_hypotheses", "repair_strategy_type",
    "root_cause_location_files", "principals",
]
