"""
test_principal_inference.py — Tests for principal_inference.py (p194 + p195)

Tests cover:
  - infer_principals: 4 deterministic rules (p194)
  - diff_principals: three-way split (missing_required / missing_expected / fake) (p194)
  - V3 stub: build_retry_hints returns [] (p194)
  - Rule registry: register_rule, get_rules, run_inference (p195)
  - InferenceResult: signals, explanation fields (p195)
  - Backward-compat: diff_principals accepts InferredPrincipalResult (p195)
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
    register_rule,
    get_rules,
    run_inference,
    InferenceRule,
    InferenceResult,
    InferredPrincipalResult,
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
        root_cause="",
        causal_chain="",
    ):
        self.phase = phase
        self.evidence_refs = evidence_refs or []
        self.claims = claims or []
        self.from_steps = from_steps or []
        self.content = content
        self.principals = principals or []
        self.root_cause = root_cause
        self.causal_chain = causal_chain


# ── infer_principals tests ────────────────────────────────────────────────────

def test_infer_causal_grounding():
    """evidence_refs with code ref + root_cause + causal_chain => causal_grounding inferred (structural check)."""
    rec = FakePhaseRecord(
        phase="ANALYZE",
        evidence_refs=["django/db/models.py:42"],
        content="The bug occurs because the state machine transitions incorrectly.",
        root_cause="State machine transitions incorrectly due to missing guard",
        causal_chain="Test fails because guard condition is missing in the transition handler at models.py:42 which skips validation",
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
    """evidence_refs alone (no from_steps) DOES infer evidence_linkage (P1 fix: OR not AND).

    Rule was changed: evidence_refs OR from_steps is sufficient. from_steps=[] at record
    extraction time is the norm (gate step indices populated at runtime, not by agent).
    """
    rec = FakePhaseRecord(
        phase="ANALYZE",
        evidence_refs=["ref1"],
        from_steps=[],
        content="some content",
    )
    result = infer_principals(rec)
    assert "evidence_linkage" in result, (
        f"evidence_refs alone should infer evidence_linkage (P1 fix: OR not AND), got {result}"
    )


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
    """declared has principal that inferred does not => fake (when rule ran for subtype).

    CC2: inferrable = rules-that-ran for the subtype.
    causal_grounding has applies_to=["analysis.root_cause"], so phase="ANALYZE" is needed
    to make it inferrable. With empty phase, causal_grounding is NOT inferrable → not fake.
    """
    result = diff_principals(
        declared=["causal_grounding"],
        inferred=[],
        phase="ANALYZE",  # causal_grounding applies to analysis.root_cause (ANALYZE)
    )
    assert "causal_grounding" in result["fake"], (
        f"causal_grounding declared but not inferred should be fake for ANALYZE, got {result}"
    )


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
    """ANALYZE phase: declared has all required but not expected => missing_expected only.

    P1.2 contract: required=["causal_grounding", "evidence_linkage", "alternative_hypothesis_check"].
    Must declare all three required to avoid missing_required.
    expected principals (e.g. ontology_alignment) go in missing_expected.
    """
    result = diff_principals(
        declared=["causal_grounding", "evidence_linkage", "alternative_hypothesis_check"],
        inferred=["causal_grounding", "evidence_linkage", "alternative_hypothesis_check"],
        phase="ANALYZE",
    )
    assert result["missing_required"] == [], (
        f"Expected no missing_required when all required declared, got {result}"
    )
    assert result["fake"] == [], (
        f"Expected no fake when declared matches inferred, got {result}"
    )
    # ontology_alignment is expected but not declared => missing_expected
    assert len(result["missing_expected"]) > 0, (
        f"Expected some missing_expected (e.g. ontology_alignment) for ANALYZE, got {result}"
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


# ── p195: Rule registry tests ─────────────────────────────────────────────────

def test_register_rule_and_get_rules():
    """register_rule adds to registry; get_rules returns it."""
    before = len(get_rules())
    new_rule = InferenceRule(
        principal="test_principal_xyz",
        infer=lambda r: (0.9, ["test_signal"], "test explanation"),
        applies_to=None,
        threshold=0.7,
    )
    register_rule(new_rule)
    after = get_rules()
    assert len(after) == before + 1
    assert any(r.principal == "test_principal_xyz" for r in after), (
        f"Expected test_principal_xyz in registry, got {[r.principal for r in after]}"
    )


def test_rule_applies_to_filter():
    """A rule with applies_to=['execution.code_patch'] does NOT fire for analysis.root_cause subtype."""
    rec = FakePhaseRecord(
        phase="EXECUTE",
        evidence_refs=["ref1"],
        from_steps=[1],
        content="small patch\n" * 5,
    )
    # run_inference with analysis.root_cause subtype — minimal_change should not appear
    result = run_inference(rec, "analysis.root_cause")
    assert "minimal_change" not in result.present, (
        f"minimal_change should not fire for analysis.root_cause, got {result.present}"
    )


def test_inference_result_has_signals():
    """run_inference result.details for causal_grounding has non-empty signals list."""
    rec = FakePhaseRecord(
        phase="ANALYZE",
        evidence_refs=["ref1"],
        content="The bug occurs because the state machine transitions incorrectly.",
        from_steps=[1],
    )
    result = run_inference(rec, "analysis.root_cause")
    assert "causal_grounding" in result.details, (
        f"Expected causal_grounding in details, got {list(result.details.keys())}"
    )
    ir = result.details["causal_grounding"]
    assert isinstance(ir.signals, list), f"Expected list, got {type(ir.signals)}"
    assert len(ir.signals) > 0, f"Expected non-empty signals, got {ir.signals}"


def test_inference_result_has_explanation():
    """run_inference result.details for causal_grounding has non-empty explanation string."""
    rec = FakePhaseRecord(
        phase="ANALYZE",
        evidence_refs=["ref1"],
        content="This fails because the validator skips None values.",
        from_steps=[1],
    )
    result = run_inference(rec, "analysis.root_cause")
    assert "causal_grounding" in result.details
    ir = result.details["causal_grounding"]
    assert isinstance(ir.explanation, str), f"Expected str, got {type(ir.explanation)}"
    assert len(ir.explanation) > 0, f"Expected non-empty explanation, got {repr(ir.explanation)}"


def test_causal_grounding_score_above_threshold():
    """evidence_refs with code refs + root_cause + causal_chain => causal_grounding score >= 0.7 (structural)."""
    rec = FakePhaseRecord(
        phase="ANALYZE",
        evidence_refs=["utils.py:42", "tests/test_utils.py:10"],
        content="The failure occurs because the index is off by one.",
        from_steps=[1, 2],
        root_cause="Off-by-one error in array indexing at utils.py:42",
        causal_chain="Test fails because loop boundary uses < instead of <= causing last element to be skipped",
    )
    result = run_inference(rec, "analysis.root_cause")
    assert "causal_grounding" in result.details
    ir = result.details["causal_grounding"]
    assert ir.score >= 0.7, f"Expected score >= 0.7, got {ir.score}"
    assert "causal_grounding" in result.present, (
        f"Expected causal_grounding in present, got {result.present}"
    )


def test_minimal_change_large_patch():
    """Content with 40+ lines => minimal_change NOT present in run_inference result."""
    large_content = "\n".join(["line"] * 40)  # 39 newlines
    rec = FakePhaseRecord(phase="EXECUTE", content=large_content)
    result = run_inference(rec, "execution.code_patch")
    assert "minimal_change" not in result.present, (
        f"minimal_change should not be present for large patch, got {result.present}"
    )
    assert "minimal_change" in result.absent, (
        f"minimal_change should be in absent for large patch, got {result.absent}"
    )


def test_diff_with_rich_result():
    """diff_principals accepts InferredPrincipalResult and produces same output as list[str]."""
    rec = FakePhaseRecord(
        phase="ANALYZE",
        evidence_refs=["ref1"],
        content="The failure is because the validator skips None.",
        from_steps=[1],
    )
    rich_result = run_inference(rec, "analysis.root_cause")
    # Get list[str] version via infer_principals
    list_result = infer_principals(rec)

    diff_rich = diff_principals(
        declared=["causal_grounding"],
        inferred=rich_result,
        phase="",
    )
    diff_list = diff_principals(
        declared=["causal_grounding"],
        inferred=list_result,
        phase="",
    )
    assert diff_rich["fake"] == diff_list["fake"], (
        f"fake mismatch: rich={diff_rich['fake']} list={diff_list['fake']}"
    )
    assert diff_rich["missing_required"] == diff_list["missing_required"], (
        f"missing_required mismatch"
    )


def test_new_rule_without_engine_change():
    """Registering a new rule makes run_inference return the new principal without engine changes."""
    new_principal = "test_custom_principal_p195"
    custom_rule = InferenceRule(
        principal=new_principal,
        infer=lambda r: (0.9, ["custom_signal"], "Custom rule fired"),
        applies_to=None,
        threshold=0.7,
    )
    register_rule(custom_rule)

    rec = FakePhaseRecord(phase="ANALYZE", content="some content")
    result = run_inference(rec, "analysis.root_cause")
    assert new_principal in result.present, (
        f"Expected {new_principal} in present after register_rule, got {result.present}"
    )
    assert new_principal in result.details, (
        f"Expected {new_principal} in details, got {list(result.details.keys())}"
    )
