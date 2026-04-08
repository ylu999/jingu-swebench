"""
declaration_extractor.py — Extract type/principal declaration from agent output.

Looks for FIX_TYPE: and PRINCIPALS: lines in the last 500 chars of agent output.
Returns {"type": str, "principals": [str]} or {} if not found.

This is structural extraction — deterministic regex, no LLM.
"""

import re
from typing import TypedDict


class Declaration(TypedDict, total=False):
    type: str
    principals: list[str]


_FIX_TYPE_RE = re.compile(r"FIX_TYPE:\s*([a-z_]+)", re.IGNORECASE)
_PRINCIPALS_RE = re.compile(r"PRINCIPALS:\s*([^\n]+)", re.IGNORECASE)
_PHASE_RE = re.compile(r"PHASE:\s*([a-z_]+)", re.IGNORECASE)

# Structured field regexes for causal binding (p23)
# Matches "FIELD_NAME:\n<content>" until the next ALL_CAPS_FIELD: or end of string
_STRUCTURED_FIELD_RE = re.compile(
    r"^([A-Z_]{3,}):\s*\n(.*?)(?=\n[A-Z_]{3,}:|\Z)",
    re.MULTILINE | re.DOTALL,
)


def extract_structured_fields(text: str) -> dict[str, str]:
    """Extract structured output sections (ROOT_CAUSE, CAUSAL_CHAIN, PLAN, etc.).

    Parses "FIELD_NAME:\n<content>" blocks from agent output.
    Returns dict of field_name (lowercased) -> stripped content.
    Never raises.
    """
    if not text:
        return {}
    result: dict[str, str] = {}
    for m in _STRUCTURED_FIELD_RE.finditer(text):
        key = m.group(1).strip().lower()
        val = m.group(2).strip()
        if val:
            result[key] = val
    return result


def extract_declaration(agent_output: str) -> Declaration:
    """
    Extract fix type and principals from agent output.

    Scans the last 500 characters where declarations are expected to appear.
    Returns {} if FIX_TYPE is not found (opt-in gate).
    """
    if not agent_output:
        return {}

    tail = agent_output[-500:]

    type_match = _FIX_TYPE_RE.search(tail)
    if not type_match:
        return {}

    fix_type = type_match.group(1).strip().lower()

    principals: list[str] = []
    principals_match = _PRINCIPALS_RE.search(tail)
    if principals_match:
        raw = principals_match.group(1).strip()
        # Accept comma or space separated principals
        principals = [p.strip().lower() for p in re.split(r"[,\s]+", raw) if p.strip()]

    return {"type": fix_type, "principals": principals}


def extract_last_agent_message(messages: list[dict]) -> str:
    """
    Extract the last assistant message text from a traj messages list.
    Returns "" if no assistant message found.
    """
    for m in reversed(messages):
        if m.get("role") != "assistant":
            continue
        content = m.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # Claude API format: list of content blocks
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            return "\n".join(parts)
    return ""


if __name__ == "__main__":
    # Smoke test
    sample = """
After applying the fix:

FIX_TYPE: execution
PRINCIPALS: evidence_based minimal_change causality
"""
    result = extract_declaration(sample)
    assert result["type"] == "execution", f"expected execution, got {result}"
    assert "evidence_based" in result["principals"], result
    print("PASS declaration_extractor smoke test")

    # No declaration
    assert extract_declaration("some output without declaration") == {}
    print("PASS no-declaration returns empty dict")


# ── Per-phase record extraction ────────────────────────────────────────────────

import re as _re

_EVIDENCE_REF_RE = _re.compile(
    r"(?:[a-zA-Z0-9_\-./]+\.py(?::[\d]+)?)",  # file.py or file.py:123
)

# Load subtype names from canonical source (subtype_contracts._PHASE_TO_SUBTYPE).
# This ensures declaration_extractor and evaluate_admission use the same subtype strings,
# so PhaseRecord.subtype matches the keys in SUBTYPE_CONTRACTS.
# Fallback: static map if subtype_contracts is unavailable (no crash).
try:
    from subtype_contracts import _PHASE_TO_SUBTYPE as _PHASE_SUBTYPE_MAP  # type: ignore[assignment]
except Exception:
    _PHASE_SUBTYPE_MAP: dict[str, str] = {
        "OBSERVE":  "observation",
        "ANALYZE":  "analysis.root_cause",
        "EXECUTE":  "execution.code_patch",
        "JUDGE":    "judge.verification",
    }

# Agent-declared phase names may use gerund/noun variants (e.g. "execution", "observation").
# Normalize to canonical Phase enum values before _PHASE_SUBTYPE_MAP lookup.
_PHASE_NORM: dict[str, str] = {
    "OBSERVATION": "OBSERVE",
    "ANALYSIS":    "ANALYZE",
    "DECISION":    "DECIDE",
    "EXECUTION":   "EXECUTE",
    "JUDGEMENT":   "JUDGE",
    "JUDGMENT":    "JUDGE",
}


def _extract_phase_from_message(agent_message: str) -> str | None:
    """Extract PHASE: declaration from agent message.

    Returns the agent-declared phase (uppercased) if found, else None.
    This is the source of truth for _pr.phase — cp_state.phase is only a fallback.
    """
    if not agent_message:
        return None
    m = _PHASE_RE.search(agent_message)
    if not m:
        return None
    return m.group(1).strip().upper()


def _extract_principals_from_message(agent_message: str) -> list[str]:
    """Extract PRINCIPALS: declaration from anywhere in agent message.

    Searches the full message (not just tail) so phase-level declarations
    are captured even when they appear early in the agent's reasoning.
    Returns [] if no PRINCIPALS line found.
    """
    if not agent_message:
        return []
    m = _PRINCIPALS_RE.search(agent_message)
    if not m:
        return []
    raw = m.group(1).strip()
    return [p.strip().lower() for p in _re.split(r"[,\s]+", raw) if p.strip()]


def _extract_evidence_refs(agent_message: str) -> list[str]:
    """Extract file:line reference strings from agent message.

    Looks for patterns like 'django/db/models.py:45' or 'tests/test_foo.py'.
    Returns up to 10 unique matches to keep the record compact.
    """
    if not agent_message:
        return []
    matches = _EVIDENCE_REF_RE.findall(agent_message)
    seen: set[str] = set()
    result: list[str] = []
    for m in matches:
        if m not in seen:
            seen.add(m)
            result.append(m)
        if len(result) >= 10:
            break
    return result


def extract_phase_record(agent_message: str, phase: str, from_steps: list[int] | None = None):
    """Extract a PhaseRecord from the last agent message for the given phase.

    Parsing strategy (structure-first, surface fallback):
    - subtype:       mapped from phase name (OBSERVE -> observation, etc.)
    - principals:    extracted from PRINCIPALS: line in agent message
    - claims:        [] in initial version (p191 will utilize PhaseRecord for semantic check)
    - evidence_refs: file:line patterns found in agent message
    - from_steps:    step indices this record derives from (for gate provenance); default []
    - content:       agent_message[:500] (truncated raw content)

    Returns a PhaseRecord. Never raises — caller wraps in try/except.

    NOTE: This function uses P14 semantics — agent-declared phase takes priority over
    the `phase` parameter. This is correct for phase-intent detection (understanding
    what the agent wants to do next), but NOT for gate evaluation path.
    For gate evaluation, use extract_record_for_phase() which enforces target_phase.
    """
    from phase_record import PhaseRecord

    # P14 fix: prefer agent-declared phase over cp_state fallback.
    # extract_phase_record(msg, cp_s.phase) was using cp_s.phase unconditionally,
    # so agent declaring "execution" while cp advances to "ANALYZE" produced
    # _pr.phase="ANALYZE" — wrong contract applied → REJECTED.
    declared = _extract_phase_from_message(agent_message)
    phase_upper = declared if declared else (phase or "").upper()
    phase_upper = _PHASE_NORM.get(phase_upper, phase_upper)
    subtype = _PHASE_SUBTYPE_MAP.get(phase_upper, "unknown")
    principals = _extract_principals_from_message(agent_message)
    evidence_refs = _extract_evidence_refs(agent_message)

    structured = extract_structured_fields(agent_message or "")
    return PhaseRecord(
        phase=phase_upper or phase,
        subtype=subtype,
        principals=principals,
        claims=[],
        evidence_refs=evidence_refs,
        from_steps=from_steps if from_steps is not None else [],
        content=(agent_message or "")[:500],
        root_cause=structured.get("root_cause", ""),
        causal_chain=structured.get("causal_chain", ""),
        plan=structured.get("plan", ""),
    )


def extract_record_for_phase(
    agent_message: str,
    target_phase: str,
    from_steps: list[int] | None = None,
) -> tuple:
    """
    Gate evaluation path: extract a PhaseRecord for target_phase.

    Invariant: record_phase == target_phase, always.
    Agent's self-declared phase DOES NOT override target_phase here.

    This separates two concerns that were conflated in extract_phase_record:
    - Phase declaration intent (what agent claims it's doing) → extract_phase_record
    - Gate evaluation record (what gate must evaluate)        → this function

    Returns: (phase_record, declared_phase, foreign_phase_declared)
      phase_record:          PhaseRecord with phase=target_phase (always)
      declared_phase:        what the agent declared (may differ from target_phase)
      foreign_phase_declared: True if declared_phase != target_phase and declared_phase != ""

    Never raises — caller wraps in try/except.
    """
    from phase_record import PhaseRecord

    # Step 1: what did the agent declare?
    declared_raw = _extract_phase_from_message(agent_message)
    declared_phase = _PHASE_NORM.get(declared_raw, declared_raw) if declared_raw else ""

    # Step 2: target_phase is authoritative for this record — never let agent override it
    target_upper = _PHASE_NORM.get(target_phase.upper(), target_phase.upper())
    subtype = _PHASE_SUBTYPE_MAP.get(target_upper, "unknown")

    # Step 3: extract content signals (principals, evidence_refs apply to this record
    # only if the agent was addressing target_phase, else they belong to another phase)
    foreign_phase_declared = bool(declared_phase and declared_phase != target_upper)
    if foreign_phase_declared:
        # 改动10 (v1): foreign phase declared — preserve grounded evidence, discard phase-scoped signals.
        # Rationale: phase boundary is a governance constraint, not a signal destruction gate.
        # evidence_refs (file:line patterns) are mechanically extractable facts — they exist
        # independent of which phase the agent claims to be in. Discarding them converts the
        # gate from a governance layer into an information circuit-breaker, causing infinite
        # RETRYABLE loops where the agent has real evidence but the gate never sees it.
        #
        # What we keep:    evidence_refs — grounded, phase-neutral, mechanically extracted
        # What we discard: principals, content — phase-scoped, untrustworthy under foreign context
        #
        # Gate receives: foreign_phase_declared=True + evidence_refs populated
        # Gate should issue: foreign_phase_declared / principals_untrusted (not missing_evidence)
        principals: list[str] = []          # untrusted: declared for foreign phase
        evidence_refs = _extract_evidence_refs(agent_message)   # preserved: phase-neutral facts
        content = ""                        # discarded: belongs to declared (foreign) phase
    else:
        # Agent was addressing target_phase (or made no phase declaration) —
        # extract signals normally.
        principals = _extract_principals_from_message(agent_message)
        evidence_refs = _extract_evidence_refs(agent_message)
        content = (agent_message or "")[:500]

    structured = extract_structured_fields(agent_message or "") if not foreign_phase_declared else {}
    record = PhaseRecord(
        phase=target_upper or target_phase,
        subtype=subtype,
        principals=principals,
        claims=[],
        evidence_refs=evidence_refs,
        from_steps=from_steps if from_steps is not None else [],
        content=content,
        root_cause=structured.get("root_cause", ""),
        causal_chain=structured.get("causal_chain", ""),
        plan=structured.get("plan", ""),
    )
    return record, declared_phase, foreign_phase_declared
