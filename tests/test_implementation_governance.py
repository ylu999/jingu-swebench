"""
test_implementation_governance.py — Verify implementation governance SST module.

Tests that the cognition_contracts/implementation_governance.py module:
1. Exports all 5 subtypes
2. Exports all 6 principals
3. Exports all 6 policies
4. Has a complete failure taxonomy (5 entries)
5. Has repair routing covering all failure types
6. Module constants match the design doc values exactly
"""

import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from cognition_contracts.implementation_governance import (
    PHASE,
    SUBTYPES,
    PRINCIPALS,
    POLICIES,
    POLICY_MAP,
    FAILURE_TAXONOMY,
    REPAIR_ROUTING,
    ALLOWED_NEXT,
    REPAIR_TARGET,
    get_subtype_contract_entry,
)


# -- Phase identity -----------------------------------------------------------


class TestPhaseIdentity:
    """PHASE constant is correct."""

    def test_phase_name(self):
        assert PHASE == "IMPLEMENTATION_GOVERNANCE"


# -- Subtypes -----------------------------------------------------------------


class TestSubtypes:
    """All 5 subtypes are defined and correctly named."""

    EXPECTED_SUBTYPES = [
        "implementation.source_of_truth_identification",
        "implementation.injection_path_audit",
        "implementation.projection_derivation",
        "implementation.cross_surface_consistency",
        "implementation.shadow_contract_detection",
    ]

    def test_subtype_count(self):
        assert len(SUBTYPES) == 5

    @pytest.mark.parametrize("subtype", EXPECTED_SUBTYPES)
    def test_subtype_present(self, subtype):
        assert subtype in SUBTYPES, f"Missing subtype: {subtype}"

    def test_all_subtypes_start_with_implementation(self):
        for s in SUBTYPES:
            assert s.startswith("implementation."), f"Subtype {s!r} does not start with 'implementation.'"

    def test_no_duplicate_subtypes(self):
        assert len(SUBTYPES) == len(set(SUBTYPES))


# -- Principals ---------------------------------------------------------------


class TestPrincipals:
    """All 6 principals are defined and correctly named."""

    EXPECTED_PRINCIPALS = [
        "single_source_of_truth_preservation",
        "projection_not_definition",
        "injection_path_accountability",
        "cross_surface_consistency",
        "no_shadow_contracts",
        "compile_over_handwrite",
    ]

    def test_principal_count(self):
        assert len(PRINCIPALS) == 6

    @pytest.mark.parametrize("principal", EXPECTED_PRINCIPALS)
    def test_principal_present(self, principal):
        assert principal in PRINCIPALS, f"Missing principal: {principal}"

    def test_no_duplicate_principals(self):
        assert len(PRINCIPALS) == len(set(PRINCIPALS))


# -- Policies -----------------------------------------------------------------


class TestPolicies:
    """All 6 policies are defined with required fields."""

    EXPECTED_POLICY_IDS = [
        "canonical_source_required",
        "projection_files_cannot_define_contracts",
        "injection_path_must_be_traceable",
        "cross_surface_delta_zero",
        "shadow_contract_ci_detection",
        "compile_time_projection_verification",
    ]

    def test_policy_count(self):
        assert len(POLICIES) == 6

    @pytest.mark.parametrize("policy_id", EXPECTED_POLICY_IDS)
    def test_policy_present(self, policy_id):
        assert policy_id in POLICY_MAP, f"Missing policy: {policy_id}"

    def test_all_policies_have_required_fields(self):
        for p in POLICIES:
            assert "id" in p, f"Policy missing 'id': {p}"
            assert "rule" in p, f"Policy missing 'rule': {p}"
            assert "description" in p, f"Policy missing 'description': {p}"
            assert len(p["id"]) > 0
            assert len(p["rule"]) > 0
            assert len(p["description"]) > 0

    def test_policy_map_matches_list(self):
        assert len(POLICY_MAP) == len(POLICIES)
        for p in POLICIES:
            assert POLICY_MAP[p["id"]] is p


# -- Failure taxonomy ---------------------------------------------------------


class TestFailureTaxonomy:
    """Failure taxonomy has 5 entries with correct structure."""

    EXPECTED_FAILURE_TYPES = [
        "shadow_contract_detected",
        "projection_drift",
        "injection_gap",
        "cross_surface_inconsistency",
        "missing_canonical_source",
    ]

    def test_failure_count(self):
        assert len(FAILURE_TAXONOMY) == 5

    @pytest.mark.parametrize("failure_type", EXPECTED_FAILURE_TYPES)
    def test_failure_type_present(self, failure_type):
        assert failure_type in FAILURE_TAXONOMY, f"Missing failure type: {failure_type}"

    def test_all_entries_have_trigger_and_repair_target(self):
        for name, entry in FAILURE_TAXONOMY.items():
            assert "trigger" in entry, f"Failure {name!r} missing 'trigger'"
            assert "repair_target" in entry, f"Failure {name!r} missing 'repair_target'"
            assert len(entry["trigger"]) > 0
            assert len(entry["repair_target"]) > 0


# -- Repair routing -----------------------------------------------------------


class TestRepairRouting:
    """Repair routing covers all failure types."""

    def test_repair_routing_covers_all_failures(self):
        for failure_type in FAILURE_TAXONOMY:
            assert failure_type in REPAIR_ROUTING, (
                f"Failure type {failure_type!r} has no repair routing"
            )

    def test_repair_routing_no_extra_entries(self):
        for key in REPAIR_ROUTING:
            assert key in FAILURE_TAXONOMY, (
                f"Repair routing has entry {key!r} not in failure taxonomy"
            )

    def test_all_routing_values_non_empty(self):
        for key, value in REPAIR_ROUTING.items():
            assert len(value) > 0, f"Repair routing for {key!r} is empty"


# -- Phase transitions --------------------------------------------------------


class TestPhaseTransitions:
    """Implementation governance is self-referencing (meta-governance layer)."""

    def test_allowed_next_is_self(self):
        assert ALLOWED_NEXT == ["IMPLEMENTATION_GOVERNANCE"]

    def test_repair_target_is_self(self):
        assert REPAIR_TARGET == "IMPLEMENTATION_GOVERNANCE"


# -- Subtype contract helper --------------------------------------------------


class TestGetSubtypeContractEntry:
    """get_subtype_contract_entry() produces valid SubtypeContract dicts."""

    def test_all_subtypes_produce_valid_entry(self):
        for subtype in SUBTYPES:
            entry = get_subtype_contract_entry(subtype)
            assert entry["phase"] == PHASE
            assert entry["required_principals"] == []
            assert entry["forbidden_principals"] == []
            assert entry["required_fields"] == []
            assert entry["allowed_next"] == ["IMPLEMENTATION_GOVERNANCE"]
            assert entry["repair_target"] == "IMPLEMENTATION_GOVERNANCE"

    def test_expected_principals_non_empty(self):
        """Each subtype has at least one expected principal."""
        for subtype in SUBTYPES:
            entry = get_subtype_contract_entry(subtype)
            assert len(entry["expected_principals"]) > 0, (
                f"Subtype {subtype!r} has no expected principals"
            )

    def test_expected_principals_are_valid(self):
        """All expected principals are from the PRINCIPALS list."""
        for subtype in SUBTYPES:
            entry = get_subtype_contract_entry(subtype)
            for p in entry["expected_principals"]:
                assert p in PRINCIPALS, (
                    f"Subtype {subtype!r} has expected principal {p!r} "
                    f"not in PRINCIPALS list"
                )

    def test_unknown_subtype_raises(self):
        with pytest.raises(ValueError, match="Unknown implementation governance subtype"):
            get_subtype_contract_entry("implementation.nonexistent")

    def test_source_of_truth_identification_expected(self):
        entry = get_subtype_contract_entry("implementation.source_of_truth_identification")
        assert "single_source_of_truth_preservation" in entry["expected_principals"]

    def test_shadow_contract_detection_expected(self):
        entry = get_subtype_contract_entry("implementation.shadow_contract_detection")
        assert "no_shadow_contracts" in entry["expected_principals"]
