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

_KNOWN_CODE_EXTENSIONS = ('.py', '.ts', '.js', '.go', '.rs', '.java', '.c', '.cpp', '.h', '.rb')


def _has_file_extension(ref: str) -> bool:
    """Check if ref ends with a known code file extension (or has ext: pattern)."""
    return any(ref.rstrip().endswith(ext) or ext + ':' in ref for ext in _KNOWN_CODE_EXTENSIONS)


def _has_line_number(ref: str) -> bool:
    """Check if ref contains a file:line pattern (e.g. models.py:45)."""
    parts = ref.split(':')
    return len(parts) >= 2 and parts[-1].strip().isdigit()


def _is_structured_code_ref(ref: str) -> bool:
    """Check if a reference string points to a specific code location."""
    if not ref:
        return False
    return '/' in ref or _has_file_extension(ref) or _has_line_number(ref)


# ── Rule 1: Code Grounding ───────────────────────────────────────────────────

def _check_code_grounding(pr: PhaseRecord) -> float:
    """Check that analysis references specific code locations.

    Structural-only: reads pr.evidence_refs and pr.root_cause.
    NO content fallback. NO regex on root_cause text.

    Score:
      0.0 = no code references in evidence_refs and no root_cause
      0.5 = code refs in evidence_refs OR root_cause (not both)
      1.0 = code refs in evidence_refs AND root_cause present
    """
    has_code_in_evidence = any(
        _is_structured_code_ref(ref) for ref in (getattr(pr, 'evidence_refs', None) or [])
    )
    has_root_cause = bool(getattr(pr, 'root_cause', None) and len(pr.root_cause.strip()) > 10)

    if has_code_in_evidence and has_root_cause:
        return 1.0
    elif has_code_in_evidence or has_root_cause:
        return 0.5
    return 0.0


# ── Rule 2: Alternative Hypothesis ───────────────────────────────────────────

def _check_alternative_hypothesis(pr: PhaseRecord) -> float:
    """Read pr.alternative_hypotheses (structured array from bundle schema)."""
    hypotheses = getattr(pr, 'alternative_hypotheses', None) or []
    if not hypotheses:
        return 0.0
    substantive = [
        h for h in hypotheses
        if isinstance(h, dict)
        and len((h.get('hypothesis') or '').strip()) > 5
        and len((h.get('ruled_out_reason') or '').strip()) > 5
    ]
    if len(substantive) >= 2:
        return 1.0
    elif len(substantive) >= 1:
        return 0.5
    return 0.0


# ── Rule 3: Causal Chain ─────────────────────────────────────────────────────

def _check_causal_chain(pr: PhaseRecord) -> float:
    """Check that a causal chain connecting evidence to root cause is present.

    Structural-only: reads pr.causal_chain field length.
    NO secondary reconstruction from root_cause + evidence_refs regex.

    Score:
      0.0 = causal_chain missing or <= 5 chars
      0.3 = present but too short (5 < len <= 20)
      1.0 = substantive causal chain (> 20 chars)
    """
    causal_chain = getattr(pr, 'causal_chain', None) or ''
    if isinstance(causal_chain, str):
        chain_text = causal_chain.strip()
    else:
        chain_text = str(causal_chain).strip()

    if len(chain_text) > 20:
        return 1.0
    if len(chain_text) > 5:
        return 0.3  # present but too short
    return 0.0


# ── Rule 4: Invariant Capture (v2 — generalized with applicability) ────────

# Domain-specific signals: delimiter/boundary/parsing bugs.
# When these are present, invariant capture should be strict.
_PARSING_DOMAIN_SIGNALS = re.compile(
    r'\b(parser|lexer|regex|delimiter|separator|tokeniz|escap|quot|boundary\s+char'
    r'|validator\s+pattern|pattern\s+match(?:ing|er|es)?|re\.compile|regexp)\b',
    re.IGNORECASE,
)


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
