"""
analysis_gate.py — Phase boundary enforcement for ANALYZE phase.

Evaluates whether the agent's analysis meets minimum quality thresholds
before allowing advance to EXECUTE. Targets wrong_direction failures.

Four rules:
1. Code grounding: root_cause must reference specific code (file/function/line)
2. Alternative hypothesis: at least 2 hypotheses, non-chosen must be addressed
3. Causal chain: must connect test failure -> condition -> code -> why it fails
4. Invariant capture: analysis must identify the structural invariant being violated

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
from cognition_contracts import analysis_root_cause as _arc


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


# ── Rule 4: Invariant Capture (v2 — generalized with applicability) ────────

# Domain-specific signals: delimiter/boundary/parsing bugs.
# When these are present, invariant capture should be strict.
_PARSING_DOMAIN_SIGNALS = re.compile(
    r'\b(parser|lexer|regex|delimiter|separator|tokeniz|escap|quot|boundary\s+char'
    r'|validator\s+pattern|pattern\s+match(?:ing|er|es)?|re\.compile|regexp)\b',
    re.IGNORECASE,
)

# Generalized invariant signals (apply to ALL bug types):
# Any of these indicates the agent identified what must be preserved.
_INVARIANT_SIGNALS_GENERAL = {
    # Preserved behavior / contract
    "preserved_behavior": re.compile(
        r'(?:must\s+(?:still|continue\s+to|remain|preserve|maintain|keep)'
        r'|(?:preserve|maintain|keep)\s+(?:existing|current|original|backward)'
        r'|unchanged|invariant|contract|guarantee'
        r'|non.?regression|compatibility)',
        re.IGNORECASE,
    ),
    # Forbidden behavior (what must NOT happen)
    "forbidden_behavior": re.compile(
        r'(?:must\s+not|cannot|should\s+not|forbidden|disallow|reject|prevent|block)'
        r'\s+(?:contain|allow|accept|appear|pass|return|produce|create|generate)',
        re.IGNORECASE,
    ),
    # Boundary / constraint (domain-general)
    "boundary_constraint": re.compile(
        r'(?:boundary|constraint|limitation|restriction|precondition|postcondition'
        r'|edge\s+case|corner\s+case|valid\s+range|invalid\s+input'
        r'|type\s+(?:check|error|constraint)|assertion)',
        re.IGNORECASE,
    ),
    # Specific code structural signal (decorator, cache, override, etc.)
    "code_structural": re.compile(
        r'(?:cache[_.]clear|lru_cache|decorator|override|super\(\)|__init__'
        r'|migration|backward|forward|schema|interface|signature|call\s+site'
        r'|caller|import)',
        re.IGNORECASE,
    ),
}

# Parsing-domain-specific signals (strict — original behavior)
_INVARIANT_SIGNALS_PARSING = {
    "delimiter": re.compile(r'delimiter|separator|boundary\s+character', re.I),
    "forbidden_char": re.compile(
        r'(?:must\s+not|cannot|should\s+not|forbidden|disallow)\s+(?:contain|allow|accept|appear)',
        re.I,
    ),
    "structural_role": re.compile(r'(?:structural|parsing|syntactic)\s+(?:role|meaning|significance|boundary)', re.I),
    "specific_char": re.compile(r'[`\'"]\s*[:@/\\#]\s*[`\'"]', re.I),
}


def _is_parsing_domain(pr: PhaseRecord) -> bool:
    """Detect if the bug is in the parsing/validator/regex domain.

    When True, invariant_capture uses strict delimiter-focused signals.
    When False, invariant_capture uses generalized behavioral signals.
    """
    text = (pr.root_cause or "") + " " + (pr.content or "")
    return bool(_PARSING_DOMAIN_SIGNALS.search(text))


def _check_invariant_capture(pr: PhaseRecord) -> float:
    """
    Check that analysis identifies the behavioral constraint being violated.

    v3: Structure-first evaluation.
    Reads pr.invariant_capture (structured field from bundle schema), NOT
    free-text regex on root_cause/content/causal_chain.

    CONTRACT OWNERSHIP RULE: Any hard gate check MUST have a corresponding
    schema field that the agent was explicitly asked to produce. Gate checks
    that depend on regex-matching free text violate this rule.

    Score:
      0.0 = invariant_capture missing or empty
      0.5 = has identified_invariants but no risk_if_violated (partial)
      1.0 = has both identified_invariants and risk_if_violated
    """
    ic = pr.invariant_capture
    if not isinstance(ic, dict) or not ic:
        return 0.0

    invariants = ic.get("identified_invariants", [])
    risk = (ic.get("risk_if_violated") or "").strip()

    if not invariants:
        return 0.0

    # Has invariants listed
    has_substantive_invariants = any(
        isinstance(inv, str) and len(inv.strip()) > 5
        for inv in invariants
    )
    if not has_substantive_invariants:
        return 0.0

    if risk and len(risk) > 5:
        return 1.0  # both fields present and substantive
    return 0.5  # invariants present but risk missing/thin


# ── ANALYZE contract (SDG p217) ──────────────────────────────────────────────

# Derived from cognition_contracts/analysis_root_cause.py (single source of truth).
_ANALYZE_CONTRACT = ContractView(
    required_fields=list(_arc.GATE_REQUIRED_FIELDS),
    field_specs={
        fs.name: FieldSpec(
            description=fs.description,
            required=fs.required,
            min_length=fs.min_length,
            semantic_check=fs.semantic_check,
        )
        for fs in _arc.FIELD_SPECS
    },
)

# Rule name -> (field, hint) mapping for SDG FieldFailure construction.
# Derived from contract GATE_RULES.
_RULE_TO_FIELD: dict[str, tuple[str, str]] = {
    rule.name: (rule.field, rule.repair_hint) for rule in _arc.GATE_RULES
}


# ── Main evaluation function ─────────────────────────────────────────────────

_THRESHOLD = _arc.GATE_THRESHOLD  # From contract (single source of truth)


def evaluate_analysis(pr: PhaseRecord, *, structured_output: bool = False) -> AnalysisVerdict:
    """
    Evaluate analysis phase quality. Returns verdict with pass/fail + reasons.

    Threshold is 0.5 (soft gate). We reject clearly wrong analyses,
    not borderline ones.

    Args:
        pr: PhaseRecord to evaluate.
        structured_output: When True (p221), schema guarantees structural
            correctness (required fields present, types correct, min lengths met).
            Gate skips structural presence checks and only performs semantic checks:
            - code_grounding: still checks whether root_cause references code
            - alternative_hypothesis: downgraded to quality signal (schema enforces presence)
            - causal_chain: still checks for causal chain quality
            When False: all checks (structural + semantic) as before.
    """
    failed = []
    reasons = []
    scores = {}

    # Rule 1: Code grounding (semantic check — kept in both modes)
    # When structured_output=True, root_cause and evidence are guaranteed present
    # by schema, but we still check whether they contain actual code references.
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

    # Rule 3: Causal chain (semantic check — always hard gate)
    score3 = _check_causal_chain(pr)
    scores["causal_chain"] = score3
    if score3 < _THRESHOLD:
        failed.append("causal_chain")
        reasons.append(
            "Analysis lacks a causal chain (test failure -> condition -> code -> why). "
            "Explain step-by-step how the test failure connects to the root cause."
        )

    # Rule 4: Invariant capture (with domain-aware fail-open)
    score4 = _check_invariant_capture(pr)
    scores["invariant_capture"] = score4
    scores["invariant_domain"] = "parsing" if _is_parsing_domain(pr) else "general"

    # ── Soft gate logic for Rules 2 & 4 ─────────────────────────────────────
    # Core gate = code_grounding + causal_chain (always hard).
    # alternative_hypothesis + invariant_capture are soft when:
    #   - Non-parsing domain AND core rules (cg + cc) pass → fail-open
    #   - Parsing domain → all 4 rules are hard
    #   - structured_output mode → alternative_hypothesis always soft (schema enforces presence)
    # Replay evidence (p237): 6/6 unresolved instances had alternative_hypothesis
    # as sole blocker despite good code_grounding + causal_chain.
    is_parsing = _is_parsing_domain(pr)
    core_pass = all(scores.get(r, 0) >= _THRESHOLD for r in ("code_grounding", "causal_chain"))

    # Rule 2 enforcement
    if score2 < _THRESHOLD:
        if structured_output:
            scores["alternative_hypothesis_note"] = (
                "structured_output: schema enforces presence, score is quality signal only"
            )
        elif not is_parsing and core_pass:
            # Fail-open: core rules pass, alternative_hypothesis is quality signal
            scores["alternative_hypothesis_note"] = (
                "fail_open: non-parsing domain, core rules pass — downgraded to warning"
            )
        else:
            failed.append("alternative_hypothesis")
            reasons.append(
                "Analysis contains a single hypothesis without alternatives. "
                "Consider at least 2 hypotheses and explain why non-chosen ones were rejected."
            )

    # Rule 4 enforcement — structure-first (v3)
    # invariant_capture is now a structured field in the schema. The gate reads
    # the structure, not regex on free text.
    # - Parsing domain: hard gate (invariant_capture required)
    # - Non-parsing domain + core_pass: soft warning (invariant_capture optional)
    if score4 < _THRESHOLD:
        if not is_parsing and core_pass:
            scores["invariant_capture_note"] = (
                "soft: non-parsing domain, core rules pass — invariant_capture optional"
            )
        else:
            failed.append("invariant_capture")
            reasons.append(
                "Fill in the invariant_capture field: what behavioral constraints "
                "must the fix preserve? List identified_invariants and risk_if_violated."
            )

    extracted = {
        "root_cause": pr.root_cause[:100] if pr.root_cause else "",
        "causal_chain": pr.causal_chain[:100] if pr.causal_chain else "",
        "invariant_capture": pr.invariant_capture if pr.invariant_capture else {},
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
