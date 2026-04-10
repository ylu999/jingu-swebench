"""
principal_inference.py — Pluggable rule registry for deterministic principal inference.

Version: v0.2 — pluggable rule registry (p195)
Rules are deterministic, inspectable, no LLM.
Invariant: same input = same output.

Architecture:
  - InferenceRule: a single named rule with applies_to filter + infer function
  - InferenceResult: output of one rule evaluation (score, signals, explanation)
  - InferredPrincipalResult: aggregated output of running all rules for a phase_record
  - register_rule() / get_rules(): global registry
  - run_inference(): engine — loops over registry, exception-safe per rule
  - infer_principals(): backward-compatible wrapper returning list[str]
  - diff_principals(): three-way diff, accepts list[str] or InferredPrincipalResult

Principals inferred (5 built-in + 7 stage-2 from p207-P5):
  causal_grounding           — evidence_refs non-empty + causal language in content/claims
  evidence_linkage           — evidence_refs non-empty AND from_steps non-empty
  minimal_change             — execution.code_patch subtype + content line count <= 30
  alternative_hypothesis_check — analysis.root_cause subtype + alternatives language
  invariant_preservation     — judge.verification subtype + preservation language
  ontology_alignment         — phase maps to known subtype + principals declared
  phase_boundary_discipline  — recognized phase + consistent declaration + subtype resolved
  action_grounding           — PLAN references ROOT_CAUSE (execution.code_patch)
  option_comparison          — OPTIONS >= 2 entries (decision.fix_direction)
  constraint_satisfaction    — CONSTRAINTS non-empty (decision.fix_direction)
  result_verification        — TEST_RESULTS non-empty (judge.verification)
  uncertainty_honesty        — UNCERTAINTY non-empty (analysis.root_cause)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class InferenceResult:
    """Output of a single rule evaluation."""
    principal: str
    score: float              # 0.0–1.0
    signals: list[str]        # machine-readable signal tokens e.g. ["has_causal_language", "has_evidence_refs"]
    explanation: str          # human-readable one-liner
    threshold: float          # score must exceed this to count as inferred


@dataclass
class InferenceRule:
    """
    A single principal inference rule.

    applies_to: subtype strings this rule applies to (None = all subtypes)
    infer:      (phase_record) -> (score: float, signals: list[str], explanation: str)
    threshold:  score must exceed this to count as inferred (default 0.7)
    """
    principal: str
    infer: Callable  # (phase_record) -> tuple[float, list[str], str]
    applies_to: list[str] | None = None   # subtype strings e.g. ["analysis.root_cause"]
    threshold: float = 0.7


@dataclass
class InferredPrincipalResult:
    """Aggregated output of running all rules for a phase_record."""
    subtype: str
    present: list[str]              # principals with score >= threshold
    absent: list[str]               # principals with score < threshold
    details: dict[str, InferenceResult]  # per-principal full result


# ── Rule registry ─────────────────────────────────────────────────────────────

_RULE_REGISTRY: list[InferenceRule] = []


def register_rule(rule: InferenceRule) -> None:
    """Register an InferenceRule into the global registry."""
    _RULE_REGISTRY.append(rule)


def get_rules() -> list[InferenceRule]:
    """Return a copy of the current rule registry."""
    return list(_RULE_REGISTRY)


# ── Keyword patterns (deterministic regex, no LLM) ───────────────────────────

_CAUSAL_KEYWORDS = re.compile(
    r"\b(because|due to|causes|leads to|results in|therefore|thus)\b",
    re.IGNORECASE,
)
_ALTERNATIVE_KEYWORDS = re.compile(
    r"\b(alternative|another possibility|could also|or instead|other approach"
    r"|however|but|yet|rather than|instead of|another way|might also|could be)\b",
    re.IGNORECASE,
)
_PRESERVE_KEYWORDS = re.compile(
    r"\b(does not change|preserve|maintain|invariant|unchanged|no side effect)\b",
    re.IGNORECASE,
)

_SMALL_PATCH_MAX_LINES = 30

# Structured field extraction regex (mirrors declaration_extractor._STRUCTURED_FIELD_RE)
_STRUCTURED_FIELD_RE = re.compile(
    r"^([A-Z_]{3,}):\s*\n(.*?)(?=\n[A-Z_]{3,}:|\Z)",
    re.MULTILINE | re.DOTALL,
)

# Forbidden phase transitions: these transitions indicate wrong phase ordering.
# key=from_phase, value=set of phases that CANNOT legally follow it.
_FORBIDDEN_TRANSITIONS: dict[str, set[str]] = {
    "OBSERVE":  {"EXECUTE", "JUDGE"},         # must ANALYZE before EXECUTE
    "ANALYZE":  {"JUDGE"},                     # must EXECUTE before JUDGE
    "JUDGE":    {"OBSERVE"},                   # after JUDGE, go to EXECUTE or ANALYZE (retry), not back to OBSERVE
}

# Option-line pattern: matches "- Option N:", "N.", "N)", numbered list items
_OPTION_LINE_RE = re.compile(
    r"(?:^|\n)\s*(?:-\s*Option\s*\d|(?:\d+)[.)]\s)",
    re.IGNORECASE,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_fields(phase_record):
    """Extract standard fields from a phase_record object."""
    evidence_refs = getattr(phase_record, "evidence_refs", []) or []
    claims = getattr(phase_record, "claims", []) or []
    from_steps = getattr(phase_record, "from_steps", []) or []
    content = getattr(phase_record, "content", "") or ""
    claims_text = " ".join(str(c) for c in claims)
    full_text = content + " " + claims_text
    return evidence_refs, from_steps, content, full_text


def _extract_structured_from_content(phase_record) -> dict[str, str]:
    """Extract structured output sections (ROOT_CAUSE, PLAN, OPTIONS, etc.) from content.

    Uses the same regex as declaration_extractor.extract_structured_fields.
    Returns dict of field_name (lowercased) -> stripped content.
    Also checks named attributes on the phase_record (root_cause, plan, etc.).
    """
    result: dict[str, str] = {}
    content = getattr(phase_record, "content", "") or ""
    if content:
        for m in _STRUCTURED_FIELD_RE.finditer(content):
            key = m.group(1).strip().lower()
            val = m.group(2).strip()
            if val:
                result[key] = val
    # Merge explicit PhaseRecord attributes (higher priority — already parsed)
    for attr in ("root_cause", "causal_chain", "plan"):
        val = getattr(phase_record, attr, "") or ""
        if val:
            result[attr] = val
    return result


# ── Rule implementations ──────────────────────────────────────────────────────

def _infer_causal_grounding(phase_record) -> tuple[float, list[str], str]:
    """causal_grounding: evidence_refs present + causal language in content.

    P1 fix: from_steps linkage removed from scoring — from_steps is populated by
    gate step indices, not by the agent message itself. Requiring it caused score
    to cap at 0.8 max when from_steps=[] (the common case at extraction time).
    Threshold lowered to 0.5 so evidence_refs alone is sufficient.
    """
    evidence_refs, _from_steps, _content, full_text = _extract_fields(phase_record)
    score = 0.0
    signals: list[str] = []

    if evidence_refs:
        score += 0.5
        signals.append("has_evidence_refs")

    if _CAUSAL_KEYWORDS.search(full_text):
        score += 0.3
        signals.append("has_causal_language")

    # Require evidence_refs as minimum for causal_grounding to fire at all
    if not evidence_refs:
        score = 0.0
        signals.append("no_evidence_refs")

    if score >= 0.5:
        explanation = f"Causal reasoning grounded in {len(evidence_refs)} evidence ref(s)"
    else:
        explanation = "Missing evidence refs (causal_grounding requires file references)"

    return score, signals, explanation


def _infer_evidence_linkage(phase_record) -> tuple[float, list[str], str]:
    """evidence_linkage: evidence_refs present OR from_steps present.

    P1 fix: changed from AND to OR, threshold lowered from 0.7 to 0.5.
    from_steps is populated by gate step indices at runtime, not by agent message.
    At record extraction time from_steps=[] is the norm — requiring both AND
    caused evidence_linkage to almost never fire (score cap 0.5 < threshold 0.7).
    Evidence_refs alone (file:line references in agent output) is sufficient signal.
    """
    evidence_refs, from_steps, _content, _full_text = _extract_fields(phase_record)
    score = 0.0
    signals: list[str] = []

    if evidence_refs:
        score += 0.7
        signals.append("has_evidence_refs")

    if from_steps:
        score += 0.3
        score = min(score, 1.0)
        signals.append("has_step_linkage")

    if not evidence_refs and not from_steps:
        signals.append("no_evidence_chain")

    if score >= 0.5:
        explanation = f"Evidence chain present ({len(evidence_refs)} ref(s), {len(from_steps)} step(s))"
    else:
        explanation = "No evidence references or step linkage"

    return score, signals, explanation


def _infer_minimal_change(phase_record) -> tuple[float, list[str], str]:
    """minimal_change: content line count <= 30."""
    _evidence_refs, _from_steps, content, _full_text = _extract_fields(phase_record)
    score = 0.0
    signals: list[str] = []

    patch_lines = content.count("\n")

    if patch_lines <= _SMALL_PATCH_MAX_LINES:
        score += 0.7
        signals.append(f"small_patch_{patch_lines}_lines")
        # bonus for very small patches
        if patch_lines <= 10:
            score += 0.3
            score = min(score, 1.0)
    else:
        signals.append(f"large_patch_{patch_lines}_lines")

    if score >= 0.7:
        explanation = f"Patch within minimal change threshold ({patch_lines} lines)"
    else:
        explanation = f"Patch too large ({patch_lines} lines > {_SMALL_PATCH_MAX_LINES} threshold)"

    return score, signals, explanation


def _infer_alternative_hypothesis_check(phase_record) -> tuple[float, list[str], str]:
    """alternative_hypothesis_check: alternative language present."""
    _evidence_refs, _from_steps, _content, full_text = _extract_fields(phase_record)
    score = 0.0
    signals: list[str] = []

    if _ALTERNATIVE_KEYWORDS.search(full_text):
        # alternative language alone is sufficient signal (task spec: signals: ["has_alternative_language"])
        score += 0.7
        signals.append("has_alternative_language")
    else:
        signals.append("no_alternative_language")

    # Bonus: multiple distinct claims (rough proxy for multiple hypotheses)
    claims = getattr(phase_record, "claims", []) or []
    if len(claims) >= 2:
        score += 0.3
        score = min(score, 1.0)

    if score >= 0.7:
        explanation = "Alternative hypothesis present"
    else:
        explanation = "No competing hypothesis detected"

    return score, signals, explanation


def _infer_invariant_preservation(phase_record) -> tuple[float, list[str], str]:
    """invariant_preservation: preservation language present."""
    evidence_refs, _from_steps, _content, full_text = _extract_fields(phase_record)
    score = 0.0
    signals: list[str] = []

    if _PRESERVE_KEYWORDS.search(full_text):
        score += 0.7
        signals.append("has_preservation_language")
    else:
        signals.append("no_preservation_language")

    if evidence_refs:
        score += 0.3
        score = min(score, 1.0)

    if score >= 0.7:
        explanation = "Invariant preservation confirmed"
    else:
        explanation = "No preservation language or evidence"

    return score, signals, explanation


# ── Stage-2 principal inference rules (p207-P5) ─────────────────────────────

def _infer_ontology_alignment(phase_record) -> tuple[float, list[str], str]:
    """ontology_alignment: declared phase matches the contract-expected phase for the subtype.

    Checks that PhaseRecord.phase is a recognized phase with a valid subtype mapping.
    A phase that maps to subtype="unknown" indicates misalignment.
    """
    phase = (getattr(phase_record, "phase", "") or "").upper()
    subtype = getattr(phase_record, "subtype", "") or ""
    score = 0.0
    signals: list[str] = []

    if not phase:
        signals.append("no_phase_declared")
        return score, signals, "No phase declared — ontology alignment cannot be assessed"

    # Phase must map to a known subtype (not "unknown")
    if subtype and subtype != "unknown":
        score += 0.7
        signals.append("phase_maps_to_known_subtype")
    else:
        signals.append("phase_maps_to_unknown_subtype")

    # Bonus: principals declared (agent is engaging with the principal protocol)
    principals = getattr(phase_record, "principals", []) or []
    if principals:
        score += 0.3
        score = min(score, 1.0)
        signals.append(f"declared_{len(principals)}_principals")

    if score >= 0.7:
        explanation = f"Phase '{phase}' aligned with subtype '{subtype}'"
    else:
        explanation = f"Phase '{phase}' maps to unknown subtype — ontology mismatch"

    return score, signals, explanation


def _infer_phase_boundary_discipline(phase_record) -> tuple[float, list[str], str]:
    """phase_boundary_discipline: no forbidden phase transition occurred.

    Checks that the current phase is a recognized phase and that no forbidden
    transition signal is present. Since individual phase_records don't carry
    transition history, we check for structural signals:
    - phase is recognized (not unknown)
    - allowed_next is defined for this phase
    - content doesn't contain signals of phase confusion (declaring a different phase)
    """
    phase = (getattr(phase_record, "phase", "") or "").upper()
    content = getattr(phase_record, "content", "") or ""
    score = 0.0
    signals: list[str] = []

    recognized_phases = {"OBSERVE", "ANALYZE", "DECIDE", "DESIGN", "EXECUTE", "JUDGE"}
    if phase not in recognized_phases:
        signals.append("unrecognized_phase")
        return score, signals, f"Phase '{phase}' not in recognized set — boundary discipline unknown"

    # Base score: phase is recognized
    score += 0.5
    signals.append("recognized_phase")

    # Check for phase confusion in content: agent declares a different phase
    # than what the record says (indicates boundary violation)
    phase_decl_re = re.compile(r"PHASE:\s*(\w+)", re.IGNORECASE)
    decl_match = phase_decl_re.search(content)
    if decl_match:
        declared = decl_match.group(1).strip().upper()
        # Normalize common variants
        norm_map = {"OBSERVATION": "OBSERVE", "ANALYSIS": "ANALYZE",
                    "EXECUTION": "EXECUTE", "JUDGMENT": "JUDGE", "JUDGEMENT": "JUDGE",
                    "DECISION": "DECIDE"}
        declared = norm_map.get(declared, declared)
        if declared == phase:
            score += 0.3
            signals.append("phase_declaration_consistent")
        else:
            score -= 0.3
            signals.append(f"phase_declaration_mismatch_{declared}_vs_{phase}")
    else:
        # No explicit phase declaration in content — neutral (not a violation)
        score += 0.2
        signals.append("no_phase_declaration_in_content")

    # Bonus: subtype is not "unknown" (boundary was resolvable)
    subtype = getattr(phase_record, "subtype", "") or ""
    if subtype and subtype != "unknown":
        score += 0.2
        score = min(score, 1.0)
        signals.append("subtype_resolved")

    if score >= 0.7:
        explanation = f"Phase boundary discipline maintained for '{phase}'"
    else:
        explanation = f"Phase boundary issue detected for '{phase}'"

    return score, signals, explanation


def _infer_action_grounding(phase_record) -> tuple[float, list[str], str]:
    """action_grounding: PLAN field references ROOT_CAUSE from ANALYZE phase.

    Checks that execution plan is grounded in the root cause analysis,
    not invented from scratch. Applies to execution.code_patch only.
    """
    structured = _extract_structured_from_content(phase_record)
    plan_text = structured.get("plan", "")
    root_cause_text = structured.get("root_cause", "")
    content = getattr(phase_record, "content", "") or ""
    score = 0.0
    signals: list[str] = []

    # Check 1: PLAN field exists and is non-empty
    if plan_text:
        score += 0.4
        signals.append("has_plan_field")
    else:
        signals.append("no_plan_field")

    # Check 2: PLAN references root cause (either via root_cause field or causal language)
    if plan_text and root_cause_text:
        # Direct reference: plan contains words from root cause
        rc_words = set(w.lower() for w in root_cause_text.split() if len(w) > 4)
        plan_words = set(w.lower() for w in plan_text.split())
        overlap = rc_words & plan_words
        if len(overlap) >= 2:
            score += 0.4
            signals.append(f"plan_references_root_cause_{len(overlap)}_words")
        elif overlap:
            score += 0.2
            signals.append("plan_weak_root_cause_reference")
        else:
            signals.append("plan_no_root_cause_reference")
    elif plan_text:
        # No root_cause field but plan exists — check for causal language in plan
        if _CAUSAL_KEYWORDS.search(plan_text):
            score += 0.3
            signals.append("plan_has_causal_language")
        else:
            signals.append("plan_no_causal_language")

    # Bonus: evidence_refs present (plan grounded in specific files)
    evidence_refs = getattr(phase_record, "evidence_refs", []) or []
    if evidence_refs:
        score += 0.2
        score = min(score, 1.0)
        signals.append("has_evidence_refs")

    if score >= 0.7:
        explanation = "Execution plan grounded in root cause analysis"
    else:
        explanation = "Execution plan not grounded — PLAN missing or no root cause reference"

    return score, signals, explanation


def _infer_option_comparison(phase_record) -> tuple[float, list[str], str]:
    """option_comparison: OPTIONS field has >= 2 distinct entries.

    Checks that the agent considered multiple approaches before deciding.
    Applies to decision.approach_selection / decision.fix_direction.
    """
    structured = _extract_structured_from_content(phase_record)
    options_text = structured.get("options", "")
    content = getattr(phase_record, "content", "") or ""
    score = 0.0
    signals: list[str] = []

    # Check OPTIONS field
    search_text = options_text if options_text else content
    if not search_text:
        signals.append("no_options_content")
        return score, signals, "No OPTIONS field or content — option comparison absent"

    # Count distinct option entries
    option_matches = _OPTION_LINE_RE.findall(search_text)
    option_count = len(option_matches)

    if option_count >= 2:
        score += 0.8
        signals.append(f"has_{option_count}_options")
    elif option_count == 1:
        score += 0.3
        signals.append("has_1_option_only")
    else:
        # Fallback: check for "Option" keyword mentions
        option_mentions = len(re.findall(r"\boption\b", search_text, re.IGNORECASE))
        if option_mentions >= 2:
            score += 0.5
            signals.append(f"option_keyword_mentions_{option_mentions}")
        else:
            signals.append("no_options_detected")

    # Bonus: SELECTED field exists (made a decision)
    if structured.get("selected", ""):
        score += 0.2
        score = min(score, 1.0)
        signals.append("has_selected_field")

    if score >= 0.7:
        explanation = f"Option comparison present ({option_count} options listed)"
    else:
        explanation = f"Insufficient option comparison ({option_count} options, need >= 2)"

    return score, signals, explanation


def _infer_constraint_satisfaction(phase_record) -> tuple[float, list[str], str]:
    """constraint_satisfaction: CONSTRAINTS field is non-empty.

    Checks that the agent identified constraints (what must not break).
    Applies to decision.approach_selection / decision.fix_direction.
    """
    structured = _extract_structured_from_content(phase_record)
    constraints_text = structured.get("constraints", "")
    score = 0.0
    signals: list[str] = []

    if constraints_text:
        score += 0.7
        signals.append("has_constraints_field")

        # Bonus: multiple constraint items (indicates thorough analysis)
        constraint_items = len(re.findall(r"(?:^|\n)\s*[-*•]\s", constraints_text))
        if constraint_items >= 2:
            score += 0.3
            score = min(score, 1.0)
            signals.append(f"has_{constraint_items}_constraint_items")
        elif constraint_items == 1:
            score += 0.1
            signals.append("has_1_constraint_item")
    else:
        signals.append("no_constraints_field")

    if score >= 0.7:
        explanation = "Constraints identified for decision"
    else:
        explanation = "No CONSTRAINTS section — constraint satisfaction not demonstrated"

    return score, signals, explanation


def _infer_result_verification(phase_record) -> tuple[float, list[str], str]:
    """result_verification: TEST_RESULTS field is non-empty.

    Checks that the agent ran tests and reported results.
    Applies to judge.patch_review / judge.verification.
    """
    structured = _extract_structured_from_content(phase_record)
    test_results_text = structured.get("test_results", "")
    content = getattr(phase_record, "content", "") or ""
    score = 0.0
    signals: list[str] = []

    if test_results_text:
        score += 0.7
        signals.append("has_test_results_field")

        # Bonus: contains pass/fail indicators
        has_pass_fail = bool(re.search(r"\b(pass|fail|error|ok|PASSED|FAILED)\b",
                                       test_results_text, re.IGNORECASE))
        if has_pass_fail:
            score += 0.3
            score = min(score, 1.0)
            signals.append("test_results_has_pass_fail")
    else:
        # Fallback: check content for test execution evidence
        has_test_run = bool(re.search(
            r"\b(ran\s+\d+\s+test|test.*passed|test.*failed|pytest|unittest)\b",
            content, re.IGNORECASE,
        ))
        if has_test_run:
            score += 0.5
            signals.append("content_has_test_evidence")
        else:
            signals.append("no_test_results")

    if score >= 0.7:
        explanation = "Test results reported for verification"
    else:
        explanation = "No TEST_RESULTS section — result verification not demonstrated"

    return score, signals, explanation


def _infer_uncertainty_honesty(phase_record) -> tuple[float, list[str], str]:
    """uncertainty_honesty: UNCERTAINTY field is non-empty.

    Checks that the agent acknowledges unknowns and limitations.
    Applies to analysis.root_cause.
    """
    structured = _extract_structured_from_content(phase_record)
    uncertainty_text = structured.get("uncertainty", "")
    content = getattr(phase_record, "content", "") or ""
    score = 0.0
    signals: list[str] = []

    if uncertainty_text:
        score += 0.8
        signals.append("has_uncertainty_field")

        # Bonus: specific uncertainty (not just "none" or "nothing")
        dismissive = re.match(r"^\s*(none|nothing|n/?a|no uncertainty)\s*$",
                              uncertainty_text, re.IGNORECASE)
        if dismissive:
            score -= 0.3
            signals.append("uncertainty_dismissed")
        else:
            score += 0.2
            score = min(score, 1.0)
            signals.append("uncertainty_substantive")
    else:
        # Fallback: check content for uncertainty language
        has_uncertainty = bool(re.search(
            r"\b(not sure|uncertain|might|could be|possible|unclear|unknown)\b",
            content, re.IGNORECASE,
        ))
        if has_uncertainty:
            score += 0.4
            signals.append("content_has_uncertainty_language")
        else:
            signals.append("no_uncertainty_acknowledged")

    if score >= 0.7:
        explanation = "Uncertainty acknowledged in analysis"
    else:
        explanation = "No UNCERTAINTY section — uncertainty honesty not demonstrated"

    return score, signals, explanation


# ── Register the 5 built-in rules ─────────────────────────────────────────────

register_rule(InferenceRule(
    principal="causal_grounding",
    infer=_infer_causal_grounding,
    applies_to=["analysis.root_cause"],
    threshold=0.5,  # P1 fix: evidence_refs alone is sufficient (was 0.7, required from_steps)
))

register_rule(InferenceRule(
    principal="evidence_linkage",
    infer=_infer_evidence_linkage,
    applies_to=None,  # all subtypes
    threshold=0.5,  # P1 fix: evidence_refs OR from_steps sufficient (was 0.7, required both AND)
))

register_rule(InferenceRule(
    principal="minimal_change",
    infer=_infer_minimal_change,
    applies_to=["execution.code_patch"],
    threshold=0.7,
))

register_rule(InferenceRule(
    principal="alternative_hypothesis_check",
    infer=_infer_alternative_hypothesis_check,
    applies_to=["analysis.root_cause"],
    threshold=0.7,
))

register_rule(InferenceRule(
    principal="invariant_preservation",
    infer=_infer_invariant_preservation,
    applies_to=["judge.verification"],
    threshold=0.7,
))

# ── Register the 7 stage-2 principal rules (p207-P5) ─────────────────────────

register_rule(InferenceRule(
    principal="ontology_alignment",
    infer=_infer_ontology_alignment,
    applies_to=None,  # all subtypes — ontology alignment is universal
    threshold=0.7,
))

register_rule(InferenceRule(
    principal="phase_boundary_discipline",
    infer=_infer_phase_boundary_discipline,
    applies_to=None,  # all subtypes — boundary discipline is universal
    threshold=0.7,
))

register_rule(InferenceRule(
    principal="action_grounding",
    infer=_infer_action_grounding,
    applies_to=["execution.code_patch"],
    threshold=0.7,
))

register_rule(InferenceRule(
    principal="option_comparison",
    infer=_infer_option_comparison,
    applies_to=["decision.fix_direction"],
    threshold=0.7,
))

register_rule(InferenceRule(
    principal="constraint_satisfaction",
    infer=_infer_constraint_satisfaction,
    applies_to=["decision.fix_direction"],
    threshold=0.7,
))

register_rule(InferenceRule(
    principal="result_verification",
    infer=_infer_result_verification,
    applies_to=["judge.verification"],
    threshold=0.7,
))

register_rule(InferenceRule(
    principal="uncertainty_honesty",
    infer=_infer_uncertainty_honesty,
    applies_to=["analysis.root_cause"],
    threshold=0.7,
))


# ── Engine ────────────────────────────────────────────────────────────────────

def run_inference(phase_record, subtype: str) -> InferredPrincipalResult:
    """
    Run all applicable rules for a phase_record and return a rich result.

    Rules are filtered by applies_to: if applies_to is None, the rule applies to all subtypes.
    If applies_to is a list, the rule only fires when subtype is in that list.

    Exception-safe: if a single rule raises, it is skipped; other rules continue.

    Args:
        phase_record: PhaseRecord or any object with behavioral attributes
        subtype: subtype string e.g. "analysis.root_cause", "execution.code_patch"

    Returns:
        InferredPrincipalResult with present/absent/details
    """
    present: list[str] = []
    absent: list[str] = []
    details: dict[str, InferenceResult] = {}

    for rule in _RULE_REGISTRY:
        # applies_to filter: None means all subtypes, list means must match
        if rule.applies_to is not None and subtype not in rule.applies_to:
            continue

        try:
            score, signals, explanation = rule.infer(phase_record)
        except Exception:
            # Exception safety: skip this rule, do not affect others
            continue

        result = InferenceResult(
            principal=rule.principal,
            score=score,
            signals=signals,
            explanation=explanation,
            threshold=rule.threshold,
        )
        details[rule.principal] = result

        if score >= rule.threshold:
            present.append(rule.principal)
        else:
            absent.append(rule.principal)

    return InferredPrincipalResult(
        subtype=subtype,
        present=present,
        absent=absent,
        details=details,
    )


# ── Backward-compatible wrapper ───────────────────────────────────────────────

def infer_principals(phase_record) -> list[str]:
    """
    Backward-compatible wrapper. Returns list of inferred principal names.

    Derives subtype from phase_record.phase via _PHASE_TO_SUBTYPE map.
    Exception-safe: if subtype_contracts unavailable, uses empty subtype.
    """
    try:
        from subtype_contracts import _PHASE_TO_SUBTYPE
        phase = (getattr(phase_record, "phase", "") or "").upper()
        subtype = _PHASE_TO_SUBTYPE.get(phase, "")
    except Exception:
        subtype = ""

    result = run_inference(phase_record, subtype)
    return result.present


def diff_principals(
    declared: list[str],
    inferred: list[str] | InferredPrincipalResult,
    phase: str = "",
) -> dict:
    """
    Three-way diff of declared vs inferred principals.

    Returns:
        {
            "missing_required": [...],  # required by contract but not declared (hard reject)
            "missing_expected": [...],  # expected by contract but not declared (soft warn)
            "fake":             [...],  # declared but not inferred (hard reject)
        }

    Args:
        declared: principals the agent declared (from PhaseRecord.principals)
        inferred: principals inferred — accepts list[str] (legacy) or InferredPrincipalResult (p195)
        phase: phase name string (e.g. "ANALYZE"); used to load required/expected from contracts

    Exception-safe: if subtype_contracts import fails, required/expected default to empty sets.
    """
    # Accept both list[str] and InferredPrincipalResult
    if isinstance(inferred, InferredPrincipalResult):
        inferred_names = inferred.present
    else:
        inferred_names = list(inferred)

    try:
        from subtype_contracts import get_required_principals, get_expected_principals
        required: set[str] = set(get_required_principals(phase)) if phase else set()
        expected: set[str] = set(get_expected_principals(phase)) if phase else set()
    except Exception:
        required = set()
        expected = set()

    declared_norm = {p.lower() for p in declared}
    inferred_norm = {p.lower() for p in inferred_names}

    # Derive the subtype for this phase so we can determine which rules actually ran.
    # CC2: inferrable = rules-that-ran for this subtype, NOT all rules in registry.
    # A rule with applies_to filter that didn't match the current subtype did not run —
    # its principal cannot be judged fake (absence of unevaluated inference ≠ fake).
    try:
        from subtype_contracts import _PHASE_TO_SUBTYPE as _cc2_subtype_map
        _cc2_subtype = _cc2_subtype_map.get(phase.upper(), "") if phase else ""
    except Exception:
        _cc2_subtype = ""

    # inferrable: principals whose rule actually ran for this phase/subtype.
    # A rule ran if: applies_to is None (all subtypes) OR subtype is in applies_to.
    inferrable = {
        rule.principal.lower()
        for rule in _RULE_REGISTRY
        if rule.applies_to is None or (_cc2_subtype and _cc2_subtype in rule.applies_to)
    }

    # fake: declared AND inferrable but not inferred (agent claimed without behavioral support)
    # Principals whose rule didn't run (applies_to mismatch) are excluded — CC2 invariant.
    fake = sorted((declared_norm & inferrable) - inferred_norm)

    # missing_required: required by contract but not declared (hard reject)
    missing_required = sorted(required - declared_norm)

    # missing_expected: expected by contract but not declared
    # (only those not already captured in missing_required)
    missing_expected = sorted((expected - declared_norm) - required)

    return {
        "missing_required": missing_required,
        "missing_expected": missing_expected,
        "fake": fake,
    }


# ── V3 stub: RetryHint interface (consumed by retry shaping, not implemented yet) ──

@dataclass
class RetryHintInput:
    """Input to retry hint generator. Produced by diff_principals()."""
    subtype: str
    missing_required: list[str] = field(default_factory=list)
    missing_expected: list[str] = field(default_factory=list)
    fake: list[str] = field(default_factory=list)


@dataclass
class RepairHint:
    """A single retry/repair directive for the agent."""
    principal: str
    severity: str   # "hard" | "soft"
    message: str


def build_retry_hints(input: RetryHintInput) -> list[RepairHint]:
    """
    Build targeted retry hints from a PrincipalDiffResult.

    v0.1 stub — returns empty list. Implementation in V3 (after p196 validation data).
    """
    return []
