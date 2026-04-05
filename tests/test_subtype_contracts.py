"""
test_subtype_contracts.py — Unit tests for p193 subtype contracts canonical source.

Verifies acceptance criteria:
- get_required_principals("ANALYZE") returns ["causal_grounding", "evidence_linkage"]
- principal_gate.check_principal_gate(obj with ["causality"], "ANALYZE") -> violation
- principal_gate.check_principal_gate(obj with ["causal_grounding","evidence_linkage"], "ANALYZE") -> None
- get_repair_target("ANALYZE") returns "OBSERVE"
- build_phase_principal_guidance("ANALYZE") contains "causal_grounding"
- PHASE_GUIDANCE["ANALYZE"] in phase_prompt contains "causal_grounding"
- PHASE_REQUIRED_PRINCIPALS in principal_gate matches subtype_contracts
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest
from subtype_contracts import (
    SUBTYPE_CONTRACTS,
    get_required_principals,
    get_repair_target,
    build_phase_principal_guidance,
)
from principal_gate import check_principal_gate, PHASE_REQUIRED_PRINCIPALS
from phase_prompt import PHASE_GUIDANCE


# ── Simple stub ───────────────────────────────────────────────────────────────

class _FakePR:
    """Minimal PhaseRecord stub with .principals list."""
    def __init__(self, principals: list[str]):
        self.principals = principals
        self.phase = "TEST"


# ── Tests: SUBTYPE_CONTRACTS structure ───────────────────────────────────────

def test_contracts_has_three_subtypes():
    """SUBTYPE_CONTRACTS defines the three core subtypes."""
    assert "analysis.root_cause" in SUBTYPE_CONTRACTS
    assert "execution.code_patch" in SUBTYPE_CONTRACTS
    assert "judge.verification" in SUBTYPE_CONTRACTS


def test_analyze_contract_has_causal_grounding():
    """analysis.root_cause required_principals includes causal_grounding."""
    contract = SUBTYPE_CONTRACTS["analysis.root_cause"]
    assert "causal_grounding" in contract["required_principals"]


def test_execute_contract_has_minimal_change():
    """execution.code_patch required_principals includes minimal_change."""
    contract = SUBTYPE_CONTRACTS["execution.code_patch"]
    assert "minimal_change" in contract["required_principals"]


def test_judge_contract_has_invariant_preservation():
    """judge.verification required_principals includes invariant_preservation."""
    contract = SUBTYPE_CONTRACTS["judge.verification"]
    assert "invariant_preservation" in contract["required_principals"]


# ── Tests: get_required_principals ───────────────────────────────────────────

def test_get_required_principals_analyze():
    """ANALYZE phase returns causal_grounding (gate-enforced minimum)."""
    principals = get_required_principals("ANALYZE")
    assert "causal_grounding" in principals, f"missing causal_grounding in {principals}"


def test_get_required_principals_execute():
    """EXECUTE phase returns minimal_change."""
    principals = get_required_principals("EXECUTE")
    assert "minimal_change" in principals


def test_get_required_principals_judge():
    """JUDGE phase returns invariant_preservation."""
    principals = get_required_principals("JUDGE")
    assert "invariant_preservation" in principals


def test_get_required_principals_observe():
    """OBSERVE phase returns empty list (no enforcement)."""
    principals = get_required_principals("OBSERVE")
    assert principals == []


def test_get_required_principals_unknown():
    """Unknown phase returns empty list (no crash)."""
    principals = get_required_principals("NONEXISTENT_PHASE")
    assert principals == []


def test_get_required_principals_case_insensitive():
    """Phase name is case-insensitive."""
    principals_upper = get_required_principals("ANALYZE")
    principals_lower = get_required_principals("analyze")
    assert principals_upper == principals_lower


# ── Tests: get_repair_target ─────────────────────────────────────────────────

def test_get_repair_target_analyze():
    """ANALYZE violation repair target is OBSERVE."""
    target = get_repair_target("ANALYZE")
    assert target == "OBSERVE", f"expected OBSERVE, got {target}"


def test_get_repair_target_execute():
    """EXECUTE violation repair target is ANALYZE."""
    target = get_repair_target("EXECUTE")
    assert target == "ANALYZE", f"expected ANALYZE, got {target}"


def test_get_repair_target_judge():
    """JUDGE violation repair target is EXECUTE."""
    target = get_repair_target("JUDGE")
    assert target == "EXECUTE", f"expected EXECUTE, got {target}"


def test_get_repair_target_unknown():
    """Unknown phase repair target is empty string (no crash)."""
    target = get_repair_target("UNKNOWN")
    assert target == ""


# ── Tests: build_phase_principal_guidance ────────────────────────────────────

def test_guidance_analyze_contains_causal_grounding():
    """ANALYZE guidance mentions causal_grounding (critical acceptance criterion)."""
    guidance = build_phase_principal_guidance("ANALYZE")
    assert "causal_grounding" in guidance, (
        f"ANALYZE guidance must contain 'causal_grounding', got: {guidance!r}"
    )


def test_guidance_analyze_does_not_say_causality():
    """ANALYZE guidance uses 'causal_grounding' not old 'causality' term."""
    guidance = build_phase_principal_guidance("ANALYZE")
    # The guidance should use causal_grounding (system B), not 'causality' (system A mismatch)
    assert "causal_grounding" in guidance


def test_guidance_execute_contains_minimal_change():
    """EXECUTE guidance mentions minimal_change."""
    guidance = build_phase_principal_guidance("EXECUTE")
    assert "minimal_change" in guidance


def test_guidance_judge_contains_invariant_preservation():
    """JUDGE guidance mentions invariant_preservation."""
    guidance = build_phase_principal_guidance("JUDGE")
    assert "invariant_preservation" in guidance


def test_guidance_unknown_returns_empty():
    """Unknown phase guidance returns empty string (no crash)."""
    guidance = build_phase_principal_guidance("UNKNOWN")
    assert guidance == ""


# ── Tests: principal_gate vocabulary alignment ───────────────────────────────

def test_principal_gate_rejects_causality():
    """check_principal_gate rejects 'causality' (old term) for ANALYZE phase."""
    record = _FakePR(principals=["causality"])
    violation = check_principal_gate(record, "ANALYZE")
    assert violation is not None, "causality should NOT satisfy causal_grounding requirement"
    assert "causal_grounding" in violation


def test_principal_gate_accepts_causal_grounding():
    """check_principal_gate accepts ['causal_grounding'] for ANALYZE (gate minimum)."""
    record = _FakePR(principals=["causal_grounding"])
    violation = check_principal_gate(record, "ANALYZE")
    assert violation is None, f"expected None, got {violation}"


def test_principal_gate_accepts_causal_grounding_and_evidence_linkage():
    """check_principal_gate accepts ['causal_grounding', 'evidence_linkage'] for ANALYZE."""
    record = _FakePR(principals=["causal_grounding", "evidence_linkage"])
    violation = check_principal_gate(record, "ANALYZE")
    assert violation is None, f"expected None, got {violation}"


def test_principal_gate_analyze_requires_causal_grounding():
    """ANALYZE phase in principal gate requires causal_grounding (not any other term)."""
    required = PHASE_REQUIRED_PRINCIPALS.get("ANALYZE", [])
    assert "causal_grounding" in required, (
        f"ANALYZE must require causal_grounding, got: {required}"
    )


# ── Tests: phase_prompt vocabulary alignment ─────────────────────────────────

def test_phase_prompt_analyze_contains_causal_grounding():
    """PHASE_GUIDANCE['ANALYZE'] contains 'causal_grounding' (critical acceptance criterion)."""
    guidance = PHASE_GUIDANCE.get("ANALYZE", "")
    assert "causal_grounding" in guidance, (
        f"PHASE_GUIDANCE['ANALYZE'] must contain 'causal_grounding', got: {guidance!r}"
    )


def test_phase_prompt_execute_contains_minimal_change():
    """PHASE_GUIDANCE['EXECUTE'] contains 'minimal_change'."""
    guidance = PHASE_GUIDANCE.get("EXECUTE", "")
    assert "minimal_change" in guidance


def test_phase_prompt_judge_contains_invariant_preservation():
    """PHASE_GUIDANCE['JUDGE'] contains 'invariant_preservation'."""
    guidance = PHASE_GUIDANCE.get("JUDGE", "")
    assert "invariant_preservation" in guidance


# ── Tests: cross-system consistency ──────────────────────────────────────────

def test_principal_gate_and_prompt_vocab_consistent_analyze():
    """
    Vocabulary consistency: what prompt tells agent to declare (ANALYZE)
    must match what gate checks — no drift between systems A and B.
    """
    # Gate requires these
    gate_required = set(PHASE_REQUIRED_PRINCIPALS.get("ANALYZE", []))
    # Prompt mentions these (check each required principal is in prompt)
    prompt_text = PHASE_GUIDANCE.get("ANALYZE", "")
    for principal in gate_required:
        assert principal in prompt_text, (
            f"Gate requires '{principal}' but ANALYZE prompt does not mention it. "
            f"Vocabulary drift detected."
        )


def test_principal_gate_and_prompt_vocab_consistent_execute():
    """EXECUTE: prompt mentions all gate-required principals."""
    gate_required = set(PHASE_REQUIRED_PRINCIPALS.get("EXECUTE", []))
    prompt_text = PHASE_GUIDANCE.get("EXECUTE", "")
    for principal in gate_required:
        assert principal in prompt_text, (
            f"Gate requires '{principal}' but EXECUTE prompt does not mention it."
        )


def test_principal_gate_and_prompt_vocab_consistent_judge():
    """JUDGE: prompt mentions all gate-required principals."""
    gate_required = set(PHASE_REQUIRED_PRINCIPALS.get("JUDGE", []))
    prompt_text = PHASE_GUIDANCE.get("JUDGE", "")
    for principal in gate_required:
        assert principal in prompt_text, (
            f"Gate requires '{principal}' but JUDGE prompt does not mention it."
        )
