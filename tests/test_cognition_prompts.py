"""Tests for cognition_prompts.py — Phase-specific prompt templates from bundle."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from cognition_loader import CognitionLoader
from cognition_prompts import (
    build_phase_requirements,
    build_transition_guidance,
    build_cognition_prompt_prefix,
    build_subtype_contract_prompt,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def loader() -> CognitionLoader:
    """CognitionLoader from real bundle."""
    bundle_path = Path(__file__).parent.parent / "bundle.json"
    with open(bundle_path, "r", encoding="utf-8") as f:
        bundle = json.load(f)
    return CognitionLoader(bundle)


# ── build_phase_requirements ─────────────────────────────────────────────────

def test_analyze_requirements(loader: CognitionLoader):
    """ANALYZE phase requirements include principals."""
    result = build_phase_requirements("ANALYZE", loader)
    assert "[Phase: ANALYZE]" in result
    assert "causal_grounding" in result
    assert "evidence_linkage" in result


def test_execute_requirements(loader: CognitionLoader):
    """EXECUTE phase requirements include minimal_change."""
    result = build_phase_requirements("EXECUTE", loader)
    assert "[Phase: EXECUTE]" in result
    assert "minimal_change" in result


def test_observe_requirements(loader: CognitionLoader):
    """OBSERVE phase includes forbidden actions."""
    result = build_phase_requirements("OBSERVE", loader)
    assert "Forbidden actions:" in result
    assert "do not write code" in result


def test_unknown_phase_empty(loader: CognitionLoader):
    """Unknown phase returns empty string."""
    result = build_phase_requirements("NONEXISTENT", loader)
    assert result == ""


def test_case_insensitive(loader: CognitionLoader):
    """Phase lookup is case insensitive."""
    result = build_phase_requirements("analyze", loader)
    assert "[Phase: ANALYZE]" in result


def test_forbidden_principals_in_analysis(loader: CognitionLoader):
    """ANALYZE requirements mention forbidden principals."""
    result = build_phase_requirements("ANALYZE", loader)
    assert "action_grounding" in result


# ── build_transition_guidance ────────────────────────────────────────────────

def test_analyze_transitions(loader: CognitionLoader):
    """ANALYZE can transition to DECIDE, ANALYZE, OBSERVE."""
    result = build_transition_guidance("ANALYZE", loader)
    assert "DECIDE" in result
    assert "ANALYZE" in result
    assert "OBSERVE" in result


def test_execute_transitions(loader: CognitionLoader):
    """EXECUTE can transition to JUDGE."""
    result = build_transition_guidance("EXECUTE", loader)
    assert "JUDGE" in result


def test_unknown_transitions(loader: CognitionLoader):
    """Unknown phase returns empty."""
    result = build_transition_guidance("NONEXISTENT", loader)
    assert result == ""


# ── build_cognition_prompt_prefix ────────────────────────────────────────────

def test_full_prefix_analyze(loader: CognitionLoader):
    """Full prefix combines requirements + transitions."""
    result = build_cognition_prompt_prefix("ANALYZE", loader)
    assert "[Phase: ANALYZE]" in result
    assert "causal_grounding" in result
    assert "Allowed next phases:" in result


def test_full_prefix_not_empty(loader: CognitionLoader):
    """All known phases produce non-empty prefix."""
    for phase in ["OBSERVE", "ANALYZE", "DECIDE", "DESIGN", "EXECUTE", "JUDGE"]:
        result = build_cognition_prompt_prefix(phase, loader)
        assert result, f"Empty prefix for phase {phase}"


# ── build_subtype_contract_prompt ────────────────────────────────────────────

def test_subtype_contract_analysis(loader: CognitionLoader):
    """analysis.root_cause contract prompt includes details."""
    result = build_subtype_contract_prompt("analysis.root_cause", loader)
    assert "[Contract: analysis.root_cause]" in result
    assert "Phase: ANALYZE" in result
    assert "causal_grounding" in result


def test_subtype_contract_execution(loader: CognitionLoader):
    """execution.code_patch contract prompt includes minimal_change."""
    result = build_subtype_contract_prompt("execution.code_patch", loader)
    assert "minimal_change" in result


def test_subtype_contract_unknown(loader: CognitionLoader):
    """Unknown subtype returns empty."""
    result = build_subtype_contract_prompt("nonexistent", loader)
    assert result == ""


def test_subtype_contract_forbidden(loader: CognitionLoader):
    """analysis.root_cause contract mentions forbidden principals."""
    result = build_subtype_contract_prompt("analysis.root_cause", loader)
    assert "Forbidden principals:" in result
    assert "action_grounding" in result


# ── No loader (graceful degradation) ────────────────────────────────────────

def test_no_loader_returns_empty():
    """All functions return empty string when loader is explicitly unavailable."""
    from cognition_prompts import reset_loader
    reset_loader()
    # Create a loader that has no phases/subtypes to simulate "unavailable"
    empty_loader = CognitionLoader({
        "cognition": {"phases": [], "subtypes": [], "principal_mapping": {}, "transitions": []}
    })
    assert build_phase_requirements("ANALYZE", loader=empty_loader) == ""
    assert build_transition_guidance("ANALYZE", loader=empty_loader) == ""
    assert build_cognition_prompt_prefix("ANALYZE", loader=empty_loader) == ""
    assert build_subtype_contract_prompt("analysis.root_cause", loader=empty_loader) == ""
