"""
test_trace_replay.py — L1 trace replay tests.

Verifies that the control-plane chain works deterministically without LLM:
  extraction → gate → verdict → routing → retry signal

Each test uses synthetic data (no S3, no Docker, no LLM calls).
These tests enforce that replay capability exists and works correctly.

Build-time enforcement: if a control field cannot be replayed through
the full chain, the build fails.
"""

import json
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mini-swe-agent"))


# ── Synthetic ANALYZE structured output ──────────────────────────────────────

GOOD_ANALYZE_OUTPUT = {
    "phase": "ANALYZE",
    "subtype": "analysis.root_cause",
    "root_cause": (
        "In django/utils/dateparse.py:32, the standard_duration_re regex uses "
        "r'(?:(?P<hours>-?\\d+):)(?=\\d+:\\d+)' which requires positive digits "
        "after the colon. When hours/minutes are negative like '-1:30:00', the "
        "lookahead (?=\\d+:\\d+) fails because '-' is not matched by \\d."
    ),
    "causal_chain": (
        "Test calls parse_duration('-1:30:00') -> standard_duration_re.match() "
        "-> lookahead (?=\\d+:\\d+) fails on '-1' -> hours group not captured "
        "-> function returns None instead of timedelta(-1, 66600)"
    ),
    "evidence_refs": [
        "django/utils/dateparse.py:32",
        "tests/test_dateparse.py:45",
    ],
    "alternative_hypotheses": [
        {
            "hypothesis": "The negative sign in seconds causes the failure",
            "ruled_out_reason": "Seconds regex already has -? prefix, verified by testing '30:-10' which parses correctly",
        },
        {
            "hypothesis": "The microseconds group fails on negative input",
            "ruled_out_reason": "Microseconds are optional and unsigned, not involved in this failure path",
        },
    ],
    "invariant_capture": {
        "identified_invariants": [
            "parse_duration must handle ISO 8601 negative components",
            "lookahead must allow optional negative sign before digits",
        ],
        "risk_if_violated": "All negative duration strings silently return None instead of raising or parsing correctly",
    },
    "repair_strategy_type": "REGEX_FIX",
    "root_cause_location_files": ["django/utils/dateparse.py"],
    "mechanism_path": ["parse_duration()", "standard_duration_re.match()", "lookahead fails on negative"],
    "rejected_nearby_files": [
        {"file": "django/utils/duration.py", "reason": "output formatter only, not involved in parsing"},
    ],
    "principals": ["causal_grounding", "evidence_linkage"],
}

BAD_ANALYZE_OUTPUT_NO_STRATEGY = {
    **GOOD_ANALYZE_OUTPUT,
    "repair_strategy_type": "",  # missing
}

BAD_ANALYZE_OUTPUT_INVALID_STRATEGY = {
    **GOOD_ANALYZE_OUTPUT,
    "repair_strategy_type": "MAGIC_FIX",  # invalid enum
}


# ── Stage 1: Extraction replay ──────────────────────────────────────────────

class TestExtractionReplay:
    """Extraction of PhaseRecord from structured output must be deterministic."""

    def test_extract_good_analyze(self):
        from declaration_extractor import build_phase_record_from_structured
        pr = build_phase_record_from_structured(GOOD_ANALYZE_OUTPUT, phase="ANALYZE")
        assert pr.root_cause and len(pr.root_cause) > 10
        assert pr.causal_chain and len(pr.causal_chain) > 10
        assert pr.repair_strategy_type == "REGEX_FIX"
        assert len(pr.evidence_refs) >= 2
        assert len(pr.alternative_hypotheses) >= 2

    def test_extract_missing_strategy_uses_classifier(self):
        from declaration_extractor import build_phase_record_from_structured
        pr = build_phase_record_from_structured(BAD_ANALYZE_OUTPUT_NO_STRATEGY, phase="ANALYZE")
        # Classifier should detect REGEX_FIX from root_cause mentioning "regex"
        assert pr.repair_strategy_type == "REGEX_FIX", (
            f"Classifier failed to detect REGEX_FIX, got: {pr.repair_strategy_type!r}"
        )

    def test_extract_invalid_strategy_preserved(self):
        from declaration_extractor import build_phase_record_from_structured
        pr = build_phase_record_from_structured(BAD_ANALYZE_OUTPUT_INVALID_STRATEGY, phase="ANALYZE")
        # Invalid enum should still be extracted (gate rejects, not extractor)
        assert pr.repair_strategy_type == "MAGIC_FIX"


# ── Stage 2: Gate replay ────────────────────────────────────────────────────

class TestGateReplay:
    """Gate verdict from PhaseRecord must be deterministic."""

    def test_good_analyze_passes_gate(self):
        from declaration_extractor import build_phase_record_from_structured
        from analysis_gate import evaluate_analysis
        pr = build_phase_record_from_structured(GOOD_ANALYZE_OUTPUT, phase="ANALYZE")
        verdict = evaluate_analysis(pr)
        assert verdict.passed, (
            f"Good ANALYZE should pass gate, but failed: {verdict.failed_rules} — {verdict.reasons}"
        )
        assert verdict.scores["repair_strategy_type"] == 1.0

    def test_missing_strategy_fails_gate(self):
        from declaration_extractor import build_phase_record_from_structured
        from analysis_gate import evaluate_analysis
        # Force empty strategy (bypass classifier)
        from phase_record import PhaseRecord
        pr = build_phase_record_from_structured(GOOD_ANALYZE_OUTPUT, phase="ANALYZE")
        pr.repair_strategy_type = ""  # force empty
        verdict = evaluate_analysis(pr)
        assert not verdict.passed
        assert "repair_strategy_type" in verdict.failed_rules

    def test_invalid_strategy_fails_gate(self):
        from declaration_extractor import build_phase_record_from_structured
        from analysis_gate import evaluate_analysis
        pr = build_phase_record_from_structured(BAD_ANALYZE_OUTPUT_INVALID_STRATEGY, phase="ANALYZE")
        verdict = evaluate_analysis(pr)
        assert not verdict.passed
        assert "repair_strategy_type" in verdict.failed_rules

    def test_gate_scores_all_rules(self):
        """Gate must emit scores for all rules (telemetry completeness)."""
        from declaration_extractor import build_phase_record_from_structured
        from analysis_gate import evaluate_analysis
        pr = build_phase_record_from_structured(GOOD_ANALYZE_OUTPUT, phase="ANALYZE")
        verdict = evaluate_analysis(pr)
        required_scores = ["code_grounding", "alternative_hypothesis", "causal_chain",
                           "invariant_capture", "repair_strategy_type"]
        for rule in required_scores:
            assert rule in verdict.scores, f"Missing score for rule: {rule}"


# ── Stage 3: Full chain replay (extraction → gate → verdict) ────────────────

class TestFullChainReplay:
    """End-to-end chain: structured output → PhaseRecord → gate → verdict."""

    def test_chain_good_output_passes(self):
        from declaration_extractor import build_phase_record_from_structured
        from analysis_gate import evaluate_analysis
        pr = build_phase_record_from_structured(GOOD_ANALYZE_OUTPUT, phase="ANALYZE")
        verdict = evaluate_analysis(pr)
        assert verdict.passed
        assert verdict.extracted["repair_strategy_type"] == "REGEX_FIX"
        assert verdict.extracted["root_cause"][:20] == GOOD_ANALYZE_OUTPUT["root_cause"][:20]

    def test_chain_bad_strategy_rejects(self):
        from declaration_extractor import build_phase_record_from_structured
        from analysis_gate import evaluate_analysis
        pr = build_phase_record_from_structured(BAD_ANALYZE_OUTPUT_INVALID_STRATEGY, phase="ANALYZE")
        verdict = evaluate_analysis(pr)
        assert not verdict.passed
        assert "repair_strategy_type" in verdict.failed_rules
        # Rejection must carry repair hint (NBR compliance)
        assert any("REPAIR_STRATEGY_TYPE" in r for r in verdict.reasons)

    def test_chain_deterministic(self):
        """Same input must produce same verdict (determinism invariant)."""
        from declaration_extractor import build_phase_record_from_structured
        from analysis_gate import evaluate_analysis
        v1 = evaluate_analysis(build_phase_record_from_structured(GOOD_ANALYZE_OUTPUT, phase="ANALYZE"))
        v2 = evaluate_analysis(build_phase_record_from_structured(GOOD_ANALYZE_OUTPUT, phase="ANALYZE"))
        assert v1.passed == v2.passed
        assert v1.scores == v2.scores
        assert v1.failed_rules == v2.failed_rules


# ── Stage 4: Control field replay capability enforcement ─────────────────────

class TestReplayCapabilityEnforced:
    """Every control field must be replayable through the full chain.

    This is the enforcement test: if a control field cannot be extracted,
    gated, and consumed via replay (no LLM), the build fails.
    """

    def test_repair_strategy_type_roundtrip(self):
        """repair_strategy_type: declared → extracted → gated → consumed."""
        from declaration_extractor import build_phase_record_from_structured
        from analysis_gate import evaluate_analysis
        from cognition_contracts.analysis_root_cause import REPAIR_STRATEGY_TYPES

        for strategy in REPAIR_STRATEGY_TYPES:
            output = {**GOOD_ANALYZE_OUTPUT, "repair_strategy_type": strategy}
            pr = build_phase_record_from_structured(output, phase="ANALYZE")
            # Extracted
            assert pr.repair_strategy_type == strategy, (
                f"Extraction failed for {strategy}"
            )
            # Gated
            verdict = evaluate_analysis(pr)
            assert verdict.passed, (
                f"Gate failed for valid strategy {strategy}: {verdict.failed_rules}"
            )
            assert verdict.scores["repair_strategy_type"] == 1.0
            # Consumed (in extracted dict for downstream)
            assert verdict.extracted["repair_strategy_type"] == strategy
