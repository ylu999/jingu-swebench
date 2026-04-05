"""
principal_inference.py — Deterministic heuristic inference of principals from PhaseRecord.

Version: v0.1 — 4 inferred principals (p194)
Rules are deterministic, inspectable, no LLM.
Invariant: same input = same output.

Principals inferred:
  causal_grounding           — evidence_refs non-empty + causal language in content/claims
  evidence_linkage           — evidence_refs non-empty AND from_steps non-empty
  minimal_change             — EXECUTE phase + content line count <= 30
  alternative_hypothesis_check — ANALYZE phase + alternatives language in content/claims
  invariant_preservation     — JUDGE phase + preservation language in content/claims
"""

from __future__ import annotations

import re

# ── Keyword patterns (deterministic regex, no LLM) ──────────────────────────

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


def infer_principals(phase_record) -> list[str]:
    """
    Infer principals from PhaseRecord behavioral signals.

    Returns a list of inferred principal strings (lowercase).
    All rules are deterministic — same input = same output.

    Args:
        phase_record: PhaseRecord or any object with attributes:
            phase (str), evidence_refs (list), claims (list[str]),
            from_steps (list), content (str)
    """
    inferred: list[str] = []

    phase = (getattr(phase_record, "phase", "") or "").upper()
    evidence_refs = getattr(phase_record, "evidence_refs", []) or []
    claims = getattr(phase_record, "claims", []) or []
    from_steps = getattr(phase_record, "from_steps", []) or []
    content = getattr(phase_record, "content", "") or ""

    claims_text = " ".join(str(c) for c in claims)
    full_text = content + " " + claims_text

    # Rule 1: causal_grounding — evidence present + causal language
    if evidence_refs and _CAUSAL_KEYWORDS.search(full_text):
        inferred.append("causal_grounding")

    # Rule 2: evidence_linkage — evidence refs present AND step provenance present
    if evidence_refs and from_steps:
        inferred.append("evidence_linkage")

    # Rule 3: minimal_change — EXECUTE phase + short patch (heuristic: line count <= 30)
    if phase == "EXECUTE":
        patch_lines = content.count("\n")
        if patch_lines <= _SMALL_PATCH_MAX_LINES:
            inferred.append("minimal_change")

    # Rule 4a: alternative_hypothesis_check — ANALYZE phase + alternatives language
    if phase in ("ANALYZE", "ANALYSIS") and _ALTERNATIVE_KEYWORDS.search(full_text):
        inferred.append("alternative_hypothesis_check")

    # Rule 4b: invariant_preservation — JUDGE/DESIGN phase + preservation language
    if phase in ("JUDGE", "DESIGN") and _PRESERVE_KEYWORDS.search(full_text):
        inferred.append("invariant_preservation")

    return inferred


def diff_principals(
    declared: list[str],
    inferred: list[str],
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
        inferred: principals inferred by infer_principals()
        phase: phase name string (e.g. "ANALYZE"); used to load required/expected from contracts

    Exception-safe: if subtype_contracts import fails, required/expected default to empty sets.
    """
    try:
        from subtype_contracts import get_required_principals, get_expected_principals
        required: set[str] = set(get_required_principals(phase)) if phase else set()
        expected: set[str] = set(get_expected_principals(phase)) if phase else set()
    except Exception:
        required = set()
        expected = set()

    declared_norm = {p.lower() for p in declared}
    inferred_norm = {p.lower() for p in inferred}

    # fake: declared but not inferred (agent claimed without behavioral support)
    fake = sorted(declared_norm - inferred_norm)

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

from dataclasses import dataclass, field


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
