"""
analysis_gate.py — Phase boundary enforcement for ANALYZE phase.

Evaluates whether the agent's analysis meets minimum quality thresholds
before allowing advance to EXECUTE. Targets wrong_direction failures.

Three rules:
1. Code grounding: root_cause must reference specific code (file/function/line)
2. Alternative hypothesis: at least 2 hypotheses, non-chosen must be addressed
3. Causal chain: must connect test failure -> condition -> code -> why it fails

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
class AnalysisVerdict:
    """Result of analysis gate evaluation."""
    passed: bool
    failed_rules: list  # e.g. ["code_grounding", "causal_chain"]
    reasons: list       # human-readable rejection reasons
    scores: dict        # per-rule scores for telemetry
    extracted: dict = field(default_factory=dict)  # p214: field status for repair feedback
    rejection: GateRejection | None = None  # p217: structured SDG rejection (populated on failure)


# ── Code reference detection (structural) ────────────────────────────────────

# Pattern: file paths with extensions (e.g. django/db/models.py, src/utils.ts)
_CODE_REF_PATH = re.compile(r'[\w/\\]+\.\w{1,4}')
# Pattern: file:line references (e.g. models.py:45, utils.ts:120)
_CODE_REF_LINE = re.compile(r'[\w./\\]+:\d+')
# Pattern: function/method references (e.g. def foo, class Bar, self.method())
_CODE_REF_FUNC = re.compile(r'(?:def |class |self\.\w+|__\w+__|\.(?:get|set|save|delete|create|update|filter|exclude)\()')


def _has_code_reference(text: str) -> bool:
    """Check if text contains at least one code-level reference (structural)."""
    if not text:
        return False
    return bool(_CODE_REF_LINE.search(text) or _CODE_REF_PATH.search(text) or _CODE_REF_FUNC.search(text))


def _count_code_references(text: str) -> int:
    """Count distinct code references in text."""
    if not text:
        return 0
    refs = set()
    refs.update(_CODE_REF_LINE.findall(text))
    refs.update(_CODE_REF_PATH.findall(text))
    refs.update(_CODE_REF_FUNC.findall(text))
    return len(refs)


def _is_code_evidence_ref(ref: str) -> bool:
    """Check if an evidence_ref looks like a code reference (contains path-like pattern)."""
    if not ref:
        return False
    # Code refs contain / or .py or .ts or .js or :line_number
    return bool(
        '/' in ref
        or _CODE_REF_LINE.search(ref)
        or re.search(r'\.\w{1,4}$', ref)
    )


# ── Rule 1: Code Grounding ───────────────────────────────────────────────────

def _check_code_grounding(pr: PhaseRecord) -> float:
    """
    Check that analysis references specific code locations.

    Primary signal: pr.evidence_refs (structured field from declaration extractor)
    Secondary signal: pr.root_cause (structured field for ANALYZE phase)

    Score:
      0.0 = no code references anywhere
      0.5 = code refs in evidence_refs but not in root_cause, or vice versa
      1.0 = code refs in both evidence_refs and root_cause
    """
    has_code_in_evidence = any(_is_code_evidence_ref(ref) for ref in (pr.evidence_refs or []))
    has_code_in_root_cause = _has_code_reference(pr.root_cause)

    if has_code_in_evidence and has_code_in_root_cause:
        return 1.0
    elif has_code_in_evidence or has_code_in_root_cause:
        return 0.5
    else:
        # Fallback: check content field (less reliable, but captures cases
        # where structured extraction missed the reference)
        # This is a TEMPORARY fallback — documented per structure-over-surface rules
        code_ref_count = _count_code_references(pr.content)
        if code_ref_count >= 2:
            return 0.5
        return 0.0


# ── Rule 2: Alternative Hypothesis ───────────────────────────────────────────

# Structural markers indicating hypothesis enumeration.
# These are NOT surface keyword checks — they indicate the agent structured
# its reasoning into multiple distinct hypotheses.
_HYPOTHESIS_MARKERS = [
    # Explicit hypothesis labeling
    r'hypothesis\s*[:#\d]',
    r'possibility\s*[:#\d]',
    # Numbered/lettered alternatives
    r'(?:^|\n)\s*(?:\d+[\.\):]|[a-c][\.\):])\s+',
    # Explicit alternative framing
    r'alternative(?:\s+\w+){0,3}\s*:',
    r'another\s+(?:possible|potential|likely)\s+(?:cause|reason|explanation)',
    # Rejection reasoning (evidence of multi-hypothesis analysis)
    r'(?:ruled?\s+out|eliminated?|less\s+likely|unlikely)\s+because',
    r'(?:however|but)\s+(?:this|that)\s+(?:doesn\'t|does not|cannot|can\'t)\s+explain',
]
_HYPOTHESIS_PATTERNS = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in _HYPOTHESIS_MARKERS]


def _check_alternative_hypothesis(pr: PhaseRecord) -> float:
    """
    Check that analysis considers multiple hypotheses.

    Primary signal: structural markers in pr.content indicating multiple
    distinct hypotheses with rejection reasoning for non-chosen ones.

    Score:
      0.0 = single assertion, no alternatives
      0.5 = mentions alternatives vaguely (1-2 markers)
      1.0 = 2+ distinct hypotheses with rejection reasoning (3+ markers)
    """
    text = pr.content or ""
    if not text:
        return 0.0

    # Also check root_cause — sometimes hypothesis comparison is there
    full_text = text
    if pr.root_cause:
        full_text = text + " " + pr.root_cause

    matched_patterns = 0
    for pattern in _HYPOTHESIS_PATTERNS:
        if pattern.search(full_text):
            matched_patterns += 1

    if matched_patterns >= 3:
        return 1.0
    elif matched_patterns >= 1:
        return 0.5
    else:
        return 0.0


# ── Rule 3: Causal Chain ─────────────────────────────────────────────────────

def _check_causal_chain(pr: PhaseRecord) -> float:
    """
    Check that analysis contains a causal chain connecting
    test failure -> condition -> code -> why it fails.

    Primary signal: pr.causal_chain (structured field — if non-empty, agent
    produced structured output explicitly labeled as causal chain)

    Secondary signal: pr.root_cause + pr.evidence_refs — if root_cause contains
    causal reasoning AND evidence_refs has test + code references, the chain
    is implicitly present.

    Score:
      0.0 = no causal chain
      0.5 = partial chain (root_cause has reasoning but missing test link or code link)
      1.0 = complete chain (causal_chain field present, or root_cause + evidence covers all links)
    """
    # Primary: structured causal_chain field
    if pr.causal_chain and len(pr.causal_chain.strip()) > 20:
        return 1.0

    # Secondary: reconstruct chain from root_cause + evidence_refs
    has_root_cause = bool(pr.root_cause and len(pr.root_cause.strip()) > 10)
    has_code_ref = _has_code_reference(pr.root_cause) if has_root_cause else False
    has_test_ref = any(
        'test' in ref.lower() or '::' in ref
        for ref in (pr.evidence_refs or [])
    )

    if has_root_cause and has_code_ref and has_test_ref:
        return 1.0
    elif has_root_cause and (has_code_ref or has_test_ref):
        return 0.5
    elif has_root_cause:
        # root_cause exists but lacks specific links
        return 0.3
    else:
        return 0.0


# ── ANALYZE contract (SDG p217) ──────────────────────────────────────────────

_ANALYZE_CONTRACT = ContractView(
    required_fields=["root_cause", "causal_chain", "evidence_refs"],
    field_specs={
        "root_cause": FieldSpec(
            description="Identified root cause with specific code reference (file/function/line)",
            required=True,
            min_length=10,
            semantic_check="grounded_in_code",
        ),
        "causal_chain": FieldSpec(
            description="Causal chain: test failure -> condition -> code -> why it fails",
            required=True,
            min_length=20,
            semantic_check="connects_test_to_code",
        ),
        "evidence_refs": FieldSpec(
            description="Code and test references supporting the analysis",
            required=True,
        ),
        "alternative_hypothesis": FieldSpec(
            description="At least 2 hypotheses with rejection reasoning for non-chosen",
            required=False,
            semantic_check="multiple_distinct_hypotheses",
        ),
    },
)

# Rule name -> (field, hint) mapping for SDG FieldFailure construction
_RULE_TO_FIELD: dict[str, tuple[str, str]] = {
    "code_grounding": (
        "root_cause",
        "Point to exact code location (file:line or function name) causing the issue",
    ),
    "alternative_hypothesis": (
        "alternative_hypothesis",
        "Consider at least 2 hypotheses and explain why non-chosen ones were rejected",
    ),
    "causal_chain": (
        "causal_chain",
        "Explain step-by-step: test failure -> condition -> code -> why it fails",
    ),
}


# ── Main evaluation function ─────────────────────────────────────────────────

_THRESHOLD = 0.5  # Soft gate: reject only clearly inadequate analyses


def evaluate_analysis(pr: PhaseRecord) -> AnalysisVerdict:
    """
    Evaluate analysis phase quality. Returns verdict with pass/fail + reasons.

    Threshold is 0.5 (soft gate). We reject clearly wrong analyses,
    not borderline ones.
    """
    failed = []
    reasons = []
    scores = {}

    # Rule 1: Code grounding
    score1 = _check_code_grounding(pr)
    scores["code_grounding"] = score1
    if score1 < _THRESHOLD:
        failed.append("code_grounding")
        reasons.append(
            "Analysis lacks specific code references (file/function/line). "
            "Point to the exact code location causing the issue."
        )

    # Rule 2: Alternative hypothesis
    score2 = _check_alternative_hypothesis(pr)
    scores["alternative_hypothesis"] = score2
    if score2 < _THRESHOLD:
        failed.append("alternative_hypothesis")
        reasons.append(
            "Analysis contains a single hypothesis without alternatives. "
            "Consider at least 2 hypotheses and explain why non-chosen ones were rejected."
        )

    # Rule 3: Causal chain
    score3 = _check_causal_chain(pr)
    scores["causal_chain"] = score3
    if score3 < _THRESHOLD:
        failed.append("causal_chain")
        reasons.append(
            "Analysis lacks a causal chain (test failure -> condition -> code -> why). "
            "Explain step-by-step how the test failure connects to the root cause."
        )

    extracted = {
        "root_cause": pr.root_cause[:100] if pr.root_cause else "",
        "causal_chain": pr.causal_chain[:100] if pr.causal_chain else "",
    }

    # p217: Build structured GateRejection on failure (when SDG enabled)
    rejection = None
    if failed and SDG_ENABLED:
        field_failures = []
        for rule_name in failed:
            field_name, hint = _RULE_TO_FIELD.get(
                rule_name, (rule_name, f"Fix {rule_name}")
            )
            score = scores.get(rule_name, 0.0)
            # Determine reason based on score
            if score == 0.0:
                reason = "missing"
            elif score < _THRESHOLD:
                reason = "too_short" if field_name in ("causal_chain", "root_cause") else "semantic_fail"
            else:
                reason = "semantic_fail"

            field_spec = _ANALYZE_CONTRACT.field_specs.get(field_name)
            expected = field_spec.description if field_spec else f"{field_name} required"
            actual_val = extracted.get(field_name)

            field_failures.append(FieldFailure(
                field=field_name,
                reason=reason,
                hint=hint,
                expected=expected,
                actual=actual_val if actual_val else None,
            ))

        rejection = build_gate_rejection(
            gate_name="analysis_gate",
            contract=_ANALYZE_CONTRACT,
            extracted=extracted,
            failures=field_failures,
        )

    return AnalysisVerdict(
        passed=len(failed) == 0,
        failed_rules=failed,
        reasons=reasons,
        scores=scores,
        extracted=extracted,
        rejection=rejection,
    )
