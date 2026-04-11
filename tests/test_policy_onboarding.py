"""Tests for policy_onboarding.py — Policy onboarding API."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from jingu_loader import JinguLoader, PolicyLifecycle


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_bundle(prompt_override: str | None = None, missing_field: bool = False) -> dict:
    """Create a minimal valid bundle for testing."""
    prompt = prompt_override if prompt_override is not None else (
        "## Phase: ANALYZE\n"
        "Goal: Root-cause analysis\n"
        "evidence_refs required\n"
        "## Required Principals\n"
        "causal_grounding, evidence_linkage"
    )
    bundle = {
        "version": "1.0.0",
        "generated_at": "2026-04-10T00:00:00.000Z",
        "generator_commit": "abc1234",
        "capabilities": ["prompt_only", "repair_view"],
        "phases": ["ANALYZE"],
        "contracts": {
            "analysis.root_cause": {
                "phase": "ANALYZE",
                "subtype": "analysis.root_cause",
                "compiled_at": "2026-04-10T00:00:00.000Z",
                "phase_spec": {"name": "ANALYZE", "goal": "Root-cause analysis",
                               "forbidden_moves": [], "allowed_next_phases": [], "default_schema": ""},
                "cognition_spec": {"type": "analysis.root_cause", "phase": "ANALYZE",
                                   "task_shape": "root_cause_analysis", "success_criteria": [],
                                   "required_evidence_kinds": [], "schema_ref": ""},
                "principals": [],
                "policy": {
                    "id": "policy:analysis.root_cause",
                    "phase": "ANALYZE",
                    "subtype": "analysis.root_cause",
                    "required_fields": ["evidence_refs"] if not missing_field else ["evidence_refs", "missing_field_xyz"],
                    "forbidden_moves": [],
                    "required_principals": ["causal_grounding", "evidence_linkage"],
                    "forbidden_principals": [],
                    "schema_ref": "",
                },
                "schema": {},
                "prompt": prompt,
                "repair_templates": {
                    "causal_grounding": "[causal_grounding] violation",
                    "evidence_linkage": "[evidence_linkage] violation",
                },
                "routing": {"principal_routes": {}, "default_route": {"next_phase": "OBSERVE", "strategy": "retry"}},
            },
        },
    }
    return bundle


def _write_bundle(bundle: dict) -> str:
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(bundle, f)
    return path


# ── Test: Onboarding Completeness ─────────────────────────────────────────────

class TestOnboardingCompleteness:
    def test_complete_bundle_zero_violations(self):
        """Well-formed bundle passes completeness check."""
        from policy_onboarding import check_onboarding_completeness
        path = _write_bundle(_make_bundle())
        try:
            loader = JinguLoader(path)
            violations = check_onboarding_completeness(loader)
            assert violations == [], f"Expected zero violations, got: {violations}"
        finally:
            os.unlink(path)

    def test_empty_prompt_violation(self):
        """Missing prompt is reported as violation."""
        from policy_onboarding import check_onboarding_completeness
        path = _write_bundle(_make_bundle(prompt_override=""))
        try:
            loader = JinguLoader(path)
            violations = check_onboarding_completeness(loader)
            assert len(violations) == 1
            assert "no prompt" in violations[0]
        finally:
            os.unlink(path)

    def test_missing_required_field_in_prompt(self):
        """Prompt that doesn't mention a required field is reported."""
        from policy_onboarding import check_onboarding_completeness
        path = _write_bundle(_make_bundle(missing_field=True))
        try:
            loader = JinguLoader(path)
            violations = check_onboarding_completeness(loader)
            field_violations = [v for v in violations if "missing_field_xyz" in v]
            assert len(field_violations) == 1
        finally:
            os.unlink(path)

    def test_missing_repair_template(self):
        """Missing repair template for required principal is reported."""
        from policy_onboarding import check_onboarding_completeness
        bundle = _make_bundle()
        # Remove one repair template
        del bundle["contracts"]["analysis.root_cause"]["repair_templates"]["evidence_linkage"]
        path = _write_bundle(bundle)
        try:
            loader = JinguLoader(path)
            violations = check_onboarding_completeness(loader)
            repair_violations = [v for v in violations if "repair template" in v]
            assert len(repair_violations) == 1
            assert "evidence_linkage" in repair_violations[0]
        finally:
            os.unlink(path)


# ── Test: get_prompt_slice ────────────────────────────────────────────────────

class TestGetPromptSlice:
    def test_returns_non_empty_string(self):
        """get_prompt_slice returns non-empty string for valid phase."""
        from policy_onboarding import get_prompt_slice, reset_loader
        reset_loader()
        path = _write_bundle(_make_bundle())
        try:
            loader = JinguLoader(path)
            # Patch the loader instance
            import policy_onboarding
            policy_onboarding._loader_instance = loader
            result = get_prompt_slice("ANALYZE")
            assert len(result) > 0
            assert "ANALYZE" in result
        finally:
            reset_loader()
            os.unlink(path)

    def test_prompt_mentions_required_fields(self):
        """Prompt slice mentions the required fields from the contract."""
        from policy_onboarding import get_prompt_slice, reset_loader
        reset_loader()
        path = _write_bundle(_make_bundle())
        try:
            loader = JinguLoader(path)
            import policy_onboarding
            policy_onboarding._loader_instance = loader
            result = get_prompt_slice("ANALYZE")
            assert "evidence_refs" in result
        finally:
            reset_loader()
            os.unlink(path)


# ── Test: inject_contract_into_prompt ─────────────────────────────────────────

class TestInjectContract:
    def test_injects_contract_info(self):
        """inject_contract_into_prompt appends contract requirements."""
        from policy_onboarding import inject_contract_into_prompt, reset_loader
        reset_loader()
        path = _write_bundle(_make_bundle())
        try:
            loader = JinguLoader(path)
            import policy_onboarding
            policy_onboarding._loader_instance = loader
            result = inject_contract_into_prompt("Original prompt", "ANALYZE")
            assert "Original prompt" in result
            assert "Contract Requirements" in result
            assert "ANALYZE" in result
        finally:
            reset_loader()
            os.unlink(path)

    def test_graceful_fallback_on_error(self):
        """Returns original prompt if loader fails."""
        from policy_onboarding import inject_contract_into_prompt, reset_loader
        reset_loader()
        # No loader set, default path won't find bundle
        import policy_onboarding
        policy_onboarding._loader_instance = None
        result = inject_contract_into_prompt("Original prompt", "NONEXISTENT")
        assert result == "Original prompt"


# ── Test: get_lifecycle_state ─────────────────────────────────────────────────

class TestLifecycleState:
    def test_repaired_state(self):
        """Complete contract with repair templates achieves REPAIRED."""
        from policy_onboarding import get_lifecycle_state, reset_loader
        reset_loader()
        path = _write_bundle(_make_bundle())
        try:
            loader = JinguLoader(path)
            import policy_onboarding
            policy_onboarding._loader_instance = loader
            state = get_lifecycle_state("ANALYZE")
            assert state == PolicyLifecycle.REPAIRED
        finally:
            reset_loader()
            os.unlink(path)

    def test_enforced_state_without_repair(self):
        """Contract missing repair templates stops at ENFORCED."""
        from policy_onboarding import get_lifecycle_state, reset_loader
        reset_loader()
        bundle = _make_bundle()
        bundle["contracts"]["analysis.root_cause"]["repair_templates"] = {}
        path = _write_bundle(bundle)
        try:
            loader = JinguLoader(path)
            import policy_onboarding
            policy_onboarding._loader_instance = loader
            state = get_lifecycle_state("ANALYZE")
            assert state == PolicyLifecycle.ENFORCED
        finally:
            reset_loader()
            os.unlink(path)

    def test_registered_state_without_prompt(self):
        """Contract with empty prompt stops at REGISTERED."""
        from policy_onboarding import get_lifecycle_state, reset_loader
        reset_loader()
        path = _write_bundle(_make_bundle(prompt_override=""))
        try:
            loader = JinguLoader(path)
            import policy_onboarding
            policy_onboarding._loader_instance = loader
            state = get_lifecycle_state("ANALYZE")
            assert state == PolicyLifecycle.REGISTERED
        finally:
            reset_loader()
            os.unlink(path)


# ── Test: Legacy Path Still Works ─────────────────────────────────────────────

class TestLegacyPath:
    def test_use_bundle_loader_flag_default_false(self):
        """USE_BUNDLE_LOADER defaults to false."""
        from jingu_loader import USE_BUNDLE_LOADER
        # Unless env var is set, should be False
        if os.environ.get("USE_BUNDLE_LOADER", "false").lower() != "true":
            assert USE_BUNDLE_LOADER is False

    def test_phase_prompt_still_works(self):
        """phase_prompt.py still works (backward compat)."""
        from phase_prompt import build_phase_prefix
        prefix = build_phase_prefix("ANALYZE")
        assert "[Phase: ANALYZE]" in prefix
        assert len(prefix) > 50
