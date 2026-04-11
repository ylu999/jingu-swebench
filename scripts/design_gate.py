"""
design_gate.py — Design quality enforcement for ANALYZE→EXECUTE transition.

Evaluates whether the agent's design meets minimum quality thresholds
before allowing advance to EXECUTE. Targets constraint_encoding_failure.

Three rules:
1. Invariant preservation: design states what invariant must hold
2. Design comparison: at least 2 approaches compared (allowlist vs exclusion etc.)
3. Constraint encoding: if allowlist detected, must justify completeness

Events are system-generated facts, never LLM self-descriptions.
Every field must be derived from system state, not from LLM output.
"""

import re
from dataclasses import dataclass, field
from phase_record import PhaseRecord
from gate_rejection import (
    GateRejection, ContractView, FieldSpec, FieldFailure,
    build_gate_rejection, SDG_ENABLED,
)


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


# ── DESIGN contract (SDG) ──────────────────────────────────────────────────

_DESIGN_CONTRACT = ContractView(
    required_fields=["content"],
    field_specs={
        "content": FieldSpec(
            description="Design plan with invariant preservation, approach comparison, and constraint encoding justification",
            required=True,
            min_length=20,
            semantic_check="design_quality",
        ),
        "invariant_preservation": FieldSpec(
            description="Statement of what invariant the design preserves",
            required=False,
            semantic_check="invariant_stated",
        ),
        "design_comparison": FieldSpec(
            description="Comparison of at least 2 design approaches",
            required=False,
            semantic_check="multiple_approaches",
        ),
        "constraint_encoding": FieldSpec(
            description="If allowlist: justification of completeness",
            required=False,
            semantic_check="completeness_justified",
        ),
    },
)

_RULE_TO_FIELD: dict[str, tuple[str, str]] = {
    "invariant_preservation": (
        "invariant_preservation",
        "State what invariant your design preserves: what delimiter/boundary must NOT be allowed?",
    ),
    "design_comparison": (
        "design_comparison",
        "Compare at least 2 approaches (e.g. allowlist vs exclusion) with tradeoffs",
    ),
    "constraint_encoding": (
        "constraint_encoding",
        "Your design uses an allowlist. Justify completeness: does it cover all valid inputs?",
    ),
}


# ── Main evaluation function ──────────────────────────────────────────────

_THRESHOLD = 0.5


def evaluate_design(
    pr: PhaseRecord,
    analysis_records: list[PhaseRecord] | None = None,
) -> DesignVerdict:
    """
    Evaluate design quality before EXECUTE phase advance.

    Fires once at ANALYZE→EXECUTE transition (not every step).

    Args:
        pr: PhaseRecord containing the design/plan.
        analysis_records: Previous analysis PhaseRecords (for invariant cross-check).
    """
    failed = []
    reasons = []
    scores = {}

    # Rule 1: Invariant preservation
    score1 = _check_invariant_preservation(pr, analysis_records)
    scores["invariant_preservation"] = score1
    if score1 < _THRESHOLD:
        failed.append("invariant_preservation")
        reasons.append(
            "Design does not state what invariant must be preserved. "
            "What delimiter or boundary character must NOT appear? "
            "Why must it be excluded?"
        )

    # Rule 2: Design comparison
    score2 = _check_design_comparison(pr)
    scores["design_comparison"] = score2
    if score2 < _THRESHOLD:
        failed.append("design_comparison")
        reasons.append(
            "Design proposes a single approach without comparing alternatives. "
            "Compare at least 2 approaches (e.g. allowlist vs exclusion-based) "
            "with tradeoffs before choosing."
        )

    # Rule 3: Constraint encoding
    score3 = _check_constraint_encoding(pr)
    scores["constraint_encoding"] = score3
    if score3 < _THRESHOLD:
        failed.append("constraint_encoding")
        reasons.append(
            "Design uses an allowlist approach without justifying completeness. "
            "Can you prove your allowlist covers all valid inputs? "
            "Have you verified against both passing and failing test cases?"
        )

    # Build SDG rejection on failure
    rejection = None
    if failed and SDG_ENABLED:
        field_failures = []
        for rule_name in failed:
            field_name, hint = _RULE_TO_FIELD.get(
                rule_name, (rule_name, f"Fix {rule_name}")
            )
            score = scores.get(rule_name, 0.0)
            reason = "missing" if score == 0.0 else "semantic_fail"

            field_spec = _DESIGN_CONTRACT.field_specs.get(field_name)
            expected = field_spec.description if field_spec else f"{field_name} required"

            field_failures.append(FieldFailure(
                field=field_name,
                reason=reason,
                hint=hint,
                expected=expected,
                actual=None,
            ))

        rejection = build_gate_rejection(
            gate_name="design_gate",
            contract=_DESIGN_CONTRACT,
            extracted={},
            failures=field_failures,
        )

    return DesignVerdict(
        passed=len(failed) == 0,
        failed_rules=failed,
        reasons=reasons,
        scores=scores,
        rejection=rejection,
    )
