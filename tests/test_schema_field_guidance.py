"""
test_schema_field_guidance.py — Tests for the schema → field guidance renderer.

Verifies that render_schema_field_guidance() produces deterministic,
schema-driven output and that validate_schema_descriptions() catches
missing descriptions.
"""

import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from schema_field_guidance import (
    render_schema_field_guidance,
    validate_schema_descriptions,
    _unwrap_schema,
    _render_field_type,
)


# -- Test schema fixtures --

ANALYZE_SCHEMA = {
    "type": "object",
    "properties": {
        "root_cause": {
            "type": "string",
            "description": "The identified root cause with file:line reference.",
        },
        "causal_chain": {
            "type": "string",
            "description": "Step-by-step causal chain from test failure to code bug.",
        },
        "evidence_refs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "File:line evidence references.",
        },
    },
    "required": ["root_cause", "causal_chain", "evidence_refs"],
}

SCHEMA_WITH_OPTIONAL = {
    "type": "object",
    "properties": {
        "plan": {
            "type": "string",
            "description": "Fix plan referencing root cause.",
        },
        "notes": {
            "type": "string",
            "description": "Optional notes.",
        },
    },
    "required": ["plan"],
}

SCHEMA_MISSING_DESC = {
    "type": "object",
    "properties": {
        "root_cause": {
            "type": "string",
            "description": "Good description here.",
        },
        "causal_chain": {
            "type": "string",
            # no description!
        },
    },
    "required": ["root_cause", "causal_chain"],
}


class TestRenderSchemaFieldGuidance:
    def test_basic_rendering(self):
        result = render_schema_field_guidance(ANALYZE_SCHEMA, phase="ANALYZE")
        assert "ANALYZE" in result
        assert "root_cause" in result
        assert "causal_chain" in result
        assert "evidence_refs" in result
        assert "required" in result

    def test_required_vs_optional(self):
        result = render_schema_field_guidance(SCHEMA_WITH_OPTIONAL, phase="EXECUTE")
        assert "plan [required" in result
        assert "notes [optional" in result

    def test_descriptions_from_schema(self):
        result = render_schema_field_guidance(ANALYZE_SCHEMA, phase="ANALYZE")
        assert "The identified root cause with file:line reference." in result
        assert "Step-by-step causal chain" in result

    def test_empty_schema_returns_empty(self):
        result = render_schema_field_guidance({}, phase="EMPTY")
        assert result == ""

    def test_no_properties_returns_empty(self):
        result = render_schema_field_guidance({"type": "object"}, phase="X")
        assert result == ""

    def test_missing_description_has_fallback(self):
        result = render_schema_field_guidance(SCHEMA_MISSING_DESC, phase="TEST")
        assert "causal_chain" in result
        assert "no description" in result.lower()

    def test_deterministic(self):
        """Same schema, same output — no randomness."""
        r1 = render_schema_field_guidance(ANALYZE_SCHEMA, phase="ANALYZE")
        r2 = render_schema_field_guidance(ANALYZE_SCHEMA, phase="ANALYZE")
        assert r1 == r2


class TestValidateSchemaDescriptions:
    def test_all_present(self):
        missing = validate_schema_descriptions(ANALYZE_SCHEMA, phase="ANALYZE")
        assert missing == []

    def test_missing_detected(self):
        missing = validate_schema_descriptions(SCHEMA_MISSING_DESC, phase="TEST")
        assert len(missing) == 1
        assert "causal_chain" in missing[0]
        assert "description missing" in missing[0]

    def test_no_properties(self):
        """Schema with no properties has nothing to validate."""
        missing = validate_schema_descriptions({"type": "object"}, phase="EMPTY")
        assert missing == []


class TestUnwrapSchema:
    def test_direct_object(self):
        result = _unwrap_schema({"type": "object", "properties": {"a": {}}})
        assert "properties" in result

    def test_nested_schema(self):
        result = _unwrap_schema({"schema": {"type": "object", "properties": {"a": {}}}})
        assert "properties" in result

    def test_json_schema_wrapper(self):
        result = _unwrap_schema({
            "json_schema": {"schema": {"type": "object", "properties": {"a": {}}}}
        })
        assert "properties" in result


class TestRenderFieldType:
    def test_string(self):
        assert _render_field_type({"type": "string"}) == "string"

    def test_array(self):
        result = _render_field_type({"type": "array", "items": {"type": "string"}})
        assert result == "array[string]"

    def test_enum(self):
        result = _render_field_type({"type": "string", "enum": ["a", "b"]})
        assert "enum" in result
        assert "'a'" in result

    def test_object(self):
        assert _render_field_type({"type": "object"}) == "object"
