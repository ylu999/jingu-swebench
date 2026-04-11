"""extraction_schemas.py — JSON Schemas for structured phase extraction.

These schemas are used with grammar-constrained sampling (response_format
json_schema) to guarantee schema-valid LLM output at phase transitions.

Constraints (from Anthropic structured output spec):
  - No minLength / maxLength (not supported)
  - No minimum / maximum for numbers (not supported)
  - additionalProperties must be false for all objects
  - minItems: only 0 and 1 are supported

These are the EXTRACTION schemas, distinct from phase_schemas.py which
were designed for tool-use enforcement. Extraction schemas are simpler
and focused on what the gate needs to evaluate.
"""

from typing import Any


# ── ANALYZE extraction schema ─────────────────────────────────────────────

ANALYZE_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "phase": {
            "type": "string",
            "enum": ["ANALYZE"],
            "description": "Must be ANALYZE.",
        },
        "fix_type": {
            "type": "string",
            "description": "Classification of the fix approach (e.g. 'analysis', 'diagnosis').",
        },
        "principals": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Cognitive principals declared for this phase.",
        },
        "root_cause": {
            "type": "string",
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
                        "description": "File path.",
                    },
                    "line": {
                        "type": "integer",
                        "description": "Line number.",
                    },
                    "observation": {
                        "type": "string",
                        "description": "What was observed at this location.",
                    },
                },
                "required": ["file", "observation"],
                "additionalProperties": False,
            },
            "description": "Evidence items grounding the root cause in code.",
        },
        "alternative_hypotheses": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Alternative hypotheses considered and reasons they were rejected.",
        },
    },
    "required": [
        "phase", "fix_type", "principals", "root_cause",
        "causal_chain", "evidence", "alternative_hypotheses",
    ],
    "additionalProperties": False,
}


# ── EXECUTE extraction schema ─────────────────────────────────────────────

EXECUTE_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "phase": {
            "type": "string",
            "enum": ["EXECUTE"],
            "description": "Must be EXECUTE.",
        },
        "fix_type": {
            "type": "string",
            "description": "Classification of the fix approach.",
        },
        "principals": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Cognitive principals declared for this phase.",
        },
        "plan": {
            "type": "string",
            "description": "How the root cause will be fixed. Must reference the root cause.",
        },
        "patch_description": {
            "type": "string",
            "description": "Description of code changes made.",
        },
        "change_scope": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Files/functions modified.",
        },
    },
    "required": ["phase", "fix_type", "principals", "plan"],
    "additionalProperties": False,
}


# ── JUDGE extraction schema ───────────────────────────────────────────────

JUDGE_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "phase": {
            "type": "string",
            "enum": ["JUDGE"],
            "description": "Must be JUDGE.",
        },
        "fix_type": {
            "type": "string",
            "description": "Classification of the fix approach.",
        },
        "principals": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Cognitive principals declared for this phase.",
        },
        "verification_result": {
            "type": "string",
            "enum": ["pass", "fail", "partial", "inconclusive"],
            "description": "Overall verification outcome.",
        },
        "confidence": {
            "type": "number",
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
    "additionalProperties": False,
}


# ── OBSERVE extraction schema ─────────────────────────────────────────────

OBSERVE_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "phase": {
            "type": "string",
            "enum": ["OBSERVE"],
            "description": "Must be OBSERVE.",
        },
        "principals": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Cognitive principals declared for this phase.",
        },
        "observations": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Key observations made during this phase.",
        },
        "evidence_refs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "File:line references observed.",
        },
    },
    "required": ["phase", "principals", "observations", "evidence_refs"],
    "additionalProperties": False,
}


# ── Phase → schema mapping ────────────────────────────────────────────────

EXTRACTION_SCHEMAS: dict[str, dict[str, Any]] = {
    "ANALYZE": ANALYZE_EXTRACTION_SCHEMA,
    "EXECUTE": EXECUTE_EXTRACTION_SCHEMA,
    "JUDGE": JUDGE_EXTRACTION_SCHEMA,
    "OBSERVE": OBSERVE_EXTRACTION_SCHEMA,
}


def get_extraction_schema(phase: str) -> dict[str, Any] | None:
    """Return the extraction schema for a phase, or None if not defined."""
    return EXTRACTION_SCHEMAS.get(phase.upper())
