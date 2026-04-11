"""
test_jingu_onboard.py — Unit tests for constrained decoding schema validation.

Verifies:
- _validate_adapted_schema catches missing additionalProperties, $ref, depth limit
- get_constrained_schema returns valid adapted schema for phases with schemas
- get_constrained_schema returns None for phases without schemas (e.g. UNDERSTAND)
- get_constrained_schema returns None + warning for invalid schemas
"""
import sys
import os
import copy
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest
from jingu_onboard import (
    _adapt_schema_for_constrained_decoding,
    _validate_adapted_schema,
    onboard,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

VALID_SCHEMA = {
    "type": "object",
    "properties": {
        "phase": {"type": "string"},
        "items": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["phase"],
    "additionalProperties": False,
}

NESTED_OBJECT_SCHEMA = {
    "type": "object",
    "properties": {
        "phase": {"type": "string"},
        "detail": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    "required": ["phase", "detail"],
    "additionalProperties": False,
}


# ── _validate_adapted_schema tests ───────────────────────────────────────────

class TestValidateAdaptedSchema:

    def test_valid_flat_schema_passes(self):
        errors = _validate_adapted_schema(VALID_SCHEMA, "TEST")
        assert errors == []

    def test_valid_nested_schema_passes(self):
        errors = _validate_adapted_schema(NESTED_OBJECT_SCHEMA, "TEST")
        assert errors == []

    def test_missing_additional_properties_at_top_level(self):
        schema = {
            "type": "object",
            "properties": {"phase": {"type": "string"}},
            "required": ["phase"],
            # no additionalProperties
        }
        errors = _validate_adapted_schema(schema, "TEST")
        assert any("additionalProperties" in e for e in errors)

    def test_missing_required_array(self):
        schema = {
            "type": "object",
            "properties": {"phase": {"type": "string"}},
            "additionalProperties": False,
            # no required
        }
        errors = _validate_adapted_schema(schema, "TEST")
        assert any("required" in e for e in errors)

    def test_nested_object_missing_additional_properties(self):
        schema = {
            "type": "object",
            "properties": {
                "detail": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    # missing additionalProperties
                },
            },
            "required": ["detail"],
            "additionalProperties": False,
        }
        errors = _validate_adapted_schema(schema, "TEST")
        assert len(errors) == 1
        assert "additionalProperties" in errors[0]
        assert "detail" in errors[0]

    def test_array_items_object_validated(self):
        schema = {
            "type": "object",
            "properties": {
                "items_list": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"x": {"type": "string"}},
                        # missing required and additionalProperties
                    },
                },
            },
            "required": ["items_list"],
            "additionalProperties": False,
        }
        errors = _validate_adapted_schema(schema, "TEST")
        assert any("additionalProperties" in e for e in errors)
        assert any("required" in e for e in errors)

    def test_ref_detected(self):
        schema = {
            "type": "object",
            "properties": {
                "link": {"$ref": "#/definitions/Foo"},
            },
            "required": ["link"],
            "additionalProperties": False,
        }
        errors = _validate_adapted_schema(schema, "TEST")
        assert any("$ref" in e for e in errors)

    def test_depth_limit_exceeded(self):
        # Build a schema 6 levels deep
        inner = {
            "type": "object",
            "properties": {"v": {"type": "string"}},
            "required": ["v"],
            "additionalProperties": False,
        }
        for _ in range(5):
            inner = {
                "type": "object",
                "properties": {"child": inner},
                "required": ["child"],
                "additionalProperties": False,
            }
        errors = _validate_adapted_schema(inner, "TEST")
        assert any("depth" in e for e in errors)

    def test_non_object_type_passes(self):
        schema = {"type": "string"}
        errors = _validate_adapted_schema(schema, "TEST")
        assert errors == []


# ── _adapt + _validate integration ──────────────────────────────────────────

class TestAdaptAndValidate:

    def test_adapt_adds_additional_properties_to_nested(self):
        """_adapt_schema_for_constrained_decoding should add additionalProperties: false
        to nested objects, making them pass validation."""
        raw = {
            "type": "object",
            "properties": {
                "phase": {"type": "string"},
                "detail": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            },
            "required": ["phase", "detail"],
        }
        adapted = _adapt_schema_for_constrained_decoding(raw)
        errors = _validate_adapted_schema(adapted, "TEST")
        assert errors == []
        assert adapted["additionalProperties"] is False
        assert adapted["properties"]["detail"]["additionalProperties"] is False

    def test_adapt_removes_unsupported_constraints(self):
        raw = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 5, "maxLength": 100},
                "count": {"type": "integer", "minimum": 0, "maximum": 10},
            },
            "required": ["name"],
        }
        adapted = _adapt_schema_for_constrained_decoding(raw)
        name_prop = adapted["properties"]["name"]
        assert "minLength" not in name_prop
        assert "maxLength" not in name_prop
        count_prop = adapted["properties"]["count"]
        assert "minimum" not in count_prop
        assert "maximum" not in count_prop


# ── get_constrained_schema via onboard ───────────────────────────────────────

class TestGetConstrainedSchema:

    @pytest.fixture(autouse=True)
    def _gov(self):
        self.gov = onboard(force_reload=True)

    def test_analyze_returns_schema_with_additional_properties(self):
        schema = self.gov.get_constrained_schema("ANALYZE")
        assert schema is not None
        assert schema.get("additionalProperties") is False

    def test_understand_returns_none(self):
        """UNDERSTAND has no schema in the bundle — should return None."""
        schema = self.gov.get_constrained_schema("UNDERSTAND")
        assert schema is None

    def test_all_returned_schemas_pass_validation(self):
        """Every phase that returns a schema must pass validation."""
        for phase in self.gov.list_phases():
            schema = self.gov.get_constrained_schema(phase)
            if schema is not None:
                errors = _validate_adapted_schema(schema, phase)
                assert errors == [], f"Phase {phase} schema has errors: {errors}"

    def test_invalid_schema_returns_none_with_warning(self, caplog):
        """If adapted schema has $ref, get_constrained_schema returns None."""
        # Inject a bad schema into the governance object for testing
        cfg = self.gov.get_phase_config("ANALYZE")
        assert cfg is not None

        # Create a modified PhaseConfig with $ref in schema
        from jingu_onboard import PhaseConfig
        bad_schema = copy.deepcopy(cfg.schema)
        bad_schema["properties"]["bad_field"] = {"$ref": "#/definitions/Foo"}

        bad_cfg = PhaseConfig(
            phase=cfg.phase,
            subtype=cfg.subtype,
            prompt=cfg.prompt,
            schema=bad_schema,
            gate=cfg.gate,
            cognition=cfg.cognition,
            repair_templates=cfg.repair_templates,
            routing=cfg.routing,
            allowed_next_phases=cfg.allowed_next_phases,
        )
        self.gov._phases["ANALYZE"] = bad_cfg

        with caplog.at_level(logging.WARNING):
            result = self.gov.get_constrained_schema("ANALYZE")

        assert result is None
        assert any("[schema_validation]" in r.message for r in caplog.records)
