"""Tests for jingu_loader.py — Python JinguLoader for bundle consumption."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

# Ensure scripts/ is importable
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from jingu_loader import JinguLoader, USE_BUNDLE_LOADER, PolicyLifecycle, _resolve_subtype, RUNTIME_CAPABILITIES


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_bundle(overrides: dict | None = None) -> dict:
    """Create a minimal valid bundle dict for testing."""
    bundle = {
        "version": "1.0.0",
        "compiler_version": "0.1.0",
        "generated_at": "2026-04-10T00:00:00.000Z",
        "generator_commit": "abc1234",
        "capabilities": ["prompt_only", "schema_enforced", "repair_view", "routing_view"],
        "phases": ["UNDERSTAND", "OBSERVE", "ANALYZE", "DECIDE", "DESIGN", "EXECUTE", "JUDGE"],
        "contracts": {
            "analysis.root_cause": {
                "phase": "ANALYZE",
                "subtype": "analysis.root_cause",
                "compiled_at": "2026-04-10T00:00:00.000Z",
                "phase_spec": {
                    "name": "ANALYZE",
                    "goal": "Root-cause analysis",
                    "forbidden_moves": ["do not write code"],
                    "allowed_next_phases": ["DECIDE", "OBSERVE"],
                    "default_schema": "analysis_output",
                },
                "cognition_spec": {
                    "type": "analysis.root_cause",
                    "phase": "ANALYZE",
                    "task_shape": "root_cause_analysis",
                    "success_criteria": ["root cause identified"],
                    "required_evidence_kinds": ["file_reference"],
                    "schema_ref": "analysis_output",
                },
                "principals": [
                    {
                        "name": "causal_grounding",
                        "applies_to": ["analysis.root_cause"],
                        "requires_fields": ["evidence_refs"],
                        "semantic_checks": ["grounded_in_code"],
                        "repair_hint": "Provide causal evidence linking root cause to observed behavior",
                        "inference_rule_exists": True,
                        "fake_check_eligible": True,
                    }
                ],
                "policy": {
                    "id": "policy:analysis.root_cause",
                    "phase": "ANALYZE",
                    "subtype": "analysis.root_cause",
                    "required_fields": ["evidence_refs"],
                    "forbidden_moves": ["do not write code"],
                    "required_principals": ["causal_grounding", "evidence_linkage"],
                    "forbidden_principals": ["action_grounding"],
                    "schema_ref": "analysis_output",
                },
                "schema": {"type": "object", "properties": {"phase": {"type": "string"}}},
                "prompt": "## Phase: ANALYZE\nGoal: Root-cause analysis\nevidence_refs required",
                "repair_templates": {
                    "causal_grounding": "[causal_grounding] violation detected.\nRequirement: Provide causal evidence",
                },
                "routing": {
                    "principal_routes": {
                        "causal_grounding": {"next_phase": "OBSERVE", "strategy": "gather more evidence"},
                    },
                    "default_route": {"next_phase": "OBSERVE", "strategy": "redirect to OBSERVE"},
                },
            },
            "execution.code_patch": {
                "phase": "EXECUTE",
                "subtype": "execution.code_patch",
                "compiled_at": "2026-04-10T00:00:00.000Z",
                "phase_spec": {
                    "name": "EXECUTE",
                    "goal": "Produce the minimal code patch",
                    "forbidden_moves": [],
                    "allowed_next_phases": ["JUDGE"],
                    "default_schema": "execute_output",
                },
                "cognition_spec": {
                    "type": "execution.code_patch",
                    "phase": "EXECUTE",
                    "task_shape": "code_patch",
                    "success_criteria": ["patch applies cleanly"],
                    "required_evidence_kinds": [],
                    "schema_ref": "execute_output",
                },
                "principals": [],
                "policy": {
                    "id": "policy:execution.code_patch",
                    "phase": "EXECUTE",
                    "subtype": "execution.code_patch",
                    "required_fields": [],
                    "forbidden_moves": [],
                    "required_principals": ["minimal_change", "action_grounding"],
                    "forbidden_principals": [],
                    "schema_ref": "execute_output",
                },
                "schema": {"type": "object"},
                "prompt": "## Phase: EXECUTE\nGoal: Produce the minimal code patch",
                "repair_templates": {},
                "routing": {
                    "principal_routes": {},
                    "default_route": {"next_phase": "EXECUTE", "strategy": "retry"},
                },
            },
        },
    }
    if overrides:
        bundle.update(overrides)
    return bundle


def _write_bundle(bundle: dict) -> str:
    """Write a bundle dict to a temp file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(bundle, f)
    return path


@pytest.fixture
def bundle_path():
    """Create a valid bundle file and return its path."""
    path = _write_bundle(_make_bundle())
    yield path
    os.unlink(path)


@pytest.fixture
def loader(bundle_path):
    """Create a JinguLoader from the test bundle."""
    return JinguLoader(bundle_path)


# ── Test: Loading ─────────────────────────────────────────────────────────────

class TestLoading:
    def test_loads_valid_bundle(self, loader):
        """Valid bundle loads without error."""
        assert loader is not None

    def test_file_not_found(self):
        """Missing file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            JinguLoader("/nonexistent/path/bundle.json")

    def test_invalid_json(self):
        """Invalid JSON raises json.JSONDecodeError."""
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write("not valid json {{{")
        try:
            with pytest.raises(json.JSONDecodeError):
                JinguLoader(path)
        finally:
            os.unlink(path)

    def test_version_mismatch(self):
        """Incompatible version raises ValueError."""
        bundle = _make_bundle({"version": "2.0.0"})
        path = _write_bundle(bundle)
        try:
            with pytest.raises(ValueError, match="not compatible"):
                JinguLoader(path)
        finally:
            os.unlink(path)

    def test_missing_version(self):
        """Missing version field raises ValueError."""
        bundle = _make_bundle()
        del bundle["version"]
        path = _write_bundle(bundle)
        try:
            with pytest.raises(ValueError, match="no version"):
                JinguLoader(path)
        finally:
            os.unlink(path)


# ── Test: Contract Access ─────────────────────────────────────────────────────

class TestContractAccess:
    def test_get_active_contract_by_phase(self, loader):
        """Get contract by phase name (auto-resolves subtype)."""
        contract = loader.get_active_contract("ANALYZE")
        assert contract["phase"] == "ANALYZE"
        assert contract["subtype"] == "analysis.root_cause"

    def test_get_active_contract_by_subtype(self, loader):
        """Get contract with explicit subtype."""
        contract = loader.get_active_contract("ANALYZE", "analysis.root_cause")
        assert contract["phase"] == "ANALYZE"

    def test_get_active_contract_case_insensitive_phase(self, loader):
        """Phase name is case-insensitive (uppercased internally)."""
        contract = loader.get_active_contract("analyze")
        assert contract["phase"] == "ANALYZE"

    def test_get_active_contract_unknown_phase(self, loader):
        """Unknown phase raises ValueError."""
        with pytest.raises(ValueError, match="Cannot resolve"):
            loader.get_active_contract("UNKNOWN_PHASE")

    def test_get_active_contract_unknown_subtype(self, loader):
        """Unknown subtype raises KeyError."""
        with pytest.raises(KeyError, match="No contract found"):
            loader.get_active_contract("ANALYZE", "analysis.nonexistent")

    def test_get_required_principals(self, loader):
        """Required principals returns list of strings."""
        principals = loader.get_required_principals("ANALYZE")
        assert "causal_grounding" in principals
        assert "evidence_linkage" in principals

    def test_get_prompt(self, loader):
        """Prompt returns non-empty string."""
        prompt = loader.get_prompt("ANALYZE")
        assert len(prompt) > 0
        assert "ANALYZE" in prompt

    def test_get_schema(self, loader):
        """Schema returns a dict with at least 'type' field."""
        schema = loader.get_schema("ANALYZE")
        assert isinstance(schema, dict)
        assert "type" in schema

    def test_get_prompt_view(self, loader):
        """Prompt view returns all expected fields."""
        view = loader.get_prompt_view("ANALYZE")
        assert "prompt" in view
        assert "required_fields" in view
        assert "forbidden_moves" in view
        assert "required_principals" in view
        assert isinstance(view["required_fields"], list)

    def test_get_repair_view(self, loader):
        """Repair view returns repair_templates and routing."""
        view = loader.get_repair_view("ANALYZE")
        assert "repair_templates" in view
        assert "routing" in view
        assert "causal_grounding" in view["repair_templates"]


# ── Test: Metadata ────────────────────────────────────────────────────────────

class TestMetadata:
    def test_get_metadata(self, loader):
        """Metadata returns version, capabilities, etc."""
        meta = loader.get_metadata()
        assert meta["version"] == "1.0.0"
        assert "prompt_only" in meta["capabilities"]
        assert meta["contract_count"] == 2

    def test_list_contracts(self, loader):
        """List contracts returns all subtype keys."""
        contracts = loader.list_contracts()
        assert "analysis.root_cause" in contracts
        assert "execution.code_patch" in contracts

    def test_list_phases(self, loader):
        """List phases returns all phase names."""
        phases = loader.list_phases()
        assert "ANALYZE" in phases
        assert "EXECUTE" in phases


# ── Test: Subtype Resolution ──────────────────────────────────────────────────

class TestSubtypeResolution:
    def test_resolve_known_phase(self):
        assert _resolve_subtype("ANALYZE") == "analysis.root_cause"
        assert _resolve_subtype("EXECUTE") == "execution.code_patch"

    def test_resolve_case_insensitive(self):
        assert _resolve_subtype("analyze") == "analysis.root_cause"

    def test_resolve_explicit_subtype(self):
        assert _resolve_subtype("ANALYZE", "analysis.root_cause") == "analysis.root_cause"

    def test_resolve_unknown_phase_raises(self):
        with pytest.raises(ValueError):
            _resolve_subtype("UNKNOWN")


# ── Test: Policy Lifecycle ────────────────────────────────────────────────────

class TestPolicyLifecycle:
    def test_lifecycle_states(self):
        """All lifecycle states are defined."""
        assert PolicyLifecycle.DEFINED == "defined"
        assert PolicyLifecycle.REGISTERED == "registered"
        assert PolicyLifecycle.INJECTED == "injected"
        assert PolicyLifecycle.ENFORCED == "enforced"
        assert PolicyLifecycle.REPAIRED == "repaired"


# ── Test: Real Bundle (integration) ──────────────────────────────────────────

class TestRealBundle:
    """Integration tests using the actual generated bundle.json if available."""

    @pytest.fixture
    def real_loader(self):
        """Load the real bundle.json from project root."""
        bundle_path = Path(__file__).parent.parent / "bundle.json"
        if not bundle_path.exists():
            pytest.skip("Real bundle.json not found (run generate-bundle first)")
        return JinguLoader(str(bundle_path))

    def test_real_bundle_loads(self, real_loader):
        """Real bundle loads without error."""
        meta = real_loader.get_metadata()
        assert meta["version"] == "1.0.0"
        assert meta["contract_count"] == 6

    def test_real_bundle_all_phases_have_contracts(self, real_loader):
        """Every phase with a subtype has a contract."""
        for phase in ["OBSERVE", "ANALYZE", "DECIDE", "DESIGN", "EXECUTE", "JUDGE"]:
            contract = real_loader.get_active_contract(phase)
            assert contract is not None
            assert len(contract.get("prompt", "")) > 0

    def test_real_bundle_no_empty_prompts(self, real_loader):
        """No contract has an empty prompt."""
        for subtype in real_loader.list_contracts():
            contract = real_loader.get_active_contract("ANALYZE", subtype) if "analysis" in subtype else None
            if contract:
                assert len(contract.get("prompt", "")) > 0


# ── Test: Capability Negotiation (w4-02) ─────────────────────────────────────

class TestCapabilityNegotiation:
    """Tests for get_negotiated_contract() and RUNTIME_CAPABILITIES."""

    def test_runtime_capabilities_defined(self):
        """RUNTIME_CAPABILITIES is a dict with expected keys."""
        assert isinstance(RUNTIME_CAPABILITIES, dict)
        assert "schema_enforced" in RUNTIME_CAPABILITIES
        assert "repair_view" in RUNTIME_CAPABILITIES
        assert "routing_view" in RUNTIME_CAPABILITIES

    def test_default_capabilities_values(self):
        """Default runtime capabilities match expected jingu-swebench state."""
        assert RUNTIME_CAPABILITIES["schema_enforced"] is False
        assert RUNTIME_CAPABILITIES["repair_view"] is True
        assert RUNTIME_CAPABILITIES["routing_view"] is False

    def test_negotiate_all_true(self, loader):
        """All capabilities true: all fields present."""
        caps = {"schema_enforced": True, "repair_view": True, "routing_view": True}
        negotiated = loader.get_negotiated_contract("ANALYZE", runtime_caps=caps)

        assert "prompt" in negotiated
        assert len(negotiated["prompt"]) > 0
        assert "schema" in negotiated
        assert "repair_templates" in negotiated
        assert "routing" in negotiated

    def test_negotiate_schema_false(self, loader):
        """schema_enforced=False: no schema field."""
        caps = {"schema_enforced": False, "repair_view": True, "routing_view": True}
        negotiated = loader.get_negotiated_contract("ANALYZE", runtime_caps=caps)

        assert "prompt" in negotiated
        assert "schema" not in negotiated
        assert "repair_templates" in negotiated
        assert "routing" in negotiated

    def test_negotiate_all_false(self, loader):
        """All false: only prompt field."""
        caps = {"schema_enforced": False, "repair_view": False, "routing_view": False}
        negotiated = loader.get_negotiated_contract("ANALYZE", runtime_caps=caps)

        assert "prompt" in negotiated
        assert len(negotiated["prompt"]) > 0
        assert "schema" not in negotiated
        assert "repair_templates" not in negotiated
        assert "routing" not in negotiated

    def test_negotiate_uses_default_capabilities(self, loader):
        """Without explicit caps, uses RUNTIME_CAPABILITIES."""
        negotiated = loader.get_negotiated_contract("ANALYZE")

        # With default caps: schema_enforced=False, repair_view=True, routing_view=False
        assert "prompt" in negotiated
        assert "schema" not in negotiated
        assert "repair_templates" in negotiated
        assert "routing" not in negotiated

    def test_negotiate_no_crash_on_empty_contract_fields(self, loader):
        """Negotiation does not crash when contract has empty fields."""
        caps = {"schema_enforced": True, "repair_view": True, "routing_view": True}
        negotiated = loader.get_negotiated_contract("EXECUTE", runtime_caps=caps)

        assert "prompt" in negotiated
        # EXECUTE has empty repair_templates, so it should not appear
        assert "repair_templates" not in negotiated

    def test_metadata_includes_compiler_version(self, loader):
        """Metadata includes compiler_version field."""
        meta = loader.get_metadata()
        assert "compiler_version" in meta
        assert meta["compiler_version"] == "0.1.0"
