"""
test_phase_schemas.py — Tests for contract-derived PHASE_RECORD_SCHEMAS.

Updated: Legacy ANALYZE_SCHEMA/EXECUTE_SCHEMA/JUDGE_SCHEMA deleted in A-02.
Now tests PHASE_RECORD_SCHEMAS derived from cognition_contracts (B-10).
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest
from phase_schemas import (
    PHASE_RECORD_SCHEMAS,
    get_phase_record_schema,
)


class TestPhaseRecordSchemas:

    def test_has_entries(self):
        """PHASE_RECORD_SCHEMAS has at least 5 entries (OBSERVE through JUDGE)."""
        assert len(PHASE_RECORD_SCHEMAS) >= 5

    def test_analyze_schema_exists(self):
        """ANALYZE has a schema entry."""
        assert "ANALYZE" in PHASE_RECORD_SCHEMAS

    def test_schema_has_properties(self):
        """Each schema has 'properties' and 'required' keys."""
        for phase, schema in PHASE_RECORD_SCHEMAS.items():
            assert "properties" in schema, f"{phase} missing properties"
            assert "required" in schema, f"{phase} missing required"

    def test_analyze_has_root_cause(self):
        """ANALYZE schema includes root_cause in properties."""
        schema = PHASE_RECORD_SCHEMAS["ANALYZE"]
        assert "root_cause" in schema["properties"]


class TestGetPhaseRecordSchema:

    def test_analyze_found(self):
        result = get_phase_record_schema("ANALYZE")
        assert result is not None
        assert "properties" in result

    def test_unknown_returns_none(self):
        assert get_phase_record_schema("UNKNOWN") is None

    def test_case_handling(self):
        """Function handles the phase name as given."""
        # PHASE_RECORD_SCHEMAS uses uppercase keys
        assert get_phase_record_schema("ANALYZE") is not None
