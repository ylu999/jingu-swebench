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
    _is_code_evidence_ref,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_good_pr() -> PhaseRecord:
    """A well-formed analysis PhaseRecord that should pass all 3 rules."""
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
            "so it was eliminated."
        ),
        root_cause=(
            "DateTimeField.to_python() in django/db/models/fields/__init__.py:1234 "
            "does not handle timezone-naive datetime objects correctly."
        ),
        causal_chain=(
            "test_invalid_date -> DateTimeField.clean() -> to_python() "
            "-> datetime.strptime() without tz -> ValueError"
        ),
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

    def test_no_code_refs_fails_code_grounding(self):
        """Analysis without code references should fail code_grounding."""
        pr = _make_no_code_refs_pr()
        verdict = evaluate_analysis(pr)
        assert verdict.passed is False
        assert "code_grounding" in verdict.failed_rules
        assert verdict.scores["code_grounding"] < 0.5

    def test_single_hypothesis_fails_alternative(self):
        """Single hypothesis analysis should fail alternative_hypothesis."""
        pr = _make_single_hypothesis_pr()
        verdict = evaluate_analysis(pr)
        assert "alternative_hypothesis" in verdict.failed_rules
        assert verdict.scores["alternative_hypothesis"] < 0.5

    def test_no_causal_chain_fails(self):
        """Analysis without causal chain should fail causal_chain."""
        pr = _make_no_causal_chain_pr()
        verdict = evaluate_analysis(pr)
        assert "causal_chain" in verdict.failed_rules
        assert verdict.scores["causal_chain"] < 0.5

    def test_empty_pr_fails_all(self):
        """Completely empty PhaseRecord should fail all 3 rules."""
        pr = _make_empty_pr()
        verdict = evaluate_analysis(pr)
        assert verdict.passed is False
        assert len(verdict.failed_rules) == 3
        assert "code_grounding" in verdict.failed_rules
        assert "alternative_hypothesis" in verdict.failed_rules
        assert "causal_chain" in verdict.failed_rules

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
        """Code ref in evidence_refs but not root_cause gives 0.5."""
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=[], claims=[],
            evidence_refs=["django/db/models.py:45"],
            from_steps=[], content="",
            root_cause="The validation logic is wrong.",  # No code ref
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
        """No code references anywhere gives 0.0."""
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=[], claims=[],
            evidence_refs=[],
            from_steps=[], content="The bug is somewhere in the codebase.",
            root_cause="Something is wrong with the validation.",
        )
        assert _check_code_grounding(pr) == 0.0

    def test_content_fallback_with_code_refs(self):
        """Content field with 2+ code refs gives 0.5 as fallback."""
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=[], claims=[],
            evidence_refs=[],
            from_steps=[],
            content="Looking at django/db/models.py:45 and django/utils/timezone.py:12",
            root_cause="",
        )
        assert _check_code_grounding(pr) == 0.5


class TestIsCodeEvidenceRef:

    def test_file_line_ref(self):
        assert _is_code_evidence_ref("django/db/models.py:45") is True

    def test_path_ref(self):
        assert _is_code_evidence_ref("django/db/models.py") is True

    def test_test_name_with_double_colon(self):
        assert _is_code_evidence_ref("tests/test_models.py::test_clean") is True

    def test_plain_text_not_code(self):
        assert _is_code_evidence_ref("validation logic") is False

    def test_empty_string(self):
        assert _is_code_evidence_ref("") is False


# ── Tests: Alternative hypothesis (Rule 2) ───────────────────────────────────

class TestAlternativeHypothesis:

    def test_multiple_hypotheses_with_rejection(self):
        """Content with multiple hypotheses and rejection reasoning scores 1.0."""
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=[], claims=[], evidence_refs=[],
            from_steps=[], root_cause="",
            content=(
                "Hypothesis 1: The issue is in to_python() handling.\n"
                "Hypothesis 2: The issue could be in validate().\n"
                "Alternative explanation: it might be a serializer bug.\n"
                "However, hypothesis 2 doesn't explain the traceback, "
                "so it was ruled out because the error originates in to_python()."
            ),
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

    def test_vague_alternatives_score_half(self):
        """Content mentioning alternatives vaguely (1-2 markers) scores 0.5."""
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=[], claims=[], evidence_refs=[],
            from_steps=[], root_cause="",
            content=(
                "The root cause is the validation method. "
                "Another possible cause could be the serializer, "
                "but the validation seems more likely."
            ),
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

    def test_root_cause_plus_code_and_test_refs(self):
        """root_cause with code ref + test in evidence_refs = 1.0."""
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=[], claims=[],
            evidence_refs=["tests/test_models.py::test_clean"],
            from_steps=[], content="",
            root_cause="Bug in django/db/models.py:45 causes ValueError.",
        )
        assert _check_causal_chain(pr) == 1.0

    def test_root_cause_with_code_only(self):
        """root_cause with code ref but no test ref = 0.5."""
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=[], claims=[],
            evidence_refs=["django/db/models.py:45"],  # code ref, not test
            from_steps=[], content="",
            root_cause="Bug in django/db/models.py:45 causes ValueError.",
        )
        assert _check_causal_chain(pr) == 0.5

    def test_root_cause_without_code_ref(self):
        """root_cause present but without code references = 0.3."""
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=[], claims=[],
            evidence_refs=[],
            from_steps=[], content="",
            root_cause="The validation logic is incorrect and causes failures.",
        )
        assert _check_causal_chain(pr) == 0.3

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
        """Score of exactly 0.5 should pass (threshold is < 0.5 to fail)."""
        pr = PhaseRecord(
            phase="ANALYZE", subtype="analysis.root_cause",
            principals=[], claims=[],
            evidence_refs=["django/db/models.py:45"],
            from_steps=[], content="",
            root_cause="validation logic is wrong",  # no code ref
            causal_chain="test -> clean() -> to_python() -> ValueError long enough",
        )
        verdict = evaluate_analysis(pr)
        # code_grounding = 0.5 (evidence only), should pass
        assert verdict.scores["code_grounding"] == 0.5
        assert "code_grounding" not in verdict.failed_rules

    def test_all_scores_returned(self):
        """All 3 scores are present in the verdict."""
        pr = _make_empty_pr()
        verdict = evaluate_analysis(pr)
        assert "code_grounding" in verdict.scores
        assert "alternative_hypothesis" in verdict.scores
        assert "causal_chain" in verdict.scores
