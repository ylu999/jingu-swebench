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

Principals inferred:
  causal_grounding           — evidence_refs non-empty + causal language in content/claims
  evidence_linkage           — evidence_refs non-empty AND from_steps non-empty
  minimal_change             — execution.code_patch subtype + content line count <= 30
  alternative_hypothesis_check — analysis.root_cause subtype + alternatives language
  invariant_preservation     — judge.verification subtype + preservation language
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
    r"\b(alternative|another possibility|could also|or instead|other approach)\b",
    re.IGNORECASE,
)
_PRESERVE_KEYWORDS = re.compile(
    r"\b(does not change|preserve|maintain|invariant|unchanged|no side effect)\b",
    re.IGNORECASE,
)

_SMALL_PATCH_MAX_LINES = 30


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


# ── Rule implementations ──────────────────────────────────────────────────────

def _infer_causal_grounding(phase_record) -> tuple[float, list[str], str]:
    """causal_grounding: evidence_refs present + causal language + from_steps linkage."""
    evidence_refs, from_steps, _content, full_text = _extract_fields(phase_record)
    score = 0.0
    signals: list[str] = []

    if evidence_refs:
        score += 0.4
        signals.append("has_evidence_refs")
    else:
        # without evidence_refs we can still get partial score for causal language
        pass

    if _CAUSAL_KEYWORDS.search(full_text):
        score += 0.4
        signals.append("has_causal_language")

    if from_steps:
        score += 0.2
        signals.append("has_step_linkage")

    # Require evidence_refs as minimum for causal_grounding to fire at all
    if not evidence_refs:
        score = 0.0

    if score >= 0.7:
        explanation = f"Causal reasoning supported by {len(evidence_refs)} evidence ref(s)"
    else:
        explanation = "Missing causal explanation or evidence"

    return score, signals, explanation


def _infer_evidence_linkage(phase_record) -> tuple[float, list[str], str]:
    """evidence_linkage: evidence_refs present AND from_steps present."""
    evidence_refs, from_steps, _content, _full_text = _extract_fields(phase_record)
    score = 0.0
    signals: list[str] = []

    if evidence_refs:
        score += 0.5
        signals.append("has_evidence_refs")

    if from_steps:
        score += 0.5
        signals.append("has_step_linkage")

    if score >= 1.0:
        explanation = "Evidence chain present with step linkage"
    elif score > 0:
        explanation = "Partial evidence chain (missing refs or step linkage)"
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


# ── Register the 5 built-in rules ─────────────────────────────────────────────

register_rule(InferenceRule(
    principal="causal_grounding",
    infer=_infer_causal_grounding,
    applies_to=["analysis.root_cause"],
    threshold=0.7,
))

register_rule(InferenceRule(
    principal="evidence_linkage",
    infer=_infer_evidence_linkage,
    applies_to=None,  # all subtypes
    threshold=0.7,
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

    # inferrable: principals that have at least one registered inference rule.
    # A declared principal with no inference rule cannot be verified or falsified —
    # it must not be counted as fake. Only principals the engine can actually
    # evaluate are subject to the fake check.
    inferrable = {rule.principal.lower() for rule in _RULE_REGISTRY}

    # fake: declared AND inferrable but not inferred (agent claimed without behavioral support)
    # Principals with no inference rule are excluded — absence of inference ≠ fake.
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
