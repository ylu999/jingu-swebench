"""
design_gate.py — Design quality enforcement for ANALYZE->EXECUTE transition.

Evaluates whether the agent's design meets minimum quality thresholds
before allowing advance to EXECUTE. Targets constraint_encoding_failure.

Contract source of truth: cognition_contracts/design_solution_shape.py
Gate rules and field specs are defined there; this file implements scoring.

Events are system-generated facts, never LLM self-descriptions.
Every field must be derived from system state, not from LLM output.
"""

import re
from dataclasses import dataclass, field
from phase_record import PhaseRecord
from gate_rejection import GateRejection

# ── Contract-derived references ──────────────────────────────────────────────
# Gate rules and repair hints originate from the canonical contract.
# design_gate scoring functions implement the checks; the contract defines
# which fields they target and what repair hint to emit on failure.
try:
    from cognition_contracts.design_solution_shape import (
        GATE_RULES as _CONTRACT_GATE_RULES,
        GATE_RULE_MAP as _CONTRACT_RULE_MAP,
        GATE_THRESHOLD as _CONTRACT_THRESHOLD,
    )
except ImportError:
    # Fallback: contract module not yet on PYTHONPATH — degrade gracefully.
    _CONTRACT_GATE_RULES = []
    _CONTRACT_RULE_MAP = {}
    _CONTRACT_THRESHOLD = 0.5


@dataclass
class DesignVerdict:
    """Result of design gate evaluation."""
    passed: bool
    failed_rules: list
    reasons: list
    scores: dict
    rejection: GateRejection | None = None


# ── Rule 1: Invariant Preservation ──────────────────────────────────────────

# Signals that the design references a specific invariant to preserve
_INVARIANT_PRESERVATION_SIGNALS = {
    "invariant_stated": re.compile(
        r'(?:must|should|need\s+to)\s+(?:preserve|maintain|keep|ensure|enforce)\s+'
        r'(?:the\s+)?(?:invariant|constraint|rule|property|guarantee)',
        re.I,
    ),
    "delimiter_referenced": re.compile(
        r'(?:delimiter|separator|boundary|special\s+char)',
        re.I,
    ),
    "forbidden_chars": re.compile(
        r'(?:must\s+not|cannot|should\s+not|forbidden|disallow|reject|exclude)\s+'
        r'(?:contain|allow|accept|include|permit)',
        re.I,
    ),
}


def _check_invariant_preservation(pr: PhaseRecord, analysis_records: list[PhaseRecord] | None = None) -> float:
    """
    Check that design references the invariant identified in analysis.

    Score:
      0.0 = no invariant reference in design
      0.5 = mentions invariant vaguely (1 signal)
      1.0 = explicit invariant preservation statement (2+ signals)
    """
    text = (pr.content or "") + " " + (pr.root_cause or "")
    if not text.strip():
        return 0.0

    matched = sum(1 for p in _INVARIANT_PRESERVATION_SIGNALS.values() if p.search(text))

    if matched >= 2:
        return 1.0
    elif matched >= 1:
        return 0.5
    else:
        return 0.0


# ── Rule 2: Design Comparison ──────────────────────────────────────────────

_COMPARISON_SIGNALS = {
    "alternative_approach": re.compile(
        r'(?:alternative|another\s+approach|option\s+\d|approach\s+\d|'
        r'(?:first|second)\s+(?:approach|option|strategy))',
        re.I,
    ),
    "comparison_language": re.compile(
        r'(?:(?:instead\s+of|rather\s+than|compared\s+to|versus|vs\.?)\s+'
        r'|(?:pros?\s+and\s+cons?|tradeoff|trade-off))',
        re.I,
    ),
    "design_choice": re.compile(
        r'(?:allowlist|whitelist|blocklist|blacklist|exclusion|inclusion|'
        r'deny\s*list|permit\s*list|positive\s+match|negative\s+match)',
        re.I,
    ),
}


def _check_design_comparison(pr: PhaseRecord) -> float:
    """
    Check that design compares at least 2 approaches.

    Score:
      0.0 = single approach, no alternatives
      0.5 = mentions alternatives vaguely (1 signal)
      1.0 = explicit comparison of 2+ approaches (2+ signals)
    """
    text = (pr.content or "")
    if not text.strip():
        return 0.0

    matched = sum(1 for p in _COMPARISON_SIGNALS.values() if p.search(text))

    if matched >= 2:
        return 1.0
    elif matched >= 1:
        return 0.5
    else:
        return 0.0


# ── Rule 3: Constraint Encoding ────────────────────────────────────────────

_ALLOWLIST_INDICATORS = re.compile(
    r'(?:allowlist|whitelist|permitted\s+characters?|'
    r'valid\s+characters?|accepted\s+characters?|'
    r'\[a-zA-Z|\\w\+|[a-z]\+)',
    re.I,
)

_COMPLETENESS_SIGNALS = {
    "justification": re.compile(
        r'(?:because|since|this\s+covers?|this\s+includes?|'
        r'complete\s+(?:set|list|coverage)|all\s+(?:valid|allowed|permitted))',
        re.I,
    ),
    "test_verification": re.compile(
        r'(?:test\s+case|verified?\s+(?:against|with|by)|'
        r'pass(?:es|ing)?\s+(?:all|both|every)|'
        r'fail(?:s|ing)?\s+(?:for|on|with))',
        re.I,
    ),
}


def _check_constraint_encoding(pr: PhaseRecord) -> float:
    """
    Check that if design uses allowlist approach, completeness is justified.

    If no allowlist detected: returns 1.0 (rule not applicable).
    If allowlist detected without justification: returns 0.0.
    If allowlist detected with justification: returns 1.0.

    Score:
      1.0 = no allowlist (rule N/A) OR allowlist with completeness justification
      0.5 = allowlist with partial justification (1 signal)
      0.0 = allowlist without any completeness justification
    """
    text = (pr.content or "")
    if not text.strip():
        return 1.0  # no content = rule not applicable

    # Check if design uses allowlist approach
    if not _ALLOWLIST_INDICATORS.search(text):
        return 1.0  # no allowlist detected, rule not applicable

    # Allowlist detected — check for completeness justification
    matched = sum(1 for p in _COMPLETENESS_SIGNALS.values() if p.search(text))

    if matched >= 2:
        return 1.0
    elif matched >= 1:
        return 0.5
    else:
        return 0.0


# ── Main evaluation function (soft quality signal — no rejection) ─────────


def evaluate_design(
    pr: PhaseRecord,
    analysis_records: list[PhaseRecord] | None = None,
) -> DesignVerdict:
    """
    Evaluate design quality as soft telemetry signal.

    Scores are computed for observability but never cause rejection.
    Gate mode is soft_quality_signal — all rules emit scores only.

    Args:
        pr: PhaseRecord containing the design/plan.
        analysis_records: Previous analysis PhaseRecords (for invariant cross-check).
    """
    scores: dict = {}

    # Rule 1: Invariant preservation (score only)
    scores["invariant_preservation"] = _check_invariant_preservation(pr, analysis_records)

    # Rule 2: Design comparison (score only)
    scores["design_comparison"] = _check_design_comparison(pr)

    # Rule 3: Constraint encoding (score only)
    scores["constraint_encoding"] = _check_constraint_encoding(pr)

    # Mark as soft quality signal — no hard rejection
    scores["gate_mode"] = "soft_quality_signal"

    return DesignVerdict(
        passed=True,
        failed_rules=[],
        reasons=[],
        scores=scores,
        rejection=None,
    )
