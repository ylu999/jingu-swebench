"""Tests for cognition_loader.py — CognitionLoader."""

import json
import sys
from pathlib import Path

import pytest

# Add scripts directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from cognition_loader import CognitionLoader


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def real_bundle() -> dict:
    """Load the real bundle.json from project root."""
    bundle_path = Path(__file__).parent.parent / "bundle.json"
    with open(bundle_path, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def loader(real_bundle: dict) -> CognitionLoader:
    """CognitionLoader initialized from real bundle."""
    return CognitionLoader(real_bundle)


@pytest.fixture
def minimal_bundle() -> dict:
    """Minimal bundle with cognition section for unit tests."""
    return {
        "cognition": {
            "phases": [
                {"name": "ANALYZE", "required_fields": ["evidence_refs"], "forbidden_outputs": ["do not write code"]},
                {"name": "EXECUTE", "required_fields": [], "forbidden_outputs": ["do not re-analyze"]},
            ],
            "subtypes": [
                {
                    "name": "analysis.root_cause",
                    "phase": "ANALYZE",
                    "required_principals": ["causal_grounding", "evidence_linkage"],
                    "forbidden_principals": ["action_grounding"],
                },
                {
                    "name": "execution.code_patch",
                    "phase": "EXECUTE",
                    "required_principals": ["minimal_change"],
                    "forbidden_principals": [],
                },
            ],
            "principal_mapping": {
                "analysis.root_cause": ["causal_grounding", "evidence_linkage"],
                "execution.code_patch": ["minimal_change"],
            },
            "transitions": [
                {"from": "ANALYZE", "to": "DECIDE", "allowed": True},
                {"from": "ANALYZE", "to": "EXECUTE", "allowed": False},
                {"from": "EXECUTE", "to": "JUDGE", "allowed": True},
            ],
        }
    }


# ── Constructor Tests ────────────────────────────────────────────────────────

def test_missing_cognition_section_raises():
    """Bundle without cognition section raises ValueError."""
    with pytest.raises(ValueError, match="no 'cognition' section"):
        CognitionLoader({"version": "1.0.0"})


def test_loader_from_real_bundle(loader: CognitionLoader):
    """CognitionLoader loads successfully from real bundle."""
    assert len(loader.phases) >= 6
    assert len(loader.subtypes) >= 6
    assert len(loader.principal_mapping) >= 6
    assert len(loader.transitions) > 0


# ── Phase Tests ──────────────────────────────────────────────────────────────

def test_get_all_phases(loader: CognitionLoader):
    """All canonical phases are present."""
    phases = loader.get_all_phases()
    for expected in ["OBSERVE", "ANALYZE", "DECIDE", "DESIGN", "EXECUTE", "JUDGE"]:
        assert expected in phases


def test_get_phase_definition(loader: CognitionLoader):
    """Phase definition has required structure."""
    defn = loader.get_phase_definition("ANALYZE")
    assert defn is not None
    assert defn["name"] == "ANALYZE"
    assert "forbidden_outputs" in defn


def test_get_phase_definition_unknown():
    """Unknown phase returns None."""
    loader = CognitionLoader({"cognition": {"phases": [], "subtypes": [], "principal_mapping": {}, "transitions": []}})
    assert loader.get_phase_definition("NONEXISTENT") is None


# ── Subtype Tests ────────────────────────────────────────────────────────────

def test_get_subtype_definition(loader: CognitionLoader):
    """Subtype definition has correct phase."""
    defn = loader.get_subtype_definition("analysis.root_cause")
    assert defn is not None
    assert defn["phase"] == "ANALYZE"
    assert "required_principals" in defn


def test_get_subtypes_for_phase(loader: CognitionLoader):
    """Subtypes for ANALYZE include analysis.root_cause."""
    subtypes = loader.get_subtypes_for_phase("ANALYZE")
    names = [s["name"] for s in subtypes]
    assert "analysis.root_cause" in names


def test_get_phase_for_subtype(loader: CognitionLoader):
    """Phase for subtype returns correct phase."""
    assert loader.get_phase_for_subtype("execution.code_patch") == "EXECUTE"
    assert loader.get_phase_for_subtype("nonexistent") is None


# ── Principal Tests ──────────────────────────────────────────────────────────

def test_get_required_principals_analysis(loader: CognitionLoader):
    """analysis.root_cause requires causal_grounding."""
    principals = loader.get_required_principals("analysis.root_cause")
    assert "causal_grounding" in principals


def test_get_required_principals_execution(loader: CognitionLoader):
    """execution.code_patch requires minimal_change."""
    principals = loader.get_required_principals("execution.code_patch")
    assert "minimal_change" in principals


def test_get_required_principals_unknown():
    """Unknown subtype returns empty list."""
    loader = CognitionLoader({"cognition": {"phases": [], "subtypes": [], "principal_mapping": {}, "transitions": []}})
    assert loader.get_required_principals("nonexistent") == []


def test_get_forbidden_principals(loader: CognitionLoader):
    """analysis.root_cause forbids action_grounding."""
    forbidden = loader.get_forbidden_principals("analysis.root_cause")
    assert "action_grounding" in forbidden


# ── Transition Tests ─────────────────────────────────────────────────────────

def test_transition_allowed(loader: CognitionLoader):
    """ANALYZE -> DECIDE is allowed."""
    assert loader.is_transition_allowed("ANALYZE", "DECIDE") is True


def test_transition_not_allowed(loader: CognitionLoader):
    """ANALYZE -> EXECUTE is not allowed (must go through DECIDE)."""
    assert loader.is_transition_allowed("ANALYZE", "EXECUTE") is False


def test_transition_with_minimal_bundle(minimal_bundle: dict):
    """Transitions work with minimal bundle."""
    loader = CognitionLoader(minimal_bundle)
    assert loader.is_transition_allowed("ANALYZE", "DECIDE") is True
    assert loader.is_transition_allowed("ANALYZE", "EXECUTE") is False
    assert loader.is_transition_allowed("EXECUTE", "JUDGE") is True
