"""
test_phase_schemas.py — Unit tests for p221 phase JSON schemas.

Verifies:
- Each schema is valid JSON Schema
- Sample valid outputs match schema
- Sample invalid outputs are rejected
- get_phase_schema() returns correct schemas or None
- get_structured_output_tool() returns correct tool config
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest
from jsonschema import validate, ValidationError, Draft7Validator
from phase_schemas import (
    ANALYZE_SCHEMA,
    EXECUTE_SCHEMA,
    JUDGE_SCHEMA,
    PHASE_SCHEMAS,
    get_phase_schema,
    get_structured_output_tool,
)


# ── Schema validity ─────────────────────────────────────────────────────────

class TestSchemaValidity:

    def test_analyze_schema_is_valid_json_schema(self):
        """ANALYZE_SCHEMA must be a valid JSON Schema."""
        Draft7Validator.check_schema(ANALYZE_SCHEMA)

    def test_execute_schema_is_valid_json_schema(self):
        """EXECUTE_SCHEMA must be a valid JSON Schema."""
        Draft7Validator.check_schema(EXECUTE_SCHEMA)

    def test_judge_schema_is_valid_json_schema(self):
        """JUDGE_SCHEMA must be a valid JSON Schema."""
        Draft7Validator.check_schema(JUDGE_SCHEMA)


# ── ANALYZE schema ───────────────────────────────────────────────────────────

class TestAnalyzeSchema:

    def test_valid_analyze_output(self):
        """A well-formed ANALYZE output matches the schema."""
        output = {
            "phase": "ANALYZE",
            "fix_type": "analysis",
            "principals": ["causal_grounding", "evidence_linkage"],
            "root_cause": "DateTimeField.to_python() in django/db/models/fields/__init__.py:1234 does not handle timezone-naive datetime objects correctly.",
            "evidence": [
                {"file": "django/db/models/fields/__init__.py", "line": 1234, "observation": "to_python() calls strptime without tz"},
                {"file": "tests/test_models.py", "observation": "test_invalid_date fails with ValueError"},
            ],
            "causal_chain": "test -> clean() -> to_python() -> strptime() -> ValueError",
            "alternative_hypotheses": ["Could be in validate(), but ruled out by traceback"],
        }
        validate(instance=output, schema=ANALYZE_SCHEMA)

    def test_minimal_analyze_output(self):
        """Minimal ANALYZE output (required fields only) matches."""
        output = {
            "phase": "ANALYZE",
            "fix_type": "diagnosis",
            "principals": ["causal_grounding"],
            "root_cause": "The bug is in the validation logic at models.py line 45 where input is not sanitized",
            "evidence": [{"file": "models.py", "observation": "unsanitized input"}],
        }
        validate(instance=output, schema=ANALYZE_SCHEMA)

    def test_missing_root_cause_rejected(self):
        """ANALYZE output without root_cause is rejected."""
        output = {
            "phase": "ANALYZE",
            "fix_type": "analysis",
            "principals": [],
            "evidence": [],
        }
        with pytest.raises(ValidationError):
            validate(instance=output, schema=ANALYZE_SCHEMA)

    def test_wrong_phase_rejected(self):
        """ANALYZE schema rejects phase != 'ANALYZE'."""
        output = {
            "phase": "EXECUTE",
            "fix_type": "analysis",
            "principals": [],
            "root_cause": "some cause that is long enough to pass minLength",
            "evidence": [],
        }
        with pytest.raises(ValidationError):
            validate(instance=output, schema=ANALYZE_SCHEMA)

    def test_short_root_cause_rejected(self):
        """ANALYZE schema rejects root_cause shorter than 20 chars."""
        output = {
            "phase": "ANALYZE",
            "fix_type": "analysis",
            "principals": [],
            "root_cause": "too short",
            "evidence": [],
        }
        with pytest.raises(ValidationError):
            validate(instance=output, schema=ANALYZE_SCHEMA)


# ── EXECUTE schema ───────────────────────────────────────────────────────────

class TestExecuteSchema:

    def test_valid_execute_output(self):
        """A well-formed EXECUTE output matches the schema."""
        output = {
            "phase": "EXECUTE",
            "fix_type": "execution",
            "principals": ["minimal_change", "action_grounding"],
            "plan": "Fix the to_python() method to handle timezone-naive datetime objects",
            "patch_description": "Add timezone awareness check before strptime call",
            "change_scope": ["django/db/models/fields/__init__.py"],
        }
        validate(instance=output, schema=EXECUTE_SCHEMA)

    def test_minimal_execute_output(self):
        """Minimal EXECUTE output (required fields only) matches."""
        output = {
            "phase": "EXECUTE",
            "fix_type": "code_patch",
            "principals": ["minimal_change"],
            "plan": "Add null check before accessing the field value",
        }
        validate(instance=output, schema=EXECUTE_SCHEMA)

    def test_missing_plan_rejected(self):
        """EXECUTE output without plan is rejected."""
        output = {
            "phase": "EXECUTE",
            "fix_type": "execution",
            "principals": [],
        }
        with pytest.raises(ValidationError):
            validate(instance=output, schema=EXECUTE_SCHEMA)


# ── JUDGE schema ─────────────────────────────────────────────────────────────

class TestJudgeSchema:

    def test_valid_judge_output(self):
        """A well-formed JUDGE output matches the schema."""
        output = {
            "phase": "JUDGE",
            "fix_type": "verification",
            "principals": ["result_verification", "uncertainty_honesty"],
            "verification_result": "pass",
            "confidence": 0.85,
            "test_evidence": "All 3 failing tests now pass",
            "remaining_risks": ["Edge case with None input not tested"],
        }
        validate(instance=output, schema=JUDGE_SCHEMA)

    def test_minimal_judge_output(self):
        """Minimal JUDGE output (required fields only) matches."""
        output = {
            "phase": "JUDGE",
            "fix_type": "judge",
            "principals": ["result_verification"],
            "verification_result": "pass",
            "confidence": 0.9,
        }
        validate(instance=output, schema=JUDGE_SCHEMA)

    def test_invalid_verification_result_rejected(self):
        """JUDGE schema rejects invalid verification_result."""
        output = {
            "phase": "JUDGE",
            "fix_type": "judge",
            "principals": [],
            "verification_result": "maybe",
            "confidence": 0.5,
        }
        with pytest.raises(ValidationError):
            validate(instance=output, schema=JUDGE_SCHEMA)

    def test_confidence_out_of_range_rejected(self):
        """JUDGE schema rejects confidence > 1.0."""
        output = {
            "phase": "JUDGE",
            "fix_type": "judge",
            "principals": [],
            "verification_result": "pass",
            "confidence": 1.5,
        }
        with pytest.raises(ValidationError):
            validate(instance=output, schema=JUDGE_SCHEMA)


# ── Lookup functions ─────────────────────────────────────────────────────────

class TestGetPhaseSchema:

    def test_analyze_found(self):
        assert get_phase_schema("ANALYZE") is ANALYZE_SCHEMA

    def test_execute_found(self):
        assert get_phase_schema("EXECUTE") is EXECUTE_SCHEMA

    def test_judge_found(self):
        assert get_phase_schema("JUDGE") is JUDGE_SCHEMA

    def test_case_insensitive(self):
        assert get_phase_schema("analyze") is ANALYZE_SCHEMA
        assert get_phase_schema("Execute") is EXECUTE_SCHEMA

    def test_unknown_phase_returns_none(self):
        assert get_phase_schema("UNKNOWN") is None
        assert get_phase_schema("OBSERVE") is None

    def test_empty_string_returns_none(self):
        assert get_phase_schema("") is None


class TestGetStructuredOutputTool:

    def test_analyze_tool(self):
        tool = get_structured_output_tool("ANALYZE")
        assert tool is not None
        assert tool["tool"]["function"]["name"] == "structured_output"
        assert tool["tool"]["function"]["parameters"] is ANALYZE_SCHEMA
        assert tool["tool_choice"]["function"]["name"] == "structured_output"

    def test_unknown_phase_returns_none(self):
        assert get_structured_output_tool("UNKNOWN") is None

    def test_phase_schemas_dict_has_three_entries(self):
        assert len(PHASE_SCHEMAS) == 3
        assert set(PHASE_SCHEMAS.keys()) == {"ANALYZE", "EXECUTE", "JUDGE"}
