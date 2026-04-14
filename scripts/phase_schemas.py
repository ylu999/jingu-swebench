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

# OBSERVE phase: evidence gathering
OBSERVE_RECORD_SCHEMA: dict[str, Any] = {
    **PHASE_RECORD_BASE_SCHEMA,
    "properties": {
        **PHASE_RECORD_BASE_SCHEMA["properties"],
        "phase": {
            "type": "string",
            "enum": ["OBSERVE"],
            "description": "The current reasoning phase.",
        },
        "subtype": {
            "type": "string",
            "enum": ["observation.fact_gathering"],
            "description": "Observation subtype.",
        },
    },
    "required": ["phase", "subtype", "principals", "evidence_refs", "content"],
}

# ANALYZE phase: root cause with evidence
# Derived from cognition_contracts/analysis_root_cause.py (single source of truth).
from cognition_contracts import analysis_root_cause as _arc
ANALYZE_RECORD_SCHEMA: dict[str, Any] = {
    **PHASE_RECORD_BASE_SCHEMA,
    "properties": {
        **PHASE_RECORD_BASE_SCHEMA["properties"],
        **_arc.SCHEMA_PROPERTIES,
    },
    "required": list(_arc.SCHEMA_REQUIRED) + ["content"],
}

# DECIDE phase: fix direction selection (with prediction fields for decision quality v1)
DECIDE_RECORD_SCHEMA: dict[str, Any] = {
    **PHASE_RECORD_BASE_SCHEMA,
    "properties": {
        **PHASE_RECORD_BASE_SCHEMA["properties"],
        "phase": {
            "type": "string",
            "enum": ["DECIDE"],
            "description": "The current reasoning phase.",
        },
        "subtype": {
            "type": "string",
            "enum": ["decision.fix_direction"],
            "description": "Decision subtype.",
        },
        "expected_tests_to_pass": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Test names you predict will pass after your fix (max 5).",
            "maxItems": 5,
        },
        "expected_files_to_change": {
            "type": "array",
            "items": {"type": "string"},
            "description": "File paths your fix will modify.",
        },
        "testable_hypothesis": {
            "type": "string",
            "description": "If we do X, then tests Y will pass because Z.",
        },
        "risk_level": {
            "type": "string",
            "enum": ["low", "medium", "high"],
            "description": "Risk of regression: low=isolated change, medium=touches shared code, high=modifies core invariants.",
        },
    },
    "required": ["phase", "subtype", "principals", "content", "testable_hypothesis"],
}

# DESIGN phase: solution shape
DESIGN_RECORD_SCHEMA: dict[str, Any] = {
    **PHASE_RECORD_BASE_SCHEMA,
    "properties": {
        **PHASE_RECORD_BASE_SCHEMA["properties"],
        "phase": {
            "type": "string",
            "enum": ["DESIGN"],
            "description": "The current reasoning phase.",
        },
        "subtype": {
            "type": "string",
            "enum": ["design.solution_shape"],
            "description": "Design subtype.",
        },
    },
    "required": ["phase", "subtype", "principals", "content"],
}

# EXECUTE phase: code patch with plan
EXECUTE_RECORD_SCHEMA: dict[str, Any] = {
    **PHASE_RECORD_BASE_SCHEMA,
    "properties": {
        **PHASE_RECORD_BASE_SCHEMA["properties"],
        "phase": {
            "type": "string",
            "enum": ["EXECUTE"],
            "description": "The current reasoning phase.",
        },
        "subtype": {
            "type": "string",
            "enum": ["execution.code_patch"],
            "description": "Execution subtype.",
        },
        "plan": {
            "type": "string",
            "minLength": 10,
            "description": "How the root cause will be fixed. Must reference the root cause.",
        },
    },
    "required": ["phase", "subtype", "principals", "content", "plan"],
}

# JUDGE phase: verification
JUDGE_RECORD_SCHEMA: dict[str, Any] = {
    **PHASE_RECORD_BASE_SCHEMA,
    "properties": {
        **PHASE_RECORD_BASE_SCHEMA["properties"],
        "phase": {
            "type": "string",
            "enum": ["JUDGE"],
            "description": "The current reasoning phase.",
        },
        "subtype": {
            "type": "string",
            "enum": ["judge.verification"],
            "description": "Judge subtype.",
        },
    },
    "required": ["phase", "subtype", "principals", "content"],
}

# Mapping: phase -> cognition-aligned record schema
PHASE_RECORD_SCHEMAS: dict[str, dict[str, Any]] = {
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


