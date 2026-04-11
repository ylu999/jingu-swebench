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


# ── ANALYZE phase schema ────────────────────────────────────────────────────

ANALYZE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "phase": {
            "type": "string",
            "enum": ["ANALYZE"],
            "description": "The current reasoning phase.",
        },
        "fix_type": {
            "type": "string",
            "description": "Classification of the fix approach (e.g. 'analysis', 'diagnosis').",
        },
        "principals": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Declared cognitive principals for this phase (e.g. 'causal_grounding', 'evidence_linkage').",
        },
        "root_cause": {
            "type": "string",
            "minLength": 20,
            "description": "The identified root cause. Must reference specific code locations (file/function/line).",
        },
        "causal_chain": {
            "type": "string",
            "description": "Step-by-step causal chain: test failure -> condition -> code -> why it fails.",
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "File path (e.g. 'django/db/models/fields/__init__.py').",
                    },
                    "line": {
                        "type": "integer",
                        "description": "Line number in the file.",
                    },
                    "observation": {
                        "type": "string",
                        "description": "What was observed at this location.",
                    },
                },
                "required": ["file", "observation"],
            },
            "description": "Evidence items grounding the root cause in code.",
        },
        "alternative_hypotheses": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "description": "Alternative hypotheses considered and reasons they were rejected.",
        },
    },
    "required": ["phase", "fix_type", "principals", "root_cause", "evidence"],
}


# ── EXECUTE phase schema ────────────────────────────────────────────────────

EXECUTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "phase": {
            "type": "string",
            "enum": ["EXECUTE"],
            "description": "The current reasoning phase.",
        },
        "fix_type": {
            "type": "string",
            "description": "Classification of the fix approach (e.g. 'execution', 'code_patch').",
        },
        "principals": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Declared cognitive principals for this phase (e.g. 'minimal_change', 'action_grounding').",
        },
        "plan": {
            "type": "string",
            "minLength": 10,
            "description": "How the root cause from ANALYZE will be fixed. Must reference the root cause.",
        },
        "patch_description": {
            "type": "string",
            "description": "Description of the code changes to be made.",
        },
        "change_scope": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Files/functions that will be modified.",
        },
    },
    "required": ["phase", "fix_type", "principals", "plan"],
}


# ── JUDGE phase schema ──────────────────────────────────────────────────────

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "phase": {
            "type": "string",
            "enum": ["JUDGE"],
            "description": "The current reasoning phase.",
        },
        "fix_type": {
            "type": "string",
            "description": "Classification of the fix approach.",
        },
        "principals": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Declared cognitive principals for this phase (e.g. 'result_verification', 'uncertainty_honesty').",
        },
        "verification_result": {
            "type": "string",
            "enum": ["pass", "fail", "partial", "inconclusive"],
            "description": "Overall verification outcome.",
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Confidence in the verification result (0.0 to 1.0).",
        },
        "test_evidence": {
            "type": "string",
            "description": "Evidence from test execution supporting the verification result.",
        },
        "remaining_risks": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Known risks or limitations of the fix.",
        },
    },
    "required": ["phase", "fix_type", "principals", "verification_result", "confidence"],
}


# ── Phase-to-schema mapping ─────────────────────────────────────────────────

PHASE_SCHEMAS: dict[str, dict[str, Any]] = {
    "ANALYZE": ANALYZE_SCHEMA,
    "EXECUTE": EXECUTE_SCHEMA,
    "JUDGE": JUDGE_SCHEMA,
}


def get_phase_schema(phase: str) -> dict[str, Any] | None:
    """Return the JSON Schema for a given phase, or None if no schema is defined.

    Args:
        phase: Phase name (case-insensitive, e.g. 'ANALYZE', 'analyze').

    Returns:
        JSON Schema dict or None.
    """
    return PHASE_SCHEMAS.get(phase.upper())


def get_structured_output_tool(phase: str) -> dict[str, Any] | None:
    """Build a Bedrock tool definition that forces structured output for a phase.

    Returns a tool config dict suitable for Bedrock Converse API's toolConfig,
    or None if no schema exists for the phase.

    The tool is named 'structured_output' and the model is forced to call it
    via tool_choice={"tool": {"name": "structured_output"}}.

    Usage with litellm:
        tool = get_structured_output_tool("ANALYZE")
        if tool:
            response = litellm.completion(
                model=MODEL,
                messages=messages,
                tools=[tool["tool"]],
                tool_choice=tool["tool_choice"],
            )
            # Extract structured output from tool call arguments
            result = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
    """
    schema = get_phase_schema(phase)
    if schema is None:
        return None
    return {
        "tool": {
            "type": "function",
            "function": {
                "name": "structured_output",
                "description": (
                    f"Submit your {phase} phase reasoning output in structured format. "
                    f"All required fields must be provided."
                ),
                "parameters": schema,
            },
        },
        "tool_choice": {"type": "function", "function": {"name": "structured_output"}},
    }
