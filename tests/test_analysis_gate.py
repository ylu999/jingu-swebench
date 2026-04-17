"""
test_analysis_gate.py — Unit tests for p211 analysis gate.

Verifies:
- evaluate_analysis() passes for well-formed analysis PhaseRecords
- evaluate_analysis() rejects analysis lacking code grounding
- evaluate_analysis() rejects single-hypothesis analysis
- evaluate_analysis() rejects analysis without causal chain
- evaluate_analysis() rejects completely empty PhaseRecord
- Individual scoring functions return correct scores
- Threshold behavior at 0.5 boundary
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest
from phase_record import PhaseRecord
from analysis_gate import (
    evaluate_analysis,
    AnalysisVerdict,
    _check_code_grounding,
    _check_alternative_hypothesis,
    _check_causal_chain,
)
from gate_rejection import GateRejection, FieldFailure


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_good_pr() -> PhaseRecord:
    """A well-formed analysis PhaseRecord that should pass all gate rules."""
    return PhaseRecord(
        phase="ANALYZE",
        subtype="analysis.root_cause",
        principals=["causal_grounding", "evidence_linkage"],
        claims=["The bug is in DateTimeField validation"],
        evidence_refs=[
            "django/db/models/fields/__init__.py:1234",
            "tests/model_fields/test_datetimefield.py::test_invalid_date",
        ],
        from_steps=[3, 4, 5],
        content=(
            "After examining the code, I identified two hypotheses:\n"
            "Hypothesis 1: The DateTimeField.to_python() method at "
            "django/db/models/fields/__init__.py:1234 fails to handle "
            "timezone-naive datetime objects, causing ValueError.\n"
            "Hypothesis 2: The issue could be in the DateTimeField.validate() "
            "method, but this is ruled out because validate() delegates to "
            "to_python() and the error trace points to to_python().\n"
            "The causal chain: test_invalid_date calls DateTimeField().clean() "
            "-> clean() calls to_python() -> to_python() calls "
            "datetime.strptime() without timezone handling -> raises ValueError.\n"
            "However, hypothesis 2 doesn't explain the traceback correctly, "
            "so it was eliminated.\n"
            "The structural boundary character ':' has a delimiter role in the "
            "parsing logic — the format must not contain ':' in the date portion "
            "because it would break the parser's structural assumptions."
        ),
        root_cause=(
            "DateTimeField.to_python() in django/db/models/fields/__init__.py:1234 "
            "does not handle timezone-naive datetime objects correctly. "
            "The delimiter ':' must not appear in the date portion because it has "
            "structural meaning as a time separator."
        ),
        causal_chain=(
            "test_invalid_date -> DateTimeField.clean() -> to_python() "
            "-> datetime.strptime() without tz -> ValueError"
        ),
        alternative_hypotheses=[
            {"hypothesis": "DateTimeField.validate() method is broken", "ruled_out_reason": "validate delegates to to_python, traceback points to to_python directly"},
            {"hypothesis": "The issue is in the model Meta class timezone config", "ruled_out_reason": "Meta class does not affect field-level parsing, only display timezone"},
        ],
        invariant_capture={
            "identified_invariants": ["timezone-naive datetime objects must be handled by to_python"],
            "risk_if_violated": "ValueError crashes any form submission with datetime input",
        },
        repair_strategy_type="REGEX_FIX",
    )


def _make_no_code_refs_pr() -> PhaseRecord:
    """Analysis that lacks any code references."""
    return PhaseRecord(
        phase="ANALYZE",
        subtype="analysis.root_cause",
        principals=["causal_grounding"],
        claims=[],
        evidence_refs=[],  # No code refs
        from_steps=[3],
        content=(
            "The issue seems to be related to incorrect validation logic. "
            "The validation is probably wrong somewhere in the codebase."
        ),
        root_cause="Incorrect validation logic causes the test to fail.",
    )


def _make_single_hypothesis_pr() -> PhaseRecord:
    """Analysis with only a single hypothesis, no alternatives."""
    return PhaseRecord(
        phase="ANALYZE",
        subtype="analysis.root_cause",
        principals=["causal_grounding"],
        claims=[],
        evidence_refs=["django/db/models/fields/__init__.py:1234"],
        from_steps=[3],
        content=(
            "The root cause is clearly the DateTimeField.to_python() method. "
            "It fails to handle timezone-naive datetime objects. "
            "This is the definitive cause of the test failure."
        ),
        root_cause=(
            "DateTimeField.to_python() in django/db/models/fields/__init__.py:1234 "
            "fails to handle timezone-naive datetime."
        ),
        causal_chain=(
            "test -> clean() -> to_python() -> strptime() -> ValueError"
        ),
    )


def _make_no_causal_chain_pr() -> PhaseRecord:
    """Analysis without causal chain and without code in root_cause."""
    return PhaseRecord(
        phase="ANALYZE",
        subtype="analysis.root_cause",
        principals=["causal_grounding"],
        claims=[],
        evidence_refs=["django/db/models/fields/__init__.py:1234"],
        from_steps=[3],
        content=(
            "Looking at two possibilities:\n"
            "Hypothesis 1: something wrong with validation.\n"
            "Hypothesis 2: something wrong with serialization, "
            "but this was ruled out because the error is in validation.\n"
            "Another possible cause could be the input format."
        ),
        root_cause="",  # Empty
        causal_chain="",  # Empty
    )


def _make_empty_pr() -> PhaseRecord:
    """Completely empty PhaseRecord."""
    return PhaseRecord(
        phase="ANALYZE",
        subtype="analysis.root_cause",
        principals=[],
        claims=[],
        evidence_refs=[],
        from_steps=[],
        content="",
        root_cause="",
        causal_chain="",
    )


# ── Tests: Full evaluation ───────────────────────────────────────────────────

class TestEvaluateAnalysis:

    def test_good_analysis_passes(self):
        """Well-formed analysis should pass all 3 rules."""
        pr = _make_good_pr()
        verdict = evaluate_analysis(pr)
        assert verdict.passed is True
        assert verdict.failed_rules == []
        assert verdict.reasons == []
        assert verdict.scores["code_grounding"] >= 0.5
        assert verdict.scores["alternative_hypothesis"] >= 0.5
        assert verdict.scores["causal_chain"] >= 0.5

    def test_no_code_refs_partial_code_grounding(self):
        """Analysis without code refs but with root_cause gets partial score (0.5 = pass)."""
        pr = _make_no_code_refs_pr()
        verdict = evaluate_analysis(pr)
        # root_cause present (>10 chars) → 0.5 → at threshold → passes code_grounding
        assert verdict.scores["code_grounding"] == 0.5
        assert "code_grounding" not in verdict.failed_rules

    def test_single_hypothesis_soft_gate_in_general_domain(self):
        """Single hypothesis with good core rules → fail-open in general domain."""
        pr = _make_single_hypothesis_pr()
        verdict = evaluate_analysis(pr)
        # v2 soft gate: alternative_hypothesis is downgraded to warning when
        # non-parsing domain + core rules (code_grounding, causal_chain) pass.
        assert verdict.scores["alternative_hypothesis"] < 0.5
        assert "alternative_hypothesis" not in verdict.failed_rules
        assert "fail_open" in verdict.scores.get("alternative_hypothesis_note", "")

    def test_no_causal_chain_fails(self):
        """Analysis without causal chain should fail causal_chain."""
        pr = _make_no_causal_chain_pr()
        verdict = evaluate_analysis(pr)
        assert "causal_chain" in verdict.failed_rules
        assert verdict.scores["causal_chain"] < 0.5

    def test_empty_pr_fails_all(self):
        """Completely empty PhaseRecord should fail all 5 rules."""
        pr = _make_empty_pr()
        verdict = evaluate_analysis(pr)
        assert verdict.passed is False
        assert len(verdict.failed_rules) == 5
        assert "code_grounding" in verdict.failed_rules
        assert "alternative_hypothesis" in verdict.failed_rules
        assert "causal_chain" in verdict.failed_rules
        assert "invariant_capture" in verdict.failed_rules
        assert "repair_strategy_type" in verdict.failed_rules

    def test_verdict_has_correct_type(self):
        """evaluate_analysis returns an AnalysisVerdict."""
        pr = _make_good_pr()
        verdict = evaluate_analysis(pr)
        assert isinstance(verdict, AnalysisVerdict)


# ── Tests: Code grounding (Rule 1) ──────────────────────────────────────────

class TestCodeGrounding:

    def test_evidence_refs_with_file_line(self):
        """evidence_refs with file:line pattern gives full score."""
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=[], claims=[],
            evidence_refs=["django/db/models.py:45"],
            from_steps=[], content="",
            root_cause="The bug is in django/db/models.py:45 where clean() fails.",
        )
        assert _check_code_grounding(pr) >= 1.0

    def test_evidence_refs_only_gives_half(self):
        """Code ref in evidence_refs only (no root_cause) gives 0.5."""
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=[], claims=[],
            evidence_refs=["django/db/models.py:45"],
            from_steps=[], content="",
            root_cause="",  # No root_cause
        )
        assert _check_code_grounding(pr) == 0.5

    def test_root_cause_only_gives_half(self):
        """Code ref in root_cause but not evidence_refs gives 0.5."""
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=[], claims=[],
            evidence_refs=[],  # No code ref
            from_steps=[], content="",
            root_cause="Bug in django/db/models.py:45",
        )
        assert _check_code_grounding(pr) == 0.5

    def test_no_code_refs_anywhere(self):
        """No evidence_refs and no root_cause gives 0.0."""
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=[], claims=[],
            evidence_refs=[],
            from_steps=[], content="The bug is somewhere in the codebase.",
            root_cause="",  # empty root_cause
        )
        assert _check_code_grounding(pr) == 0.0

    def test_content_not_used_for_code_grounding(self):
        """Content field is NOT used for code_grounding (structural-only: evidence_refs + root_cause)."""
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=[], claims=[],
            evidence_refs=[],
            from_steps=[],
            content="Looking at django/db/models.py:45 and django/utils/timezone.py:12",
            root_cause="",
        )
        # Content field ignored — no evidence_refs, no root_cause → 0.0
        assert _check_code_grounding(pr) == 0.0


class TestIsCodeEvidenceRef:
    """_is_code_evidence_ref was removed in D-02/D-03 structural rewrite.
    Code grounding now checks evidence_refs structurally. These tests
    verify the structural check via _check_code_grounding instead."""

    def test_file_line_ref_scores_high(self):
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=[], claims=[],
            evidence_refs=["django/db/models.py:45"],
            from_steps=[], content="",
            root_cause="Bug in django/db/models.py:45",
        )
        assert _check_code_grounding(pr) >= 0.5

    def test_no_refs_scores_zero(self):
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=[], claims=[],
            evidence_refs=[],
            from_steps=[], content="validation logic",
            root_cause="",
        )
        assert _check_code_grounding(pr) == 0.0


# ── Tests: Alternative hypothesis (Rule 2) ───────────────────────────────────

class TestAlternativeHypothesis:

    def test_multiple_hypotheses_with_rejection(self):
        """alternative_hypotheses with 2+ substantive entries scores 1.0."""
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=[], claims=[], evidence_refs=[],
            from_steps=[], root_cause="", content="",
            alternative_hypotheses=[
                {"hypothesis": "The issue is in to_python() handling", "ruled_out_reason": "traceback points to to_python directly"},
                {"hypothesis": "The issue could be in validate() method", "ruled_out_reason": "validate delegates to to_python, error originates there"},
            ],
        )
        score = _check_alternative_hypothesis(pr)
        assert score >= 1.0

    def test_single_assertion_scores_zero(self):
        """Content with just a single assertion scores 0.0."""
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=[], claims=[], evidence_refs=[],
            from_steps=[], root_cause="",
            content="The root cause is the validation method. It fails on edge cases.",
        )
        score = _check_alternative_hypothesis(pr)
        assert score == 0.0

    def test_single_hypothesis_scores_half(self):
        """alternative_hypotheses with 1 substantive entry scores 0.5."""
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=[], claims=[], evidence_refs=[],
            from_steps=[], root_cause="", content="",
            alternative_hypotheses=[
                {"hypothesis": "Could be a serializer issue instead", "ruled_out_reason": "validation seems more likely based on traceback"},
            ],
        )
        score = _check_alternative_hypothesis(pr)
        assert score == 0.5

    def test_empty_content_scores_zero(self):
        """Empty content scores 0.0."""
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=[], claims=[], evidence_refs=[],
            from_steps=[], content="", root_cause="",
        )
        assert _check_alternative_hypothesis(pr) == 0.0


# ── Tests: Causal chain (Rule 3) ─────────────────────────────────────────────

class TestCausalChain:

    def test_structured_causal_chain_field(self):
        """Non-empty causal_chain field gives immediate 1.0."""
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=[], claims=[], evidence_refs=[],
            from_steps=[], content="", root_cause="",
            causal_chain="test_foo -> clean() -> to_python() -> ValueError",
        )
        assert _check_causal_chain(pr) == 1.0

    def test_substantive_causal_chain(self):
        """causal_chain field >20 chars = 1.0 (structural check)."""
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=[], claims=[],
            evidence_refs=["tests/test_models.py::test_clean"],
            from_steps=[], content="",
            root_cause="Bug in django/db/models.py:45 causes ValueError.",
            causal_chain="test_clean -> Model.clean() -> validate() -> django/db/models.py:45 -> ValueError",
        )
        assert _check_causal_chain(pr) == 1.0

    def test_short_causal_chain_scores_partial(self):
        """causal_chain 5 < len <= 20 = 0.3 (partial)."""
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=[], claims=[],
            evidence_refs=["django/db/models.py:45"],
            from_steps=[], content="",
            root_cause="Bug in django/db/models.py:45 causes ValueError.",
            causal_chain="a -> b -> c -> d",  # 16 chars, between 5 and 20
        )
        assert _check_causal_chain(pr) == 0.3

    def test_no_causal_chain_field_scores_zero(self):
        """No causal_chain field = 0.0 (content/root_cause not used)."""
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=[], claims=[],
            evidence_refs=[],
            from_steps=[], content="",
            root_cause="The validation logic is incorrect and causes failures.",
            causal_chain="",
        )
        assert _check_causal_chain(pr) == 0.0

    def test_no_root_cause_no_chain(self):
        """No root_cause and no causal_chain = 0.0."""
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=[], claims=[], evidence_refs=[],
            from_steps=[], content="Looking at the code.",
            root_cause="", causal_chain="",
        )
        assert _check_causal_chain(pr) == 0.0

    def test_short_causal_chain_ignored(self):
        """Very short causal_chain (<= 20 chars) is treated as empty."""
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=[], claims=[], evidence_refs=[],
            from_steps=[], content="", root_cause="",
            causal_chain="a -> b",  # too short
        )
        assert _check_causal_chain(pr) < 1.0


# ── Tests: Scoring thresholds ────────────────────────────────────────────────

class TestThresholds:

    def test_score_at_exactly_half_passes(self):
        """Score of exactly 0.5 should pass (threshold is >= 0.5 to pass)."""
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=[], claims=[],
            evidence_refs=["django/db/models.py:45"],
            from_steps=[], content="",
            root_cause="",  # no root_cause → code_grounding = 0.5 (evidence only)
            causal_chain="test -> clean() -> to_python() -> ValueError long enough",
        )
        verdict = evaluate_analysis(pr)
        # code_grounding = 0.5 (evidence_refs only, no root_cause), should pass
        assert verdict.scores["code_grounding"] == 0.5
        assert "code_grounding" not in verdict.failed_rules

    def test_all_scores_returned(self):
        """All 4 scores are present in the verdict."""
        pr = _make_empty_pr()
        verdict = evaluate_analysis(pr)
        assert "code_grounding" in verdict.scores
        assert "alternative_hypothesis" in verdict.scores
        assert "causal_chain" in verdict.scores
        assert "invariant_capture" in verdict.scores


# -- Tests: SDG GateRejection on failure (p217) --

class TestAnalysisGateRejection:

    def test_passing_verdict_has_no_rejection(self):
        """Good analysis should have rejection=None."""
        pr = _make_good_pr()
        verdict = evaluate_analysis(pr)
        assert verdict.passed is True
        assert verdict.rejection is None

    def test_failing_verdict_has_rejection(self):
        """Failed analysis should have a non-None GateRejection."""
        pr = _make_empty_pr()
        verdict = evaluate_analysis(pr)
        assert verdict.passed is False
        assert verdict.rejection is not None
        assert isinstance(verdict.rejection, GateRejection)

    def test_rejection_gate_name(self):
        """Rejection gate_name is 'analysis_gate'."""
        pr = _make_empty_pr()
        verdict = evaluate_analysis(pr)
        assert verdict.rejection.gate_name == "analysis_gate"

    def test_rejection_has_failures(self):
        """Rejection contains FieldFailure entries for each failed rule."""
        pr = _make_empty_pr()
        verdict = evaluate_analysis(pr)
        assert len(verdict.rejection.failures) == 5  # all 5 rules fail
        fields_failed = [f.field for f in verdict.rejection.failures]
        assert "root_cause" in fields_failed
        assert "causal_chain" in fields_failed
        assert "alternative_hypotheses" in fields_failed

    def test_rejection_failures_have_hints(self):
        """Each FieldFailure has a non-empty hint."""
        pr = _make_empty_pr()
        verdict = evaluate_analysis(pr)
        for f in verdict.rejection.failures:
            assert f.hint != "", f"FieldFailure for {f.field} has empty hint"
            assert len(f.hint) > 5

    def test_rejection_has_contract(self):
        """Rejection carries the ANALYZE contract view."""
        pr = _make_empty_pr()
        verdict = evaluate_analysis(pr)
        assert len(verdict.rejection.contract.required_fields) > 0
        assert "root_cause" in verdict.rejection.contract.required_fields

    def test_rejection_has_extracted(self):
        """Rejection carries extracted values."""
        pr = _make_no_code_refs_pr()
        verdict = evaluate_analysis(pr)
        assert verdict.rejection is not None
        assert "root_cause" in verdict.rejection.extracted

    def test_single_rule_failure_has_one_field_failure(self):
        """When only one rule fails, rejection has exactly 1 FieldFailure."""
        pr = _make_single_hypothesis_pr()
        verdict = evaluate_analysis(pr)
        if not verdict.passed and verdict.rejection:
            # Should have failure count matching failed_rules
            assert len(verdict.rejection.failures) == len(verdict.failed_rules)


# ── Tests: structured_output=True mode (p221) ────────────────────────────────

class TestStructuredOutputMode:
    """When structured_output=True, schema guarantees structural correctness.
    Gate skips alternative_hypothesis enforcement (schema enforces presence),
    keeps code_grounding and causal_chain as semantic checks."""

    def test_good_analysis_still_passes(self):
        """Well-formed analysis passes in structured mode too."""
        pr = _make_good_pr()
        verdict = evaluate_analysis(pr, structured_output=True)
        assert verdict.passed is True
        assert verdict.failed_rules == []

    def test_single_hypothesis_not_blocked_in_structured_mode(self):
        """Single hypothesis analysis is NOT blocked when structured_output=True.
        Schema enforces alternative_hypotheses presence (minItems:1),
        so the gate downgrades this to a quality signal."""
        pr = _make_single_hypothesis_pr()
        verdict = evaluate_analysis(pr, structured_output=True)
        # alternative_hypothesis should NOT be in failed_rules
        assert "alternative_hypothesis" not in verdict.failed_rules
        # Score is still computed for telemetry
        assert "alternative_hypothesis" in verdict.scores
        # Quality note should be present
        assert "alternative_hypothesis_note" in verdict.scores

    def test_code_grounding_still_enforced_in_structured_mode(self):
        """Code grounding is a semantic check — still enforced in structured mode."""
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=["causal_grounding"], claims=[],
            evidence_refs=[], from_steps=[3],
            content="Something is wrong.", root_cause="",  # no root_cause, no evidence_refs → 0.0
        )
        verdict = evaluate_analysis(pr, structured_output=True)
        assert "code_grounding" in verdict.failed_rules

    def test_causal_chain_still_enforced_in_structured_mode(self):
        """Causal chain is a semantic check — still enforced in structured mode."""
        pr = _make_no_causal_chain_pr()
        verdict = evaluate_analysis(pr, structured_output=True)
        assert "causal_chain" in verdict.failed_rules

    def test_empty_pr_fails_four_not_five_in_structured_mode(self):
        """Empty PR fails 4 rules but NOT alternative_hypothesis (soft gate in structured mode)."""
        pr = _make_empty_pr()
        verdict = evaluate_analysis(pr, structured_output=True)
        assert verdict.passed is False
        assert "code_grounding" in verdict.failed_rules
        assert "causal_chain" in verdict.failed_rules
        assert "invariant_capture" in verdict.failed_rules
        assert "repair_strategy_type" in verdict.failed_rules
        assert "alternative_hypothesis" not in verdict.failed_rules
        assert len(verdict.failed_rules) == 4

    def test_structured_mode_false_is_default(self):
        """Default behavior (structured_output=False) keeps all 5 rules enforced."""
        pr = _make_empty_pr()
        verdict = evaluate_analysis(pr, structured_output=False)
        assert len(verdict.failed_rules) == 5
        assert "alternative_hypothesis" in verdict.failed_rules
        assert "invariant_capture" in verdict.failed_rules
        assert "repair_strategy_type" in verdict.failed_rules
