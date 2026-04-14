"""
test_contract_definitions.py — Parameterized validation of all cognition contract modules.

Verifies that every contract module satisfies the ContractDefinition protocol
via validate_contract_definition() and additional structural checks.
"""

import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from cognition_contracts._base import validate_contract_definition, FieldSpec, GateRule

# ── Collect all contract modules ──────────────────────────────────────────────

CONTRACT_MODULES = []

_MODULE_NAMES = [
    "analysis_root_cause",
    "observation_fact_gathering",
    "design_solution_shape",
    "decision_fix_direction",
    "execution_code_patch",
    "judge_verification",
    "implementation_governance",
]

# Only include modules that implement the standard ContractDefinition protocol
# (have FIELD_SPECS, GATE_RULES, SCHEMA_PROPERTIES, etc.)
_STANDARD_ATTRS = {"FIELD_SPECS", "GATE_RULES", "SCHEMA_PROPERTIES", "SCHEMA_REQUIRED", "PHASE", "SUBTYPE"}

for _name in _MODULE_NAMES:
    try:
        _mod = __import__(f"cognition_contracts.{_name}", fromlist=[_name])
        if _STANDARD_ATTRS.issubset(set(dir(_mod))):
            CONTRACT_MODULES.append((_name, _mod))
    except ImportError:
        pass

# Ensure we have at least one module to test
assert len(CONTRACT_MODULES) > 0, "No contract modules found — check sys.path"


# ── Parameterized tests ──────────────────────────────────────────────────────

@pytest.mark.parametrize("name,module", CONTRACT_MODULES, ids=[c[0] for c in CONTRACT_MODULES])
class TestContractDefinition:
    """Validate each contract module against the ContractDefinition protocol."""

    def test_validate_no_errors(self, name, module):
        """validate_contract_definition() returns empty list for valid contracts."""
        errors = validate_contract_definition(module)
        assert errors == [], f"{name} has validation errors: {errors}"

    def test_field_specs_unique_names(self, name, module):
        """FIELD_SPECS must not contain duplicate field names."""
        names = [f.name for f in module.FIELD_SPECS]
        assert len(names) == len(set(names)), f"{name} has duplicate FIELD_SPECS names"

    def test_gate_rules_reference_valid_fields(self, name, module):
        """Every GateRule.field must reference a field defined in FIELD_SPECS."""
        field_names = {f.name for f in module.FIELD_SPECS}
        for rule in module.GATE_RULES:
            assert rule.field in field_names, (
                f"[{name}] gate rule '{rule.name}' references "
                f"'{rule.field}' not in FIELD_SPECS"
            )

    def test_schema_required_subset_properties(self, name, module):
        """Every SCHEMA_REQUIRED key must exist in SCHEMA_PROPERTIES."""
        for req in module.SCHEMA_REQUIRED:
            assert req in module.SCHEMA_PROPERTIES, (
                f"[{name}] '{req}' in SCHEMA_REQUIRED but not in SCHEMA_PROPERTIES"
            )

    def test_phase_is_nonempty_string(self, name, module):
        """PHASE must be a non-empty uppercase string."""
        assert isinstance(module.PHASE, str)
        assert len(module.PHASE.strip()) > 0

    def test_subtype_matches_pattern(self, name, module):
        """SUBTYPE must match <category>.<task_shape> pattern."""
        import re
        assert re.match(r"^[a-z]+\.[a-z_]+$", module.SUBTYPE), (
            f"[{name}] SUBTYPE '{module.SUBTYPE}' does not match pattern"
        )

    def test_gate_threshold_in_range(self, name, module):
        """GATE_THRESHOLD must be in (0.0, 1.0]."""
        assert 0.0 < module.GATE_THRESHOLD <= 1.0

    def test_allowed_next_nonempty(self, name, module):
        """ALLOWED_NEXT must have at least one entry."""
        assert len(module.ALLOWED_NEXT) > 0

    def test_required_forbidden_disjoint(self, name, module):
        """REQUIRED_PRINCIPALS and FORBIDDEN_PRINCIPALS must not overlap."""
        overlap = set(module.REQUIRED_PRINCIPALS) & set(module.FORBIDDEN_PRINCIPALS)
        assert len(overlap) == 0, f"[{name}] overlap: {overlap}"

    def test_gate_rules_have_repair_hints(self, name, module):
        """Every GateRule must have a non-empty repair_hint."""
        for rule in module.GATE_RULES:
            assert rule.repair_hint and len(rule.repair_hint.strip()) > 0, (
                f"[{name}] gate rule '{rule.name}' has empty repair_hint"
            )

    def test_required_field_specs_in_schema(self, name, module):
        """Every required FieldSpec must have a corresponding SCHEMA_PROPERTIES key."""
        prop_keys = set(module.SCHEMA_PROPERTIES.keys())
        for fs in module.FIELD_SPECS:
            if fs.required:
                assert fs.name in prop_keys, (
                    f"[{name}] required FieldSpec '{fs.name}' missing from SCHEMA_PROPERTIES"
                )
