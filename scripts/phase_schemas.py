"""
phase_schemas.py — JSON Schema definitions for structured LLM output per phase.

When STRUCTURED_OUTPUT_ENABLED=true, these schemas are sent to the Bedrock API
via the tool use / json_schema mechanism, guaranteeing the LLM response conforms
to the schema. This eliminates regex extraction (declaration_extractor.py) for
field presence and format — the gate can focus on semantic checks only.

Bedrock structured output:
  Bedrock Converse API supports structured output via toolConfig with a single
  tool whose inputSchema is the desired JSON Schema. The model is forced to call
  the tool, producing schema-valid JSON as the tool input. This is the Bedrock
  equivalent of Anthropic direct API's tool_use with forced tool_choice.

  Alternative: additionalModelRequestFields with output_config for models that
  support native json_schema mode. As of 2026-04, the tool-use approach is more
  widely supported across Bedrock model versions.

Feature flag: STRUCTURED_OUTPUT_ENABLED (env var, default false)
"""

from __future__ import annotations

from typing import Any


# ── Cognition-aligned PhaseRecord schemas (p222) ──────────────────────────

# Base schema for PhaseRecord structured output — all phases share this shape.
# Per-phase schemas extend this with phase-specific constraints.
PHASE_RECORD_BASE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "phase": {
            "type": "string",
            "enum": ["UNDERSTAND", "OBSERVE", "ANALYZE", "DECIDE", "DESIGN", "EXECUTE", "JUDGE"],
            "description": "The current reasoning phase.",
        },
        "subtype": {
            "type": "string",
            "description": "The subtype of output (e.g. 'analysis.root_cause', 'execution.code_patch').",
        },
        "principals": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Declared cognitive principals for this phase.",
        },
        "claims": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Explicit claims made in this phase.",
        },
        "evidence_refs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Evidence references (file:line or test names) grounding claims.",
        },
        "from_steps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Step identifiers this record derives from.",
        },
        "content": {
            "type": "string",
            "description": "The main reasoning content for this phase.",
        },
    },
    "required": ["phase", "subtype", "principals", "content"],
}

# ── All phase schemas derived from cognition_contracts (single source of truth) ──
from cognition_contracts import observation_fact_gathering as _ofg
from cognition_contracts import analysis_root_cause as _arc
from cognition_contracts import decision_fix_direction as _dfd
from cognition_contracts import design_solution_shape as _dss
from cognition_contracts import execution_code_patch as _ecp
from cognition_contracts import judge_verification as _jv

# OBSERVE phase: evidence gathering
OBSERVE_RECORD_SCHEMA: dict[str, Any] = {
    **PHASE_RECORD_BASE_SCHEMA,
    "properties": {
        **PHASE_RECORD_BASE_SCHEMA["properties"],
        **_ofg.SCHEMA_PROPERTIES,
    },
    "required": list(_ofg.SCHEMA_REQUIRED) + ["content"],
}

# ANALYZE phase: root cause with evidence
ANALYZE_RECORD_SCHEMA: dict[str, Any] = {
    **PHASE_RECORD_BASE_SCHEMA,
    "properties": {
        **PHASE_RECORD_BASE_SCHEMA["properties"],
        **_arc.SCHEMA_PROPERTIES,
    },
    "required": list(_arc.SCHEMA_REQUIRED) + ["content"],
}

# DECIDE phase: fix direction selection
DECIDE_RECORD_SCHEMA: dict[str, Any] = {
    **PHASE_RECORD_BASE_SCHEMA,
    "properties": {
        **PHASE_RECORD_BASE_SCHEMA["properties"],
        **_dfd.SCHEMA_PROPERTIES,
    },
    "required": list(_dfd.SCHEMA_REQUIRED) + ["content"],
}

# DESIGN phase: solution shape
DESIGN_RECORD_SCHEMA: dict[str, Any] = {
    **PHASE_RECORD_BASE_SCHEMA,
    "properties": {
        **PHASE_RECORD_BASE_SCHEMA["properties"],
        **_dss.SCHEMA_PROPERTIES,
    },
    "required": list(_dss.SCHEMA_REQUIRED) + ["content"],
}

# EXECUTE phase: code patch
EXECUTE_RECORD_SCHEMA: dict[str, Any] = {
    **PHASE_RECORD_BASE_SCHEMA,
    "properties": {
        **PHASE_RECORD_BASE_SCHEMA["properties"],
        **_ecp.SCHEMA_PROPERTIES,
    },
    "required": list(_ecp.SCHEMA_REQUIRED) + ["content"],
}

# JUDGE phase: verification
JUDGE_RECORD_SCHEMA: dict[str, Any] = {
    **PHASE_RECORD_BASE_SCHEMA,
    "properties": {
        **PHASE_RECORD_BASE_SCHEMA["properties"],
        **_jv.SCHEMA_PROPERTIES,
    },
    "required": list(_jv.SCHEMA_REQUIRED) + ["content"],
}

# UNDERSTAND phase: uses base schema (no cognition contract yet)
UNDERSTAND_RECORD_SCHEMA: dict[str, Any] = {
    **PHASE_RECORD_BASE_SCHEMA,
}

# Mapping: phase -> cognition-aligned record schema
PHASE_RECORD_SCHEMAS: dict[str, dict[str, Any]] = {
    "UNDERSTAND": UNDERSTAND_RECORD_SCHEMA,
    "OBSERVE": OBSERVE_RECORD_SCHEMA,
    "ANALYZE": ANALYZE_RECORD_SCHEMA,
    "DECIDE": DECIDE_RECORD_SCHEMA,
    "DESIGN": DESIGN_RECORD_SCHEMA,
    "EXECUTE": EXECUTE_RECORD_SCHEMA,
    "JUDGE": JUDGE_RECORD_SCHEMA,
}


def get_phase_record_schema(phase: str) -> dict[str, Any] | None:
    """Return the cognition-aligned PhaseRecord schema for a phase.

    These schemas align with PhaseRecord dataclass fields and are suitable
    for structured output enforcement via Claude API or Bedrock tool use.

    Args:
        phase: Phase name (case-insensitive).

    Returns:
        JSON Schema dict or None if no schema for this phase.
    """
    return PHASE_RECORD_SCHEMAS.get(phase.upper())


