"""
cognition_schema.py — Phase 2 in-loop cognition record parser + validator.

Parses structured phase records from LLM assistant message text.
Validates principal contracts per CDP v2.0 taxonomy (derived from subtype_contracts.py).
Returns violations for injection back into agent context.

Design: post-generation parsing (minisweagent doesn't support structured output).
Agent outputs free text; this module extracts the structured record from it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ── CDP v2.0 taxonomy (derived from subtype_contracts.py) ────────────────────

# Phase normalization — derived from canonical_symbols (SST: single definition)
from canonical_symbols import _PHASE_ALIASES as _PHASE_NORM  # noqa: E402

# VALID_PHASES: lowercase keys only (for cognition_schema parser compatibility)
VALID_PHASES = {k for k in _PHASE_NORM if k.islower()}


def _build_principal_contracts() -> dict[str, dict[str, list[str]]]:
    """
    Build PRINCIPAL_CONTRACTS from v2.0 subtype_contracts.py (canonical source).

    Returns a dict keyed by lowercase phase name (as parsed from agent output),
    with required/forbidden principals derived from the canonical SubtypeContract.
    SST2: consumers reference, never redeclare.
    """
    try:
        from scripts.subtype_contracts import SUBTYPE_CONTRACTS
    except ImportError:
        try:
            from subtype_contracts import SUBTYPE_CONTRACTS
        except ImportError:
            return {}  # graceful degradation per SST2 — empty, not stale copy

    # Reverse map: canonical phase → lowercase parser key(s)
    _upper_to_lower: dict[str, str] = {}
    for lower, upper in _PHASE_NORM.items():
        # Use the first (most natural) lowercase name for each canonical phase
        if upper not in _upper_to_lower:
            _upper_to_lower[upper] = lower

    contracts: dict[str, dict[str, list[str]]] = {}
    for _subtype, sc in SUBTYPE_CONTRACTS.items():
        phase_upper = sc.get("phase", "")
        lower_key = _upper_to_lower.get(phase_upper)
        if lower_key and lower_key not in contracts:
            contracts[lower_key] = {
                "required": list(sc.get("required_principals", [])),
                "forbidden": list(sc.get("forbidden_principals", [])),
            }
    return contracts


PRINCIPAL_CONTRACTS: dict[str, dict[str, list[str]]] = _build_principal_contracts()

# Phases that require at least one evidence_ref before action (v2.0: ANALYZE, OBSERVE)
EVIDENCE_REQUIRED_PHASES = {"analysis", "observation", "analyze", "observe"}

# Phases where action type must be "none" (no code write) (v2.0: OBSERVE, ANALYZE)
NO_ACTION_PHASES = {"analysis", "observation", "analyze", "observe"}


# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class CognitionRecord:
    phase: str
    subtype: str = ""
    principals: list[str] = field(default_factory=list)
    claims: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    action_type: str = "none"   # none | patch | command
    step_n: int = 0
    raw_text: str = ""


@dataclass
class CognitionViolation:
    code: str
    message: str
    phase: str
    step_n: int


# ── Parser ───────────────────────────────────────────────────────────────────

def _extract_field(text: str, key: str) -> str:
    """Extract single-line field: KEY: value"""
    m = re.search(rf"(?:^|\n)\s*\*{{0,2}}{re.escape(key)}\*{{0,2}}:\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _extract_list_field(text: str, key: str) -> list[str]:
    """Extract comma or space-separated list from KEY: val1 val2.
    For short token lists (principals, action types).
    For long prose fields (evidence, claims), use _extract_field directly.
    """
    raw = _extract_field(text, key)
    if not raw:
        return []
    # If raw looks like prose (> 5 words, no commas), treat as single item
    words = raw.split()
    if len(words) > 5 and "," not in raw:
        return [raw.strip()]
    # Split on commas, spaces, or pipes
    items = re.split(r"[,\s|]+", raw)
    return [i.strip().lower() for i in items if i.strip() and i.strip() not in ("-", "none", "n/a")]


def parse_cognition_record(text: str, step_n: int = 0) -> Optional[CognitionRecord]:
    """
    Parse a CognitionRecord from assistant message text.
    Returns None if no phase marker found (record is optional per step).
    """
    phase = _extract_field(text, "PHASE") or _extract_field(text, "DECISION")
    if not phase:
        return None

    # Normalise: "execution: implement the fix..." → "execution"
    phase = phase.split()[0].split(":")[0].strip().lower()
    if phase not in VALID_PHASES:
        return None

    principals = _extract_list_field(text, "PRINCIPALS")
    evidence_refs = _extract_list_field(text, "EVIDENCE") or _extract_list_field(text, "EVIDENCE_REFS")
    claims_raw = _extract_field(text, "CLAIMS") or _extract_field(text, "ROOT_CAUSE") or _extract_field(text, "HYPOTHESIS")
    claims = [claims_raw] if claims_raw else []
    subtype = _extract_field(text, "SUBTYPE") or _extract_field(text, "SCOPE") or ""

    # Detect action type from text signals
    action_type = "none"
    if re.search(r"\bwrite\b|\bedit\b|\bpatch\b|\bcreate file\b", text, re.IGNORECASE):
        action_type = "patch"
    elif re.search(r"\brun\b|\bexecute\b|\bcommand\b|\bbash\b", text, re.IGNORECASE):
        action_type = "command"

    return CognitionRecord(
        phase=phase,
        subtype=subtype,
        principals=principals,
        claims=claims,
        evidence_refs=evidence_refs,
        action_type=action_type,
        step_n=step_n,
        raw_text=text[:200],
    )


# ── Validator ─────────────────────────────────────────────────────────────────

def validate_cognition_record(record: CognitionRecord) -> list[CognitionViolation]:
    violations: list[CognitionViolation] = []
    phase = record.phase
    step_n = record.step_n

    contract = PRINCIPAL_CONTRACTS.get(phase, {})
    required = contract.get("required", [])
    forbidden = contract.get("forbidden", [])

    # V1: missing required principals
    for p in required:
        if p not in record.principals:
            violations.append(CognitionViolation(
                code="MISSING_REQUIRED_PRINCIPAL",
                message=f"phase={phase} requires principal '{p}' but it is absent. "
                        f"Add '{p}' to PRINCIPALS.",
                phase=phase,
                step_n=step_n,
            ))

    # V2: forbidden principals present
    for p in forbidden:
        if p in record.principals:
            violations.append(CognitionViolation(
                code="FORBIDDEN_PRINCIPAL",
                message=f"phase={phase} forbids principal '{p}' but it is present. "
                        f"Remove '{p}' from PRINCIPALS.",
                phase=phase,
                step_n=step_n,
            ))

    # V3: analysis/diagnosis/observation must have evidence before action
    if phase in EVIDENCE_REQUIRED_PHASES and record.action_type != "none":
        if not record.evidence_refs:
            violations.append(CognitionViolation(
                code="ACTION_WITHOUT_EVIDENCE",
                message=f"phase={phase} requires EVIDENCE_REFS before taking action "
                        f"(action_type={record.action_type}). "
                        f"Add EVIDENCE: <file:line or test name> before writing code.",
                phase=phase,
                step_n=step_n,
            ))

    # V4: execution must have at least one evidence ref (grounded action)
    if phase == "execution" and not record.evidence_refs and not record.claims:
        violations.append(CognitionViolation(
            code="UNGROUNDED_EXECUTION",
            message="phase=execution has no EVIDENCE or CLAIMS. "
                    "Add EVIDENCE: <what analysis step justified this change>.",
            phase=phase,
            step_n=step_n,
        ))

    # V5: claims require supporting evidence (p203 axis 3 — evidence_supports_claims)
    # If the record makes claims but cites no evidence, the claims are unverifiable.
    # Applies to all phases — a claim without evidence is a hypothesis, not a finding.
    if record.claims and not record.evidence_refs:
        violations.append(CognitionViolation(
            code="CLAIMS_WITHOUT_EVIDENCE",
            message=f"phase={phase} has {len(record.claims)} claim(s) but no EVIDENCE_REFS. "
                    f"Add EVIDENCE: <file:line or test name> supporting each claim. "
                    f"Claims without evidence are hypotheses, not findings.",
            phase=phase,
            step_n=step_n,
        ))

    return violations


# ── Feedback formatter ────────────────────────────────────────────────────────

def format_violation_feedback(violations: list[CognitionViolation], record: CognitionRecord) -> str:
    """
    Format violations as a feedback message to inject back into agent context.
    Short and actionable — agent must correct before continuing.
    """
    lines = [
        f"[jingu/cognition] VIOLATION at step={record.step_n} phase={record.phase}:",
    ]
    for v in violations:
        lines.append(f"  [{v.code}] {v.message}")
    lines.append(
        "Correct your PRINCIPALS / EVIDENCE and re-state the phase record before proceeding."
    )
    return "\n".join(lines)


# ── Public API ────────────────────────────────────────────────────────────────

def check_step_cognition(text: str, step_n: int) -> tuple[Optional[CognitionRecord], list[CognitionViolation]]:
    """
    Parse + validate a single step's assistant text.
    Returns (record, violations). record=None means no phase marker found (not an error).
    """
    record = parse_cognition_record(text, step_n)
    if record is None:
        return None, []
    violations = validate_cognition_record(record)
    return record, violations
