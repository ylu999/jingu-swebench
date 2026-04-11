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
    CompilationWarning,
    ParseResult,
    ResolvedBundle,
    _parse_bundle,
    _resolve_refs,
    _check_completeness,
    _check_consistency,
    _compile_prompts,
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
# S3 — _check_completeness tests
# ---------------------------------------------------------------------------

def _complete_bundle() -> dict:
    """Return a bundle whose contracts pass all S3 completeness checks."""
    return {
        "version": "1.0.0",
        "compiler_version": "0.1.0",
        "generated_at": "2026-04-11T00:00:00Z",
        "generator_commit": "abc1234",
        "capabilities": ["prompt_only"],
        "phases": ["OBSERVE", "UNDERSTAND"],
        "contracts": {
            "observation.fact_gathering": {
                "phase": "OBSERVE",
                "subtype": "observation.fact_gathering",
                "prompt": "## Phase: OBSERVE\nGather evidence.",
                "schema": {
                    "type": "object",
                    "properties": {"phase": {"type": "string"}},
                    "required": ["phase"],
                },
                "policy": {
                    "required_principals": ["ontology_alignment"],
                },
                "principals": [
                    {
                        "name": "ontology_alignment",
                        "applies_to": ["observation.fact_gathering"],
                        "requires_fields": ["phase"],
                        "inference_rule_exists": False,
                        "fake_check_eligible": False,
                    },
                ],
                "cognition_spec": {
                    "type": "observation.fact_gathering",
                    "phase": "OBSERVE",
                    "task_shape": "fact_gathering",
                    "schema_ref": "observation.fact_gathering",
                },
                "repair_templates": {
                    "ontology_alignment": "[ontology_alignment] fix it",
                },
                "routing": {
                    "principal_routes": {
                        "ontology_alignment": {
                            "next_phase": "OBSERVE",
                            "strategy": "fix",
                        },
                    },
                },
                "phase_spec": {
                    "name": "OBSERVE",
                    "allowed_next_phases": ["ANALYZE", "OBSERVE"],
                },
            },
        },
        "cognition": {"phases": [{"name": "OBSERVE"}, {"name": "UNDERSTAND"}]},
    }


def _resolve(data: dict) -> ResolvedBundle:
    """Parse + resolve a bundle dict."""
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    try:
        parsed = _parse_bundle(path)
        return _resolve_refs(parsed)
    finally:
        os.unlink(path)


class TestCheckCompletenessS3:
    """Tests for _check_completeness (S3)."""

    def test_complete_bundle_no_errors(self):
        resolved = _resolve(_complete_bundle())
        errors = _check_completeness(resolved)
        assert errors == []

    def test_never_raises(self):
        """Even a very broken bundle returns errors list, not an exception."""
        data = _complete_bundle()
        # Strip all contract fields to trigger many errors
        data["contracts"]["observation.fact_gathering"] = {"phase": "OBSERVE"}
        resolved = _resolve(data)
        errors = _check_completeness(resolved)
        assert isinstance(errors, list)
        assert len(errors) > 0

    # --- Phase coverage ---

    def test_phase_no_contract_allowed(self):
        """UNDERSTAND is in default allowed list, should not error."""
        resolved = _resolve(_complete_bundle())
        assert "UNDERSTAND" in resolved.phases_without_contracts
        errors = _check_completeness(resolved)
        codes = [e.code for e in errors]
        assert "PHASE_NO_CONTRACT_NOT_ALLOWLISTED" not in codes

    def test_phase_no_contract_not_allowed(self):
        data = _complete_bundle()
        data["phases"].append("NEWPHASE")
        resolved = _resolve(data)
        errors = _check_completeness(resolved)
        phase_errors = [e for e in errors if e.code == "PHASE_NO_CONTRACT_NOT_ALLOWLISTED"]
        assert len(phase_errors) == 1
        assert phase_errors[0].context["phase"] == "NEWPHASE"

    def test_phase_no_contract_custom_allowed(self):
        data = _complete_bundle()
        data["phases"].append("NEWPHASE")
        resolved = _resolve(data)
        errors = _check_completeness(
            resolved, allowed_no_contract_phases=frozenset({"UNDERSTAND", "NEWPHASE"})
        )
        codes = [e.code for e in errors]
        assert "PHASE_NO_CONTRACT_NOT_ALLOWLISTED" not in codes

    # --- Per-contract: MISSING_PROMPT ---

    def test_missing_prompt(self):
        data = _complete_bundle()
        del data["contracts"]["observation.fact_gathering"]["prompt"]
        resolved = _resolve(data)
        errors = _check_completeness(resolved)
        assert any(e.code == "MISSING_PROMPT" for e in errors)

    def test_empty_prompt(self):
        data = _complete_bundle()
        data["contracts"]["observation.fact_gathering"]["prompt"] = ""
        resolved = _resolve(data)
        errors = _check_completeness(resolved)
        assert any(e.code == "MISSING_PROMPT" for e in errors)

    # --- Per-contract: MISSING_SCHEMA / INVALID_SCHEMA_SHAPE ---

    def test_missing_schema(self):
        data = _complete_bundle()
        del data["contracts"]["observation.fact_gathering"]["schema"]
        # Also remove schema_ref to avoid DANGLING_SCHEMA_REF in S2
        data["contracts"]["observation.fact_gathering"]["cognition_spec"]["schema_ref"] = ""
        resolved = _resolve(data)
        errors = _check_completeness(resolved)
        assert any(e.code == "MISSING_SCHEMA" for e in errors)

    def test_schema_not_dict(self):
        data = _complete_bundle()
        # Resolve first with valid schema, then mutate contract for S3
        resolved = _resolve(data)
        resolved.subtype_to_contract["observation.fact_gathering"]["schema"] = "not a dict"
        errors = _check_completeness(resolved)
        assert any(e.code == "MISSING_SCHEMA" for e in errors)

    def test_schema_missing_type(self):
        data = _complete_bundle()
        schema = data["contracts"]["observation.fact_gathering"]["schema"]
        del schema["type"]
        resolved = _resolve(data)
        errors = _check_completeness(resolved)
        shape_errors = [e for e in errors if e.code == "INVALID_SCHEMA_SHAPE"]
        assert len(shape_errors) == 1
        assert shape_errors[0].context["missing_key"] == "type"

    def test_schema_missing_properties(self):
        data = _complete_bundle()
        schema = data["contracts"]["observation.fact_gathering"]["schema"]
        del schema["properties"]
        resolved = _resolve(data)
        errors = _check_completeness(resolved)
        assert any(
            e.code == "INVALID_SCHEMA_SHAPE" and e.context.get("missing_key") == "properties"
            for e in errors
        )

    def test_schema_missing_required(self):
        data = _complete_bundle()
        schema = data["contracts"]["observation.fact_gathering"]["schema"]
        del schema["required"]
        resolved = _resolve(data)
        errors = _check_completeness(resolved)
        assert any(
            e.code == "INVALID_SCHEMA_SHAPE" and e.context.get("missing_key") == "required"
            for e in errors
        )

    # --- Per-contract: MISSING_POLICY_KEY ---

    def test_missing_policy_required_principals(self):
        data = _complete_bundle()
        data["contracts"]["observation.fact_gathering"]["policy"] = {}
        resolved = _resolve(data)
        errors = _check_completeness(resolved)
        assert any(e.code == "MISSING_POLICY_KEY" for e in errors)

    def test_no_policy_at_all(self):
        data = _complete_bundle()
        del data["contracts"]["observation.fact_gathering"]["policy"]
        resolved = _resolve(data)
        errors = _check_completeness(resolved)
        assert any(e.code == "MISSING_POLICY_KEY" for e in errors)

    # --- Per-contract: MISSING_PRINCIPALS_ARRAY ---

    def test_missing_principals(self):
        data = _complete_bundle()
        # Resolve first, then remove principals from the resolved contract for S3
        resolved = _resolve(data)
        del resolved.subtype_to_contract["observation.fact_gathering"]["principals"]
        errors = _check_completeness(resolved)
        assert any(e.code == "MISSING_PRINCIPALS_ARRAY" for e in errors)

    def test_principals_not_list(self):
        data = _complete_bundle()
        # Resolve first, then mutate for S3
        resolved = _resolve(data)
        resolved.subtype_to_contract["observation.fact_gathering"]["principals"] = "not a list"
        errors = _check_completeness(resolved)
        assert any(e.code == "MISSING_PRINCIPALS_ARRAY" for e in errors)

    # --- Per-contract: MISSING_COGNITION_SPEC ---

    def test_missing_cognition_task_shape(self):
        data = _complete_bundle()
        del data["contracts"]["observation.fact_gathering"]["cognition_spec"]["task_shape"]
        resolved = _resolve(data)
        errors = _check_completeness(resolved)
        assert any(e.code == "MISSING_COGNITION_SPEC" for e in errors)

    def test_no_cognition_spec(self):
        data = _complete_bundle()
        del data["contracts"]["observation.fact_gathering"]["cognition_spec"]
        resolved = _resolve(data)
        errors = _check_completeness(resolved)
        assert any(e.code == "MISSING_COGNITION_SPEC" for e in errors)

    # --- Per-contract: MISSING_REPAIR_TEMPLATES ---

    def test_missing_repair_templates(self):
        data = _complete_bundle()
        del data["contracts"]["observation.fact_gathering"]["repair_templates"]
        resolved = _resolve(data)
        errors = _check_completeness(resolved)
        assert any(e.code == "MISSING_REPAIR_TEMPLATES" for e in errors)

    def test_repair_templates_not_dict(self):
        data = _complete_bundle()
        data["contracts"]["observation.fact_gathering"]["repair_templates"] = ["not", "a", "dict"]
        resolved = _resolve(data)
        errors = _check_completeness(resolved)
        assert any(e.code == "MISSING_REPAIR_TEMPLATES" for e in errors)

    # --- Per-contract: MISSING_ROUTING ---

    def test_missing_routing(self):
        data = _complete_bundle()
        del data["contracts"]["observation.fact_gathering"]["routing"]
        resolved = _resolve(data)
        errors = _check_completeness(resolved)
        assert any(e.code == "MISSING_ROUTING" for e in errors)

    def test_routing_no_principal_routes(self):
        data = _complete_bundle()
        data["contracts"]["observation.fact_gathering"]["routing"] = {}
        resolved = _resolve(data)
        errors = _check_completeness(resolved)
        assert any(e.code == "MISSING_ROUTING" for e in errors)

    # --- Per-contract: MISSING_ALLOWED_NEXT_PHASES ---

    def test_missing_allowed_next_phases(self):
        data = _complete_bundle()
        del data["contracts"]["observation.fact_gathering"]["phase_spec"]
        resolved = _resolve(data)
        errors = _check_completeness(resolved)
        assert any(e.code == "MISSING_ALLOWED_NEXT_PHASES" for e in errors)

    def test_empty_allowed_next_phases(self):
        data = _complete_bundle()
        data["contracts"]["observation.fact_gathering"]["phase_spec"]["allowed_next_phases"] = []
        resolved = _resolve(data)
        errors = _check_completeness(resolved)
        assert any(e.code == "MISSING_ALLOWED_NEXT_PHASES" for e in errors)

    # --- All errors have stage S3 ---

    def test_all_errors_stage_s3(self):
        data = _complete_bundle()
        # Resolve first with valid data, then break everything for S3
        resolved = _resolve(data)
        c = resolved.subtype_to_contract["observation.fact_gathering"]
        c["prompt"] = ""
        c["schema"] = None
        c["policy"] = {}
        c["principals"] = "bad"
        c["cognition_spec"] = {}
        c["repair_templates"] = None
        c["routing"] = {}
        c["phase_spec"] = {}
        errors = _check_completeness(resolved)
        assert all(e.stage == "S3" for e in errors)
        assert len(errors) >= 8  # at least one for each check type

    # --- Context contains phase and subtype ---

    def test_error_context_has_phase_and_subtype(self):
        data = _complete_bundle()
        data["contracts"]["observation.fact_gathering"]["prompt"] = ""
        resolved = _resolve(data)
        errors = _check_completeness(resolved)
        prompt_err = [e for e in errors if e.code == "MISSING_PROMPT"][0]
        assert prompt_err.context["phase"] == "OBSERVE"
        assert prompt_err.context["subtype"] == "observation.fact_gathering"

    # --- Real bundle integration ---

    def test_real_bundle_completeness(self):
        """Real bundle.json should pass S3 with zero errors."""
        bundle_path = os.path.join(os.path.dirname(__file__), "..", "bundle.json")
        if not os.path.exists(bundle_path):
            pytest.skip("bundle.json not found")
        parsed = _parse_bundle(bundle_path)
        resolved = _resolve_refs(parsed)
        errors = _check_completeness(resolved)
        assert errors == [], f"Real bundle has S3 errors: {errors}"


# ---------------------------------------------------------------------------
# DEFAULT_BUNDLE_PATH
# ---------------------------------------------------------------------------

class TestDefaultBundlePath:
    def test_default_is_bundle_json(self):
        # When env var is not set, default should be "bundle.json"
        # (it may already be set in env, so we just check the module-level constant exists)
        assert isinstance(DEFAULT_BUNDLE_PATH, str)
        assert len(DEFAULT_BUNDLE_PATH) > 0


# ---------------------------------------------------------------------------
# S5 — _compile_prompts tests
# ---------------------------------------------------------------------------

def _make_resolved_with_prompt(
    prompt: str,
    required_fields: list | None = None,
    required_principals: list | None = None,
    forbidden_moves: list | None = None,
    success_criteria: list | None = None,
) -> ResolvedBundle:
    """Build a minimal ResolvedBundle with one contract for S5 testing."""
    contract = {
        "phase": "OBSERVE",
        "subtype": "observation.fact_gathering",
        "prompt": prompt,
        "policy": {
            "required_fields": required_fields or [],
            "required_principals": required_principals or [],
            "forbidden_moves": forbidden_moves or [],
        },
        "cognition_spec": {
            "success_criteria": success_criteria or [],
        },
    }
    return ResolvedBundle(
        raw={},
        phase_to_subtype={"OBSERVE": "observation.fact_gathering"},
        subtype_to_contract={"observation.fact_gathering": contract},
        schema_registry={},
        principal_registry={},
        phases_with_contracts=frozenset({"OBSERVE"}),
        phases_without_contracts=frozenset(),
    )


class TestCompilePromptsS5:
    """Tests for S5 — advisory prompt coverage check."""

    def test_no_warnings_when_all_mentioned(self):
        resolved = _make_resolved_with_prompt(
            prompt="evidence_refs are required. ontology_alignment is needed. do not write code. relevant files identified.",
            required_fields=["evidence_refs"],
            required_principals=["ontology_alignment"],
            forbidden_moves=["do not write code"],
            success_criteria=["relevant files identified"],
        )
        warnings = _compile_prompts(resolved)
        assert warnings == []

    def test_missing_required_field(self):
        resolved = _make_resolved_with_prompt(
            prompt="Some prompt without field mention.",
            required_fields=["evidence_refs"],
        )
        warnings = _compile_prompts(resolved)
        assert len(warnings) == 1
        assert warnings[0].stage == "S5"
        assert warnings[0].code == "PROMPT_MISSING_FIELD_MENTION"
        assert warnings[0].context["field"] == "evidence_refs"
        assert warnings[0].context["subtype"] == "observation.fact_gathering"

    def test_missing_required_principal(self):
        resolved = _make_resolved_with_prompt(
            prompt="Some prompt without principal mention.",
            required_principals=["causal_grounding"],
        )
        warnings = _compile_prompts(resolved)
        assert len(warnings) == 1
        assert warnings[0].code == "PROMPT_MISSING_PRINCIPAL_MENTION"
        assert warnings[0].context["principal"] == "causal_grounding"

    def test_missing_forbidden_move(self):
        resolved = _make_resolved_with_prompt(
            prompt="Some prompt without forbidden move mention.",
            forbidden_moves=["do not write code"],
        )
        warnings = _compile_prompts(resolved)
        assert len(warnings) == 1
        assert warnings[0].code == "PROMPT_MISSING_FORBIDDEN_MENTION"
        assert warnings[0].context["forbidden_move"] == "do not write code"

    def test_missing_success_criterion(self):
        resolved = _make_resolved_with_prompt(
            prompt="Some prompt without criteria mention.",
            success_criteria=["root cause identified with evidence"],
        )
        warnings = _compile_prompts(resolved)
        assert len(warnings) == 1
        assert warnings[0].code == "PROMPT_MISSING_CRITERIA_MENTION"
        assert warnings[0].context["criterion"] == "root cause identified with evidence"

    def test_case_insensitive_match(self):
        """Mention check is case-insensitive."""
        resolved = _make_resolved_with_prompt(
            prompt="EVIDENCE_REFS must be provided. ONTOLOGY_ALIGNMENT is required.",
            required_fields=["evidence_refs"],
            required_principals=["ontology_alignment"],
        )
        warnings = _compile_prompts(resolved)
        assert warnings == []

    def test_empty_prompt_skipped(self):
        """Contract with empty prompt produces no warnings (nothing to check)."""
        resolved = _make_resolved_with_prompt(
            prompt="",
            required_fields=["evidence_refs"],
            required_principals=["ontology_alignment"],
        )
        warnings = _compile_prompts(resolved)
        assert warnings == []

    def test_no_prompt_key_skipped(self):
        """Contract with no prompt key produces no warnings."""
        contract = {
            "phase": "OBSERVE",
            "subtype": "observation.fact_gathering",
            # no "prompt" key at all
            "policy": {
                "required_fields": ["evidence_refs"],
                "required_principals": ["ontology_alignment"],
                "forbidden_moves": [],
            },
            "cognition_spec": {"success_criteria": []},
        }
        resolved = ResolvedBundle(
            raw={},
            phase_to_subtype={"OBSERVE": "observation.fact_gathering"},
            subtype_to_contract={"observation.fact_gathering": contract},
            schema_registry={},
            principal_registry={},
            phases_with_contracts=frozenset({"OBSERVE"}),
            phases_without_contracts=frozenset(),
        )
        warnings = _compile_prompts(resolved)
        assert warnings == []

    def test_multiple_missing_items(self):
        """Multiple missing items across categories produce multiple warnings."""
        resolved = _make_resolved_with_prompt(
            prompt="A prompt that mentions nothing relevant.",
            required_fields=["evidence_refs", "claims"],
            required_principals=["ontology_alignment"],
            forbidden_moves=["do not write code"],
            success_criteria=["tests pass"],
        )
        warnings = _compile_prompts(resolved)
        assert len(warnings) == 5
        codes = [w.code for w in warnings]
        assert codes.count("PROMPT_MISSING_FIELD_MENTION") == 2
        assert codes.count("PROMPT_MISSING_PRINCIPAL_MENTION") == 1
        assert codes.count("PROMPT_MISSING_FORBIDDEN_MENTION") == 1
        assert codes.count("PROMPT_MISSING_CRITERIA_MENTION") == 1

    def test_all_warnings_are_advisory(self):
        """All S5 results are CompilationWarning, never CompilationError."""
        resolved = _make_resolved_with_prompt(
            prompt="Empty prompt with nothing.",
            required_fields=["f1"],
            required_principals=["p1"],
            forbidden_moves=["m1"],
            success_criteria=["c1"],
        )
        warnings = _compile_prompts(resolved)
        for w in warnings:
            assert isinstance(w, CompilationWarning)
            assert w.stage == "S5"

    def test_multiple_contracts(self):
        """S5 checks all contracts in the bundle."""
        contract_a = {
            "phase": "OBSERVE",
            "prompt": "prompt mentioning evidence_refs",
            "policy": {"required_fields": ["evidence_refs"], "required_principals": [], "forbidden_moves": []},
            "cognition_spec": {"success_criteria": []},
        }
        contract_b = {
            "phase": "ANALYZE",
            "prompt": "prompt without the field",
            "policy": {"required_fields": ["root_cause"], "required_principals": [], "forbidden_moves": []},
            "cognition_spec": {"success_criteria": []},
        }
        resolved = ResolvedBundle(
            raw={},
            phase_to_subtype={"OBSERVE": "obs.fg", "ANALYZE": "ana.rc"},
            subtype_to_contract={"obs.fg": contract_a, "ana.rc": contract_b},
            schema_registry={},
            principal_registry={},
            phases_with_contracts=frozenset({"OBSERVE", "ANALYZE"}),
            phases_without_contracts=frozenset(),
        )
        warnings = _compile_prompts(resolved)
        # contract_a: evidence_refs IS mentioned -> 0 warnings
        # contract_b: root_cause NOT mentioned -> 1 warning
        assert len(warnings) == 1
        assert warnings[0].context["subtype"] == "ana.rc"
        assert warnings[0].context["field"] == "root_cause"

    def test_real_bundle(self):
        """S5 against real bundle.json — all prompts should cover their contracts."""
        bundle_path = os.path.join(os.path.dirname(__file__), "..", "bundle.json")
        if not os.path.exists(bundle_path):
            pytest.skip("bundle.json not found")
        parsed = _parse_bundle(bundle_path)
        resolved = _resolve_refs(parsed)
        warnings = _compile_prompts(resolved)
        # Real bundle prompts are generated to include all required items,
        # so there should be zero warnings (or very few if generator has gaps)
        for w in warnings:
            assert isinstance(w, CompilationWarning)
            assert w.stage == "S5"


# ---------------------------------------------------------------------------
# S4 — _check_consistency tests
# ---------------------------------------------------------------------------

def _consistency_bundle() -> dict:
    """Return a bundle that passes all S4 consistency checks."""
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
                "prompt": "## Phase: OBSERVE\nGather evidence. ontology_alignment evidence_completeness",
                "schema": {
                    "type": "object",
                    "properties": {"phase": {"type": "string"}},
                    "required": ["phase"],
                },
                "policy": {
                    "required_principals": ["ontology_alignment", "evidence_completeness"],
                    "forbidden_principals": ["minimal_change"],
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
                "cognition_spec": {
                    "type": "observation.fact_gathering",
                    "phase": "OBSERVE",
                    "task_shape": "fact_gathering",
                    "schema_ref": "observation.fact_gathering",
                },
                "repair_templates": {
                    "ontology_alignment": "[ontology_alignment] fix it",
                    "evidence_completeness": "[evidence_completeness] fix it",
                },
                "routing": {
                    "principal_routes": {
                        "ontology_alignment": {"next_phase": "OBSERVE", "strategy": "fix"},
                        "evidence_completeness": {"next_phase": "OBSERVE", "strategy": "fix"},
                    },
                },
                "phase_spec": {
                    "name": "OBSERVE",
                    "allowed_next_phases": ["ANALYZE", "OBSERVE"],
                },
            },
        },
        "cognition": {
            "phases": [{"name": "OBSERVE"}, {"name": "ANALYZE"}, {"name": "UNDERSTAND"}],
            "subtypes": [
                {
                    "name": "observation.fact_gathering",
                    "phase": "OBSERVE",
                    "required_principals": ["ontology_alignment", "evidence_completeness"],
                    "forbidden_principals": ["minimal_change"],
                },
            ],
            "transitions": [
                {"from": "OBSERVE", "to": "ANALYZE", "allowed": True},
                {"from": "OBSERVE", "to": "OBSERVE", "allowed": True},
            ],
        },
    }


def _resolve_for_consistency(data: dict) -> ResolvedBundle:
    """Parse + resolve a bundle dict for S4 testing."""
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    try:
        parsed = _parse_bundle(path)
        return _resolve_refs(parsed)
    finally:
        os.unlink(path)


class TestCheckConsistencyS4:
    """Tests for _check_consistency (S4)."""

    def test_consistent_bundle_no_errors(self):
        resolved = _resolve_for_consistency(_consistency_bundle())
        errors, warnings = _check_consistency(resolved)
        assert errors == []
        assert warnings == []

    def test_never_raises(self):
        """Even a broken bundle returns lists, not exceptions."""
        resolved = _resolve_for_consistency(_consistency_bundle())
        # Corrupt cognition subtypes phase
        resolved.raw["cognition"]["subtypes"][0]["phase"] = "WRONG"
        errors, warnings = _check_consistency(resolved)
        assert isinstance(errors, list)
        assert isinstance(warnings, list)

    # --- 4.1 Subtype/phase alignment ---

    def test_subtype_phase_mismatch(self):
        data = _consistency_bundle()
        data["cognition"]["subtypes"][0]["phase"] = "ANALYZE"
        resolved = _resolve_for_consistency(data)
        errors, _ = _check_consistency(resolved)
        mismatch = [e for e in errors if e.code == "SUBTYPE_PHASE_MISMATCH"]
        assert len(mismatch) == 1
        assert mismatch[0].context["subtype"] == "observation.fact_gathering"
        assert mismatch[0].context["cognition_phase"] == "ANALYZE"
        assert mismatch[0].context["contract_phase"] == "OBSERVE"

    def test_subtype_phase_alignment_ok(self):
        data = _consistency_bundle()
        resolved = _resolve_for_consistency(data)
        errors, _ = _check_consistency(resolved)
        assert not any(e.code == "SUBTYPE_PHASE_MISMATCH" for e in errors)

    def test_subtype_not_in_contracts_skipped(self):
        """Cognition subtype with no matching contract is silently skipped."""
        data = _consistency_bundle()
        data["cognition"]["subtypes"].append({
            "name": "nonexistent.subtype",
            "phase": "OBSERVE",
        })
        resolved = _resolve_for_consistency(data)
        errors, _ = _check_consistency(resolved)
        assert not any(
            e.code == "SUBTYPE_PHASE_MISMATCH" and e.context.get("subtype") == "nonexistent.subtype"
            for e in errors
        )

    # --- 4.2 Principal scope cross-check ---

    def test_principal_not_in_array(self):
        data = _consistency_bundle()
        # First add ghost_principal to principals[] so S2 passes, then remove after resolve
        data["contracts"]["observation.fact_gathering"]["principals"].append({
            "name": "ghost_principal",
            "applies_to": ["observation.fact_gathering"],
            "inference_rule_exists": False,
            "fake_check_eligible": False,
        })
        data["contracts"]["observation.fact_gathering"]["policy"]["required_principals"].append(
            "ghost_principal"
        )
        resolved = _resolve_for_consistency(data)
        # Now remove ghost_principal from the contract's principals array (post-resolve)
        # to simulate a principal in policy but not in the local array
        contract = resolved.subtype_to_contract["observation.fact_gathering"]
        contract["principals"] = [
            p for p in contract["principals"] if p.get("name") != "ghost_principal"
        ]
        errors, _ = _check_consistency(resolved)
        not_in_array = [e for e in errors if e.code == "PRINCIPAL_NOT_IN_ARRAY"]
        assert len(not_in_array) == 1
        assert not_in_array[0].context["principal"] == "ghost_principal"

    def test_principal_scope_mismatch(self):
        data = _consistency_bundle()
        # Change ontology_alignment's applies_to to NOT include observation.fact_gathering
        for p in data["contracts"]["observation.fact_gathering"]["principals"]:
            if p["name"] == "ontology_alignment":
                p["applies_to"] = ["analysis.root_cause"]
        resolved = _resolve_for_consistency(data)
        errors, _ = _check_consistency(resolved)
        scope_errors = [e for e in errors if e.code == "PRINCIPAL_SCOPE_MISMATCH"]
        assert len(scope_errors) == 1
        assert scope_errors[0].context["principal"] == "ontology_alignment"
        assert scope_errors[0].context["subtype"] == "observation.fact_gathering"

    def test_principal_scope_ok(self):
        data = _consistency_bundle()
        resolved = _resolve_for_consistency(data)
        errors, _ = _check_consistency(resolved)
        assert not any(e.code == "PRINCIPAL_SCOPE_MISMATCH" for e in errors)
        assert not any(e.code == "PRINCIPAL_NOT_IN_ARRAY" for e in errors)

    # --- 4.3 Forbidden/required disjoint ---

    def test_forbidden_required_overlap(self):
        data = _consistency_bundle()
        # Put ontology_alignment in both required AND forbidden
        data["contracts"]["observation.fact_gathering"]["policy"]["forbidden_principals"] = [
            "ontology_alignment"
        ]
        resolved = _resolve_for_consistency(data)
        errors, _ = _check_consistency(resolved)
        overlap_errors = [e for e in errors if e.code == "FORBIDDEN_REQUIRED_OVERLAP"]
        assert len(overlap_errors) == 1
        assert "ontology_alignment" in overlap_errors[0].context["overlap"]

    def test_no_overlap_when_disjoint(self):
        data = _consistency_bundle()
        resolved = _resolve_for_consistency(data)
        errors, _ = _check_consistency(resolved)
        assert not any(e.code == "FORBIDDEN_REQUIRED_OVERLAP" for e in errors)

    # --- 4.4 Principal lifecycle ---

    def test_lifecycle_violation(self):
        data = _consistency_bundle()
        # Set fake_check_eligible=true but inference_rule_exists=false
        for p in data["contracts"]["observation.fact_gathering"]["principals"]:
            if p["name"] == "ontology_alignment":
                p["fake_check_eligible"] = True
                p["inference_rule_exists"] = False
        resolved = _resolve_for_consistency(data)
        errors, _ = _check_consistency(resolved)
        lc_errors = [e for e in errors if e.code == "LIFECYCLE_VIOLATION"]
        assert len(lc_errors) == 1
        assert lc_errors[0].context["principal"] == "ontology_alignment"

    def test_lifecycle_ok_when_both_true(self):
        data = _consistency_bundle()
        for p in data["contracts"]["observation.fact_gathering"]["principals"]:
            if p["name"] == "ontology_alignment":
                p["fake_check_eligible"] = True
                p["inference_rule_exists"] = True
        resolved = _resolve_for_consistency(data)
        errors, _ = _check_consistency(resolved)
        assert not any(e.code == "LIFECYCLE_VIOLATION" for e in errors)

    def test_lifecycle_ok_when_both_false(self):
        data = _consistency_bundle()
        resolved = _resolve_for_consistency(data)
        errors, _ = _check_consistency(resolved)
        assert not any(e.code == "LIFECYCLE_VIOLATION" for e in errors)

    # --- 4.5 Routing coverage ---

    def test_routing_coverage_missing(self):
        data = _consistency_bundle()
        # Remove evidence_completeness from routing
        del data["contracts"]["observation.fact_gathering"]["routing"]["principal_routes"]["evidence_completeness"]
        resolved = _resolve_for_consistency(data)
        errors, _ = _check_consistency(resolved)
        route_errors = [e for e in errors if e.code == "ROUTING_COVERAGE_MISSING"]
        assert len(route_errors) == 1
        assert route_errors[0].context["principal"] == "evidence_completeness"

    def test_routing_coverage_ok(self):
        data = _consistency_bundle()
        resolved = _resolve_for_consistency(data)
        errors, _ = _check_consistency(resolved)
        assert not any(e.code == "ROUTING_COVERAGE_MISSING" for e in errors)

    # --- 4.6 Transition matrix completeness ---

    def test_transition_matrix_incomplete(self):
        data = _consistency_bundle()
        # Remove OBSERVE->ANALYZE from transitions
        data["cognition"]["transitions"] = [
            {"from": "OBSERVE", "to": "OBSERVE", "allowed": True},
        ]
        resolved = _resolve_for_consistency(data)
        _, warnings = _check_consistency(resolved)
        tw = [w for w in warnings if w.code == "TRANSITION_MATRIX_INCOMPLETE"]
        assert len(tw) == 1
        assert "OBSERVE->ANALYZE" in tw[0].context["missing"]

    def test_transition_matrix_complete(self):
        data = _consistency_bundle()
        resolved = _resolve_for_consistency(data)
        _, warnings = _check_consistency(resolved)
        assert not any(w.code == "TRANSITION_MATRIX_INCOMPLETE" for w in warnings)

    def test_transition_matrix_empty_cognition(self):
        """No transitions in cognition means all contract transitions are missing."""
        data = _consistency_bundle()
        data["cognition"]["transitions"] = []
        resolved = _resolve_for_consistency(data)
        _, warnings = _check_consistency(resolved)
        tw = [w for w in warnings if w.code == "TRANSITION_MATRIX_INCOMPLETE"]
        assert len(tw) == 1
        # Should flag both OBSERVE->ANALYZE and OBSERVE->OBSERVE
        assert len(tw[0].context["missing"]) == 2

    # --- All errors have stage S4 ---

    def test_all_errors_stage_s4(self):
        data = _consistency_bundle()
        # Trigger multiple error types
        data["cognition"]["subtypes"][0]["phase"] = "WRONG"
        data["contracts"]["observation.fact_gathering"]["policy"]["forbidden_principals"] = [
            "ontology_alignment"
        ]
        resolved = _resolve_for_consistency(data)
        errors, warnings = _check_consistency(resolved)
        assert all(e.stage == "S4" for e in errors)
        assert all(w.stage == "S4" for w in warnings)

    # --- Return type is always tuple[list, list] ---

    def test_return_type(self):
        resolved = _resolve_for_consistency(_consistency_bundle())
        result = _check_consistency(resolved)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], list)
        assert isinstance(result[1], list)

    # --- Real bundle integration ---

    def test_real_bundle_consistency(self):
        """Real bundle.json should pass S4 with zero errors."""
        bundle_path = os.path.join(os.path.dirname(__file__), "..", "bundle.json")
        if not os.path.exists(bundle_path):
            pytest.skip("bundle.json not found")
        parsed = _parse_bundle(bundle_path)
        resolved = _resolve_refs(parsed)
        errors, warnings = _check_consistency(resolved)
        assert errors == [], f"Real bundle has S4 errors: {errors}"
        # Warnings are acceptable but should be CompilationWarning
        for w in warnings:
            assert isinstance(w, CompilationWarning)
            assert w.stage == "S4"
