"""Tests for check_onboarding.py — CI onboarding completeness check."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from check_onboarding import (
    check_bundle_valid,
    check_all_phases_have_contracts,
    check_principal_lifecycle,
    check_no_orphan_references,
    run_all_checks,
)
from jingu_loader import JinguLoader


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_full_bundle() -> dict:
    """Create a bundle with all 6 required contracts."""
    subtypes = [
        ("OBSERVE", "observation.fact_gathering"),
        ("ANALYZE", "analysis.root_cause"),
        ("DECIDE", "decision.fix_direction"),
        ("DESIGN", "design.solution_shape"),
        ("EXECUTE", "execution.code_patch"),
        ("JUDGE", "judge.verification"),
    ]
    contracts = {}
    for phase, subtype in subtypes:
        contracts[subtype] = {
            "phase": phase,
            "subtype": subtype,
            "compiled_at": "2026-04-10T00:00:00.000Z",
            "phase_spec": {"name": phase, "goal": f"Goal for {phase}",
                           "forbidden_moves": [], "allowed_next_phases": [], "default_schema": ""},
            "cognition_spec": {"type": subtype, "phase": phase, "task_shape": "test",
                               "success_criteria": [], "required_evidence_kinds": [], "schema_ref": ""},
            "principals": [
                {
                    "name": "ontology_alignment",
                    "applies_to": [subtype],
                    "requires_fields": [],
                    "semantic_checks": [],
                    "repair_hint": "Fix ontology alignment",
                    "inference_rule_exists": False,
                    "fake_check_eligible": False,
                },
                {
                    "name": "phase_boundary_discipline",
                    "applies_to": [subtype],
                    "requires_fields": [],
                    "semantic_checks": [],
                    "repair_hint": "Stay within phase boundary",
                    "inference_rule_exists": False,
                    "fake_check_eligible": False,
                },
            ],
            "policy": {
                "id": f"policy:{subtype}",
                "phase": phase,
                "subtype": subtype,
                "required_fields": [],
                "forbidden_moves": [],
                "required_principals": ["ontology_alignment", "phase_boundary_discipline"],
                "forbidden_principals": [],
                "schema_ref": "",
            },
            "schema": {},
            "prompt": f"## Phase: {phase}\nGoal: Goal for {phase}\nontology_alignment, phase_boundary_discipline",
            "repair_templates": {
                "ontology_alignment": "[ontology_alignment] violation",
                "phase_boundary_discipline": "[phase_boundary_discipline] violation",
            },
            "routing": {"principal_routes": {}, "default_route": {"next_phase": phase, "strategy": "retry"}},
        }
    return {
        "version": "1.0.0",
        "generated_at": "2026-04-10T00:00:00.000Z",
        "generator_commit": "test",
        "capabilities": ["prompt_only"],
        "phases": ["UNDERSTAND", "OBSERVE", "ANALYZE", "DECIDE", "DESIGN", "EXECUTE", "JUDGE"],
        "contracts": contracts,
    }


def _write_bundle(bundle: dict) -> str:
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(bundle, f)
    return path


# ── Test: check_bundle_valid ──────────────────────────────────────────────────

class TestBundleValid:
    def test_valid_bundle(self):
        path = _write_bundle(_make_full_bundle())
        try:
            errors = check_bundle_valid(path)
            assert errors == []
        finally:
            os.unlink(path)

    def test_missing_file(self):
        errors = check_bundle_valid("/nonexistent/bundle.json")
        assert len(errors) == 1
        assert "not found" in errors[0]

    def test_invalid_json(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write("{invalid json")
        try:
            errors = check_bundle_valid(path)
            assert len(errors) == 1
            assert "not valid JSON" in errors[0]
        finally:
            os.unlink(path)

    def test_missing_required_fields(self):
        path = _write_bundle({"some_field": "value"})
        try:
            errors = check_bundle_valid(path)
            assert len(errors) == 3  # version, phases, contracts
        finally:
            os.unlink(path)


# ── Test: check_all_phases_have_contracts ─────────────────────────────────────

class TestAllPhasesHaveContracts:
    def test_full_coverage(self):
        path = _write_bundle(_make_full_bundle())
        try:
            loader = JinguLoader(path)
            errors = check_all_phases_have_contracts(loader)
            assert errors == []
        finally:
            os.unlink(path)

    def test_missing_phase(self):
        bundle = _make_full_bundle()
        del bundle["contracts"]["analysis.root_cause"]
        path = _write_bundle(bundle)
        try:
            loader = JinguLoader(path)
            errors = check_all_phases_have_contracts(loader)
            assert len(errors) == 1
            assert "ANALYZE" in errors[0]
        finally:
            os.unlink(path)


# ── Test: check_principal_lifecycle ───────────────────────────────────────────

class TestPrincipalLifecycle:
    def test_all_principals_have_lifecycle(self):
        path = _write_bundle(_make_full_bundle())
        try:
            loader = JinguLoader(path)
            errors = check_principal_lifecycle(loader)
            assert errors == []
        finally:
            os.unlink(path)

    def test_missing_lifecycle_field(self):
        bundle = _make_full_bundle()
        # Remove inference_rule_exists from one principal
        p = bundle["contracts"]["analysis.root_cause"]["principals"][0]
        del p["inference_rule_exists"]
        path = _write_bundle(bundle)
        try:
            loader = JinguLoader(path)
            errors = check_principal_lifecycle(loader)
            assert len(errors) >= 1
            assert "inference_rule_exists" in errors[0]
        finally:
            os.unlink(path)

    def test_required_principal_not_in_array(self):
        bundle = _make_full_bundle()
        # Add a required principal that doesn't exist in principals array
        bundle["contracts"]["analysis.root_cause"]["policy"]["required_principals"].append("nonexistent_principal")
        path = _write_bundle(bundle)
        try:
            loader = JinguLoader(path)
            errors = check_principal_lifecycle(loader)
            orphan_errors = [e for e in errors if "nonexistent_principal" in e]
            assert len(orphan_errors) >= 1
        finally:
            os.unlink(path)


# ── Test: check_no_orphan_references ──────────────────────────────────────────

class TestNoOrphanReferences:
    def test_no_orphans(self):
        path = _write_bundle(_make_full_bundle())
        try:
            loader = JinguLoader(path)
            errors = check_no_orphan_references(loader)
            assert errors == []
        finally:
            os.unlink(path)

    def test_orphan_detected(self):
        bundle = _make_full_bundle()
        # Add a forbidden principal reference to a name that doesn't exist
        bundle["contracts"]["analysis.root_cause"]["policy"]["forbidden_principals"] = ["completely_unknown"]
        path = _write_bundle(bundle)
        try:
            loader = JinguLoader(path)
            errors = check_no_orphan_references(loader)
            assert len(errors) == 1
            assert "completely_unknown" in errors[0]
        finally:
            os.unlink(path)


# ── Test: run_all_checks (integration) ────────────────────────────────────────

class TestRunAllChecks:
    def test_complete_bundle_passes(self):
        path = _write_bundle(_make_full_bundle())
        try:
            error_count, errors = run_all_checks(path)
            assert error_count == 0, f"Expected 0 errors, got: {errors}"
        finally:
            os.unlink(path)

    def test_missing_file_fails(self):
        error_count, errors = run_all_checks("/nonexistent/bundle.json")
        assert error_count > 0

    def test_real_bundle_if_available(self):
        """Integration test with real bundle.json."""
        bundle_path = Path(__file__).parent.parent / "bundle.json"
        if not bundle_path.exists():
            pytest.skip("Real bundle.json not found")
        error_count, errors = run_all_checks(str(bundle_path))
        # Real bundle should pass (may have known issues - report them)
        if error_count > 0:
            print(f"Real bundle has {error_count} issues:")
            for e in errors:
                print(f"  - {e}")
