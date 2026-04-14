"""
test_drift_audit.py — Tests for drift_audit layer-alignment checks.

Tests each of the 5 drift checks with synthetic contract data,
plus the audit_contract and audit_all_contracts entry points.
"""

import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from drift_audit import (
    DriftViolation,
    audit_contract,
    audit_all_contracts,
    check_gate_fields_subset_schema,
    check_schema_fields_subset_prompt,
    check_prompt_fields_subset_schema,
    check_extractor_fields_subset_record,
    check_policy_principals_subset_contract,
)


def _make_contract(
    schema_properties=None,
    policy_required_fields=None,
    policy_required_principals=None,
    principals=None,
    prompt="",
    repair_templates=None,
    routing=None,
):
    """Build a synthetic contract dict for testing."""
    return {
        "schema": {
            "properties": schema_properties or {},
            "required": list((schema_properties or {}).keys()),
        },
        "policy": {
            "required_fields": policy_required_fields or [],
            "required_principals": policy_required_principals or [],
        },
        "principals": principals or [],
        "prompt": prompt,
        "repair_templates": repair_templates or {},
        "routing": {"principal_routes": routing or {}},
    }


class TestCheckGateFieldsSubsetSchema:
    """Check 1: gate fields must exist in schema."""

    def test_no_violation_when_aligned(self):
        contract = _make_contract(
            schema_properties={"root_cause": {"type": "string"}},
            policy_required_fields=["root_cause"],
        )
        violations = check_gate_fields_subset_schema("test.subtype", contract)
        assert violations == []

    def test_ghost_field_detected(self):
        """Policy requires a field not in schema -> violation."""
        contract = _make_contract(
            schema_properties={"root_cause": {"type": "string"}},
            policy_required_fields=["root_cause", "ghost_field"],
        )
        violations = check_gate_fields_subset_schema("test.subtype", contract)
        assert len(violations) == 1
        assert violations[0].check_name == "gate_fields_subset_schema"
        assert violations[0].item == "ghost_field"
        assert violations[0].violation_type == "missing_in_b"

    def test_principal_requires_field_not_in_schema(self):
        """Principal requires_fields referencing non-schema field -> violation."""
        contract = _make_contract(
            schema_properties={"root_cause": {"type": "string"}},
            principals=[{"name": "causal_grounding", "requires_fields": ["nonexistent"]}],
        )
        violations = check_gate_fields_subset_schema("test.subtype", contract)
        assert len(violations) == 1
        assert violations[0].item == "nonexistent"


class TestCheckSchemaFieldsSubsetPrompt:
    """Check 2: schema fields should have descriptions."""

    def test_no_violation_when_all_described(self):
        contract = _make_contract(
            schema_properties={
                "root_cause": {"type": "string", "description": "The root cause."},
            }
        )
        violations = check_schema_fields_subset_prompt("test.subtype", contract)
        assert violations == []

    def test_missing_description_detected(self):
        """Schema field without description -> violation."""
        contract = _make_contract(
            schema_properties={
                "root_cause": {"type": "string"},  # no description
            }
        )
        violations = check_schema_fields_subset_prompt("test.subtype", contract)
        assert len(violations) == 1
        assert violations[0].item == "root_cause"

    def test_universal_fields_skipped(self):
        """Universal fields (phase, subtype, principals) don't need descriptions."""
        contract = _make_contract(
            schema_properties={
                "phase": {"type": "string"},  # universal, no description
                "subtype": {"type": "string"},
                "principals": {"type": "array"},
            }
        )
        violations = check_schema_fields_subset_prompt("test.subtype", contract)
        assert violations == []


class TestCheckPromptFieldsSubsetSchema:
    """Check 3: prompt should not reference fields absent from schema."""

    def test_no_violation_when_aligned(self):
        contract = _make_contract(
            schema_properties={"root_cause": {"type": "string"}},
            prompt="## Required Fields\n- root_cause\n",
        )
        violations = check_prompt_fields_subset_schema("test.subtype", contract)
        assert violations == []

    def test_prompt_referencing_nonschema_field(self):
        """Prompt mentions a field not in schema -> violation."""
        contract = _make_contract(
            schema_properties={"root_cause": {"type": "string"}},
            prompt="## Required Fields\n- root_cause\n- phantom_field\n",
        )
        violations = check_prompt_fields_subset_schema("test.subtype", contract)
        # phantom_field should be flagged (if not in false positives)
        phantom_violations = [v for v in violations if v.item == "phantom_field"]
        assert len(phantom_violations) == 1


class TestCheckExtractorFieldsSubsetRecord:
    """Check 4: schema fields must exist as PhaseRecord attributes."""

    def test_known_fields_pass(self):
        """Schema fields that exist in PhaseRecord should not flag."""
        # PhaseRecord has 'phase' and 'subtype' at minimum
        contract = _make_contract(
            schema_properties={"phase": {"type": "string"}}
        )
        violations = check_extractor_fields_subset_record("test.subtype", contract)
        assert violations == []

    def test_unknown_field_flagged(self):
        """Schema field not in PhaseRecord -> violation."""
        contract = _make_contract(
            schema_properties={"totally_made_up_field_xyz": {"type": "string"}}
        )
        violations = check_extractor_fields_subset_record("test.subtype", contract)
        assert len(violations) == 1
        assert violations[0].item == "totally_made_up_field_xyz"


class TestCheckPolicyPrincipalsSubsetContract:
    """Check 5: policy principals must be in contract principals array."""

    def test_no_violation_when_aligned(self):
        contract = _make_contract(
            policy_required_principals=["causal_grounding"],
            principals=[{"name": "causal_grounding"}],
            repair_templates={"causal_grounding": {"hint": "fix it"}},
            routing={"causal_grounding": "root_cause"},
        )
        violations = check_policy_principals_subset_contract("test.subtype", contract)
        assert violations == []

    def test_missing_principal_detected(self):
        """Policy references a principal not in the principals array."""
        contract = _make_contract(
            policy_required_principals=["ghost_principal"],
            principals=[],
        )
        violations = check_policy_principals_subset_contract("test.subtype", contract)
        # Should have violations for missing in contract, repair_templates, routing
        assert any(v.item == "ghost_principal" for v in violations)

    def test_missing_repair_template_detected(self):
        """Policy principal missing from repair_templates -> violation."""
        contract = _make_contract(
            policy_required_principals=["causal_grounding"],
            principals=[{"name": "causal_grounding"}],
            repair_templates={},  # no templates
            routing={"causal_grounding": "root_cause"},
        )
        violations = check_policy_principals_subset_contract("test.subtype", contract)
        repair_violations = [
            v for v in violations if v.layer_b == "repair_templates"
        ]
        assert len(repair_violations) == 1


class TestAuditContract:
    """Tests for the audit_contract entry point."""

    def test_clean_contract_no_violations(self):
        """A well-aligned contract produces no violations (except possibly extractor)."""
        contract = _make_contract(
            schema_properties={
                "phase": {"type": "string", "description": "Phase."},
                "subtype": {"type": "string", "description": "Subtype."},
            },
            policy_required_fields=["phase"],
        )
        violations = audit_contract("test.subtype", contract)
        # Filter out extractor violations (PhaseRecord-dependent)
        non_extractor = [
            v for v in violations
            if v.check_name != "extractor_fields_subset_record"
        ]
        assert non_extractor == []


class TestAuditAllContracts:
    """Tests for audit_all_contracts entry point."""

    def test_empty_bundle(self):
        """Empty bundle produces no violations."""
        violations = audit_all_contracts({"contracts": {}})
        assert violations == []

    def test_multiple_subtypes(self):
        """Each subtype in the bundle is audited."""
        bundle = {
            "contracts": {
                "a.b": _make_contract(
                    schema_properties={"phase": {"type": "string", "description": "P"}},
                    policy_required_fields=["ghost"],
                ),
                "c.d": _make_contract(
                    schema_properties={"phase": {"type": "string", "description": "P"}},
                ),
            }
        }
        violations = audit_all_contracts(bundle)
        # At least the ghost field violation from a.b
        ab_violations = [v for v in violations if "a.b" in v.detail]
        assert len(ab_violations) > 0
