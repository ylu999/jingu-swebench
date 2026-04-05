"""
test_principal_inference.py — Tests for principal_inference.py (p194)

Tests cover:
  - infer_principals: 4 deterministic rules
  - diff_principals: three-way split (missing_required / missing_expected / fake)
  - V3 stub: build_retry_hints returns []
"""

import sys
import os

# Add scripts directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from principal_inference import (
    infer_principals,
    diff_principals,
    build_retry_hints,
    RetryHintInput,
    RepairHint,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

class FakePhaseRecord:
    """Minimal PhaseRecord-like object for testing."""
    def __init__(
        self,
        phase="",
        evidence_refs=None,
        claims=None,
        from_steps=None,
        content="",
        principals=None,
    ):
        self.phase = phase
        self.evidence_refs = evidence_refs or []
        self.claims = claims or []
        self.from_steps = from_steps or []
        self.content = content
        self.principals = principals or []


# ── infer_principals tests ────────────────────────────────────────────────────

def test_infer_causal_grounding():
    """evidence_refs non-empty + causal keyword => causal_grounding inferred."""
    rec = FakePhaseRecord(
        phase="ANALYZE",
        evidence_refs=["ref1"],
        content="The bug occurs because the state machine transitions incorrectly.",
    )
    result = infer_principals(rec)
    assert "causal_grounding" in result, f"Expected causal_grounding, got {result}"


def test_infer_causal_grounding_not_inferred_without_evidence():
    """Causal language alone (no evidence_refs) does NOT infer causal_grounding."""
    rec = FakePhaseRecord(
        phase="ANALYZE",
        evidence_refs=[],
        content="Therefore the fix is straightforward.",
    )
    result = infer_principals(rec)
    assert "causal_grounding" not in result, f"Should not have causal_grounding, got {result}"


def test_infer_evidence_linkage():
    """evidence_refs + from_steps both non-empty => evidence_linkage inferred."""
    rec = FakePhaseRecord(
        phase="ANALYZE",
        evidence_refs=["ref1"],
        from_steps=[1, 2],
        content="Based on the trace above.",
    )
    result = infer_principals(rec)
    assert "evidence_linkage" in result, f"Expected evidence_linkage, got {result}"


def test_infer_evidence_linkage_not_inferred_without_from_steps():
    """evidence_refs alone (no from_steps) does NOT infer evidence_linkage."""
    rec = FakePhaseRecord(
        phase="ANALYZE",
        evidence_refs=["ref1"],
        from_steps=[],
        content="some content",
    )
    result = infer_principals(rec)
    assert "evidence_linkage" not in result, f"Should not have evidence_linkage, got {result}"


def test_infer_minimal_change_short_patch():
    """EXECUTE phase + content <= 30 newlines => minimal_change inferred."""
    short_content = "\n".join(["line"] * 20)  # 19 newlines
    rec = FakePhaseRecord(phase="EXECUTE", content=short_content)
    result = infer_principals(rec)
    assert "minimal_change" in result, f"Expected minimal_change, got {result}"


def test_infer_minimal_change_long_patch():
    """EXECUTE phase + content > 30 newlines => minimal_change NOT inferred."""
    long_content = "\n".join(["line"] * 40)  # 39 newlines
    rec = FakePhaseRecord(phase="EXECUTE", content=long_content)
    result = infer_principals(rec)
    assert "minimal_change" not in result, f"Should not have minimal_change, got {result}"


def test_infer_alternative_hypothesis_check():
    """ANALYZE phase + 'alternative' keyword => alternative_hypothesis_check inferred."""
    rec = FakePhaseRecord(
        phase="ANALYZE",
        content="One alternative approach would be to patch the validator instead.",
    )
    result = infer_principals(rec)
    assert "alternative_hypothesis_check" in result, f"Expected alt_hyp_check, got {result}"


def test_infer_invariant_preservation():
    """JUDGE phase + 'preserve' keyword => invariant_preservation inferred."""
    rec = FakePhaseRecord(
        phase="JUDGE",
        content="This change does not change the public API and will preserve backward compat.",
    )
    result = infer_principals(rec)
    assert "invariant_preservation" in result, f"Expected invariant_preservation, got {result}"


def test_infer_empty_record():
    """Empty PhaseRecord => empty inferred list (no crash)."""
    rec = FakePhaseRecord()
    result = infer_principals(rec)
    assert isinstance(result, list)
    assert len(result) == 0


# ── diff_principals tests (three-way split) ──────────────────────────────────

def test_diff_fake_principal():
    """declared has principal that inferred does not => fake."""
    result = diff_principals(
        declared=["causal_grounding"],
        inferred=[],
        phase="",
    )
    assert result["fake"] == ["causal_grounding"]
    assert result["missing_required"] == []
    assert result["missing_expected"] == []


def test_diff_missing_required():
    """ANALYZE phase: required=["causal_grounding"], declared=[], inferred=[] => missing_required."""
    result = diff_principals(
        declared=[],
        inferred=[],
        phase="ANALYZE",
    )
    # causal_grounding is required for ANALYZE per subtype_contracts.py
    assert "causal_grounding" in result["missing_required"], (
        f"Expected causal_grounding in missing_required, got {result}"
    )
    assert result["fake"] == []


def test_diff_missing_expected():
    """ANALYZE phase: declared has required but not expected => missing_expected only."""
    # For ANALYZE: required=["causal_grounding"], expected=["evidence_linkage", "alternative_hypothesis_check"]
    result = diff_principals(
        declared=["causal_grounding"],
        inferred=["causal_grounding"],
        phase="ANALYZE",
    )
    assert result["missing_required"] == []
    assert result["fake"] == []
    # evidence_linkage and/or alternative_hypothesis_check should be missing_expected
    assert len(result["missing_expected"]) > 0, (
        f"Expected some missing_expected for ANALYZE, got {result}"
    )


def test_diff_clean():
    """Declared matches inferred with no phase contract => all empty."""
    result = diff_principals(
        declared=["causal_grounding"],
        inferred=["causal_grounding"],
        phase="",
    )
    assert result["fake"] == []
    assert result["missing_required"] == []
    assert result["missing_expected"] == []


def test_diff_case_insensitive():
    """Principal comparison is case-insensitive."""
    result = diff_principals(
        declared=["Causal_Grounding"],
        inferred=["causal_grounding"],
        phase="",
    )
    assert result["fake"] == []
    assert result["missing_required"] == []


# ── V3 stub tests ─────────────────────────────────────────────────────────────

def test_v3_stub_returns_empty():
    """build_retry_hints stub always returns empty list."""
    inp = RetryHintInput(
        subtype="analysis.root_cause",
        missing_required=["causal_grounding"],
        missing_expected=["evidence_linkage"],
        fake=["fake_thing"],
    )
    result = build_retry_hints(inp)
    assert result == [], f"Expected [], got {result}"
    assert isinstance(result, list)


def test_v3_stub_return_type():
    """build_retry_hints return value is a list of RepairHint (or empty)."""
    inp = RetryHintInput(subtype="judge.verification")
    result = build_retry_hints(inp)
    assert isinstance(result, list)
    for item in result:
        assert isinstance(item, RepairHint)
