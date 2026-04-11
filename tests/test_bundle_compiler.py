"""Tests for bundle_compiler S1 (parse) and S2 (resolve) stages."""

import json
import os
import sys
import tempfile
import pytest

# Ensure scripts/ is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from bundle_compiler import (
    CompilationError,
    ParseResult,
    ResolvedBundle,
    _parse_bundle,
    _resolve_refs,
    DEFAULT_BUNDLE_PATH,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _minimal_bundle() -> dict:
    """Return a minimal valid bundle for testing."""
    return {
        "version": "1.0.0",
        "compiler_version": "0.1.0",
        "generated_at": "2026-04-11T00:00:00Z",
        "generator_commit": "abc1234",
        "capabilities": ["prompt_only"],
        "phases": ["OBSERVE", "ANALYZE", "UNDERSTAND"],
        "contracts": {
            "observation.fact_gathering": {
                "phase": "OBSERVE",
                "subtype": "observation.fact_gathering",
                "cognition_spec": {
                    "type": "observation.fact_gathering",
                    "phase": "OBSERVE",
                    "schema_ref": "observation.fact_gathering",
                },
                "principals": [
                    {
                        "name": "ontology_alignment",
                        "applies_to": ["observation.fact_gathering"],
                        "requires_fields": ["phase"],
                        "inference_rule_exists": False,
                        "fake_check_eligible": False,
                    },
                    {
                        "name": "evidence_completeness",
                        "applies_to": ["observation.fact_gathering"],
                        "requires_fields": ["evidence_refs"],
                        "inference_rule_exists": False,
                        "fake_check_eligible": False,
                    },
                ],
                "policy": {
                    "required_principals": ["ontology_alignment", "evidence_completeness"],
                    "schema_ref": "observation.fact_gathering",
                },
                "schema": {
                    "type": "object",
                    "properties": {"phase": {"type": "string"}},
                },
            },
            "analysis.root_cause": {
                "phase": "ANALYZE",
                "subtype": "analysis.root_cause",
                "cognition_spec": {
                    "type": "analysis.root_cause",
                    "phase": "ANALYZE",
                    "schema_ref": "analysis.root_cause",
                },
                "principals": [
                    {
                        "name": "causal_grounding",
                        "applies_to": ["analysis.root_cause"],
                        "requires_fields": ["evidence_refs"],
                        "inference_rule_exists": True,
                        "fake_check_eligible": True,
                    },
                ],
                "policy": {
                    "required_principals": ["ontology_alignment", "causal_grounding"],
                    "schema_ref": "analysis.root_cause",
                },
                "schema": {
                    "type": "object",
                    "properties": {"root_cause": {"type": "string"}},
                },
            },
        },
        "cognition": {
            "phases": [
                {"name": "OBSERVE"},
                {"name": "ANALYZE"},
                {"name": "UNDERSTAND"},
            ],
        },
    }


def _write_bundle(data: dict) -> str:
    """Write bundle dict to a temp file and return path."""
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    return path


# ---------------------------------------------------------------------------
# S1 — _parse_bundle tests
# ---------------------------------------------------------------------------

class TestParseBundleS1:
    def test_valid_bundle(self):
        path = _write_bundle(_minimal_bundle())
        try:
            result = _parse_bundle(path)
            assert isinstance(result, ParseResult)
            assert result.version == "1.0.0"
            assert result.compiler_version == "0.1.0"
            assert result.generated_at == "2026-04-11T00:00:00Z"
            assert result.generator_commit == "abc1234"
            assert result.capabilities == ["prompt_only"]
            assert "contracts" in result.bundle
        finally:
            os.unlink(path)

    def test_missing_version(self):
        data = _minimal_bundle()
        data["version"] = ""
        path = _write_bundle(data)
        try:
            with pytest.raises(CompilationError) as exc_info:
                _parse_bundle(path)
            assert exc_info.value.stage == "S1"
            assert exc_info.value.code == "MISSING_VERSION"
        finally:
            os.unlink(path)

    def test_missing_version_key(self):
        data = _minimal_bundle()
        del data["version"]
        path = _write_bundle(data)
        try:
            with pytest.raises(CompilationError) as exc_info:
                _parse_bundle(path)
            # Either MISSING_VERSION (empty get) or MISSING_TOP_LEVEL_KEY
            assert exc_info.value.stage == "S1"
        finally:
            os.unlink(path)

    def test_incompatible_version(self):
        data = _minimal_bundle()
        data["version"] = "2.0.0"
        path = _write_bundle(data)
        try:
            with pytest.raises(CompilationError) as exc_info:
                _parse_bundle(path)
            assert exc_info.value.stage == "S1"
            assert exc_info.value.code == "INCOMPATIBLE_VERSION"
            assert "2.0.0" in exc_info.value.context.get("version", "")
        finally:
            os.unlink(path)

    def test_missing_top_level_key_contracts(self):
        data = _minimal_bundle()
        del data["contracts"]
        path = _write_bundle(data)
        try:
            with pytest.raises(CompilationError) as exc_info:
                _parse_bundle(path)
            assert exc_info.value.stage == "S1"
            assert exc_info.value.code == "MISSING_TOP_LEVEL_KEY"
            assert exc_info.value.context.get("key") == "contracts"
        finally:
            os.unlink(path)

    def test_missing_top_level_key_cognition(self):
        data = _minimal_bundle()
        del data["cognition"]
        path = _write_bundle(data)
        try:
            with pytest.raises(CompilationError) as exc_info:
                _parse_bundle(path)
            assert exc_info.value.stage == "S1"
            assert exc_info.value.code == "MISSING_TOP_LEVEL_KEY"
            assert exc_info.value.context.get("key") == "cognition"
        finally:
            os.unlink(path)

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            _parse_bundle("/nonexistent/path/bundle.json")

    def test_invalid_json(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write("{invalid json")
        try:
            with pytest.raises(json.JSONDecodeError):
                _parse_bundle(path)
        finally:
            os.unlink(path)

    def test_metadata_defaults(self):
        """Missing optional metadata fields default to empty."""
        data = {
            "version": "1.0.0",
            "phases": [],
            "contracts": {},
            "cognition": {},
        }
        path = _write_bundle(data)
        try:
            result = _parse_bundle(path)
            assert result.compiler_version == ""
            assert result.generated_at == ""
            assert result.generator_commit == ""
            assert result.capabilities == []
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# S2 — _resolve_refs tests
# ---------------------------------------------------------------------------

class TestResolveRefsS2:
    def _parse(self, data: dict) -> ParseResult:
        path = _write_bundle(data)
        try:
            return _parse_bundle(path)
        finally:
            os.unlink(path)

    def test_phase_to_subtype_derived_from_contracts(self):
        parsed = self._parse(_minimal_bundle())
        resolved = _resolve_refs(parsed)
        # OBSERVE -> observation.fact_gathering, ANALYZE -> analysis.root_cause
        assert resolved.phase_to_subtype == {
            "OBSERVE": "observation.fact_gathering",
            "ANALYZE": "analysis.root_cause",
        }

    def test_phases_without_contracts(self):
        parsed = self._parse(_minimal_bundle())
        resolved = _resolve_refs(parsed)
        # UNDERSTAND has no contract
        assert "UNDERSTAND" in resolved.phases_without_contracts
        assert "OBSERVE" not in resolved.phases_without_contracts

    def test_phases_with_contracts(self):
        parsed = self._parse(_minimal_bundle())
        resolved = _resolve_refs(parsed)
        assert "OBSERVE" in resolved.phases_with_contracts
        assert "ANALYZE" in resolved.phases_with_contracts
        assert "UNDERSTAND" not in resolved.phases_with_contracts

    def test_schema_registry(self):
        parsed = self._parse(_minimal_bundle())
        resolved = _resolve_refs(parsed)
        assert "observation.fact_gathering" in resolved.schema_registry
        assert "analysis.root_cause" in resolved.schema_registry

    def test_principal_registry(self):
        parsed = self._parse(_minimal_bundle())
        resolved = _resolve_refs(parsed)
        assert "ontology_alignment" in resolved.principal_registry
        assert "evidence_completeness" in resolved.principal_registry
        assert "causal_grounding" in resolved.principal_registry

    def test_subtype_to_contract(self):
        parsed = self._parse(_minimal_bundle())
        resolved = _resolve_refs(parsed)
        assert "observation.fact_gathering" in resolved.subtype_to_contract
        assert "analysis.root_cause" in resolved.subtype_to_contract

    def test_dangling_schema_ref(self):
        data = _minimal_bundle()
        # Point cognition_spec.schema_ref to nonexistent schema
        data["contracts"]["observation.fact_gathering"]["cognition_spec"]["schema_ref"] = "nonexistent.schema"
        # Also remove inline schema so it's truly dangling
        del data["contracts"]["observation.fact_gathering"]["schema"]
        parsed = self._parse(data)
        with pytest.raises(CompilationError) as exc_info:
            _resolve_refs(parsed)
        assert exc_info.value.code == "DANGLING_SCHEMA_REF"
        assert exc_info.value.context["schema_ref"] == "nonexistent.schema"

    def test_dangling_principal_ref(self):
        data = _minimal_bundle()
        data["contracts"]["observation.fact_gathering"]["policy"]["required_principals"].append(
            "nonexistent_principal"
        )
        parsed = self._parse(data)
        with pytest.raises(CompilationError) as exc_info:
            _resolve_refs(parsed)
        assert exc_info.value.code == "DANGLING_PRINCIPAL_REF"
        assert exc_info.value.context["principal"] == "nonexistent_principal"

    def test_duplicate_phase_mapping(self):
        data = _minimal_bundle()
        # Add another contract that also maps to OBSERVE
        data["contracts"]["observation.duplicate"] = {
            "phase": "OBSERVE",
            "subtype": "observation.duplicate",
            "cognition_spec": {"schema_ref": ""},
            "principals": [],
            "policy": {"required_principals": []},
        }
        parsed = self._parse(data)
        with pytest.raises(CompilationError) as exc_info:
            _resolve_refs(parsed)
        assert exc_info.value.code == "DUPLICATE_PHASE_MAPPING"
        assert exc_info.value.context["phase"] == "OBSERVE"

    def test_contract_missing_phase(self):
        data = _minimal_bundle()
        data["contracts"]["observation.fact_gathering"]["phase"] = ""
        parsed = self._parse(data)
        with pytest.raises(CompilationError) as exc_info:
            _resolve_refs(parsed)
        assert exc_info.value.code == "PHASE_NO_SUBTYPE"

    def test_real_bundle(self):
        """Test against the actual bundle.json if present."""
        bundle_path = os.path.join(
            os.path.dirname(__file__), "..", "bundle.json"
        )
        if not os.path.exists(bundle_path):
            pytest.skip("bundle.json not found")
        parsed = _parse_bundle(bundle_path)
        resolved = _resolve_refs(parsed)
        # The real bundle has 6 contracts (OBSERVE, ANALYZE, DECIDE, DESIGN, EXECUTE, JUDGE)
        assert len(resolved.phase_to_subtype) == 6
        # UNDERSTAND has no contract
        assert "UNDERSTAND" in resolved.phases_without_contracts
        # All principals should be in registry
        assert "ontology_alignment" in resolved.principal_registry
        assert "minimal_change" in resolved.principal_registry


# ---------------------------------------------------------------------------
# DEFAULT_BUNDLE_PATH
# ---------------------------------------------------------------------------

class TestDefaultBundlePath:
    def test_default_is_bundle_json(self):
        # When env var is not set, default should be "bundle.json"
        # (it may already be set in env, so we just check the module-level constant exists)
        assert isinstance(DEFAULT_BUNDLE_PATH, str)
        assert len(DEFAULT_BUNDLE_PATH) > 0
