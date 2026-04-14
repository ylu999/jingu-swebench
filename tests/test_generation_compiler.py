"""
test_generation_compiler.py — Tests for compile_contract() and validate_round_trip().

Verifies the compiler transforms ContractDefinition modules into
valid BundleContractOutput with all 8 sections populated.
"""

import sys
import os
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from cognition_contracts._compiler import (
    compile_contract,
    CompilationError,
    BundleContractOutput,
    validate_round_trip,
)
from cognition_contracts import analysis_root_cause


class TestCompileContract:
    """Tests for compile_contract()."""

    def test_compile_produces_valid_output(self):
        """compile_contract on a valid module returns BundleContractOutput."""
        output = compile_contract(analysis_root_cause)
        assert isinstance(output, BundleContractOutput)

    def test_all_sections_populated(self):
        """Compiled output has all 8 sections non-empty."""
        output = compile_contract(analysis_root_cause)
        assert output.phase_spec, "phase_spec is empty"
        assert output.cognition_spec, "cognition_spec is empty"
        assert output.principals, "principals is empty"
        assert output.policy, "policy is empty"
        assert output.schema, "schema is empty"
        assert output.prompt, "prompt is empty"
        assert output.repair_templates, "repair_templates is empty"
        assert output.routing, "routing is empty"

    def test_phase_spec_content(self):
        """phase_spec contains expected keys."""
        output = compile_contract(analysis_root_cause)
        ps = output.phase_spec
        assert ps["name"] == "ANALYZE"
        assert "goal" in ps
        assert "forbidden_moves" in ps
        assert "allowed_next_phases" in ps
        assert set(ps["allowed_next_phases"]) == {"DECIDE", "ANALYZE", "OBSERVE"}

    def test_schema_has_properties_and_required(self):
        """schema section has 'properties' and 'required' keys."""
        output = compile_contract(analysis_root_cause)
        assert "properties" in output.schema
        assert "required" in output.schema
        assert "root_cause" in output.schema["properties"]

    def test_policy_has_required_fields(self):
        """policy section lists required_fields and required_principals."""
        output = compile_contract(analysis_root_cause)
        assert "required_fields" in output.policy
        assert "required_principals" in output.policy
        assert "causal_grounding" in output.policy["required_principals"]

    def test_repair_templates_match_gate_rules(self):
        """repair_templates has one entry per gate rule."""
        output = compile_contract(analysis_root_cause)
        rule_names = {r.name for r in analysis_root_cause.GATE_RULES}
        template_names = set(output.repair_templates.keys())
        assert rule_names == template_names

    def test_prompt_contains_phase_name(self):
        """Compiled prompt mentions the phase."""
        output = compile_contract(analysis_root_cause)
        assert "ANALYZE" in output.prompt

    def test_invalid_module_raises_compilation_error(self):
        """compile_contract raises CompilationError for an invalid module."""
        bad_module = types.ModuleType("bad")
        with pytest.raises(CompilationError) as exc_info:
            compile_contract(bad_module)
        assert len(exc_info.value.errors) > 0

    def test_compile_with_custom_phase_goals(self):
        """Custom phase_goals override the default."""
        custom_goals = {"ANALYZE": "Custom analysis goal."}
        output = compile_contract(analysis_root_cause, phase_goals=custom_goals)
        assert output.phase_spec["goal"] == "Custom analysis goal."

    def test_compile_with_principal_registry(self):
        """principal_registry enriches principal entries."""
        registry = {
            "causal_grounding": {
                "inference_rule_exists": True,
                "fake_check_eligible": True,
                "repair_hint": "Provide causal grounding.",
            }
        }
        output = compile_contract(analysis_root_cause, principal_registry=registry)
        cg = next(p for p in output.principals if p["name"] == "causal_grounding")
        assert cg["inference_rule_exists"] is True
        assert cg["fake_check_eligible"] is True


class TestValidateRoundTrip:
    """Tests for validate_round_trip()."""

    def test_matching_bundle_no_mismatches(self):
        """A bundle section compiled from the same contract has zero mismatches."""
        output = compile_contract(analysis_root_cause)
        # Build a bundle section dict from the compiled output
        bundle_section = {
            "schema": output.schema,
            "policy": output.policy,
            "phase_spec": output.phase_spec,
        }
        mismatches = validate_round_trip(analysis_root_cause, bundle_section)
        assert mismatches == []

    def test_drifted_schema_detected(self):
        """A bundle section with different schema.required triggers mismatch."""
        output = compile_contract(analysis_root_cause)
        bundle_section = {
            "schema": {
                "properties": output.schema["properties"],
                "required": ["phase"],  # subset of actual required
            },
            "policy": output.policy,
            "phase_spec": output.phase_spec,
        }
        mismatches = validate_round_trip(analysis_root_cause, bundle_section)
        assert len(mismatches) > 0
        assert any("schema.required" in m for m in mismatches)

    def test_drifted_policy_detected(self):
        """A bundle section with different policy.required_fields triggers mismatch."""
        output = compile_contract(analysis_root_cause)
        bundle_section = {
            "schema": output.schema,
            "policy": {
                **output.policy,
                "required_fields": ["root_cause"],  # missing others
            },
            "phase_spec": output.phase_spec,
        }
        mismatches = validate_round_trip(analysis_root_cause, bundle_section)
        assert len(mismatches) > 0
        assert any("policy.required_fields" in m for m in mismatches)
