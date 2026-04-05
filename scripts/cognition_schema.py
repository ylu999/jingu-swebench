"""
cognition_schema.py — Phase 2 in-loop cognition record parser + validator.

Parses structured phase records from LLM assistant message text.
Validates principal contracts per CDP v1 taxonomy.
Returns violations for injection back into agent context.

Design: post-generation parsing (minisweagent doesn't support structured output).
Agent outputs free text; this module extracts the structured record from it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ── CDP v1 taxonomy ──────────────────────────────────────────────────────────

VALID_PHASES = {
    "analysis", "decision", "design", "execution",
    "observation", "understanding", "diagnosis", "planning", "validation",
}

# Required and forbidden principals per phase (subset — execution + diagnosis enforced strictly)
PRINCIPAL_CONTRACTS: dict[str, dict[str, list[str]]] = {
    "execution": {
        "required": ["scope_control", "minimal_change"],
        "forbidden": ["causality", "hypothesis_testing"],
    },
    "diagnosis": {
        "required": ["evidence_based", "causality"],
        "forbidden": ["minimal_change"],
    },
    "analysis": {
        "required": ["causality"],
        "forbidden": ["execution_first", "scope_control"],
    },
    "validation": {
        "required": ["execution_first", "consistency_check"],
        "forbidden": ["causality", "hypothesis_testing"],
    },
    "observation": {
        "required": ["evidence_based", "no_hallucination"],
        "forbidden": ["minimal_change", "scope_control"],
    },
    "understanding": {
        "required": ["constraint_awareness", "explicit_assumption"],
        "forbidden": ["execution_first", "minimal_change"],
    },
    "decision": {
        "required": ["constraint_awareness"],
        "forbidden": ["execution_first"],
    },
    "design": {
        "required": ["constraint_awareness", "completeness"],
        "forbidden": ["execution_first"],
    },
    "planning": {
        "required": ["completeness", "consistency_check"],
        "forbidden": ["execution_first", "minimal_change"],
    },
}

# Phases that require at least one evidence_ref before action
EVIDENCE_REQUIRED_PHASES = {"analysis", "diagnosis", "observation"}

# Phases where action type must be "none" (no code write)
NO_ACTION_PHASES = {"analysis", "diagnosis", "observation", "understanding"}


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
    """Extract comma or space-separated list from KEY: val1 val2"""
    raw = _extract_field(text, key)
    if not raw:
        return []
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
