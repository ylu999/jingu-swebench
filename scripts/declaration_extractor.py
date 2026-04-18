"""
declaration_extractor.py — Extract type/principal declaration from agent output.

Looks for FIX_TYPE: and PRINCIPALS: lines in the last 2000 chars of agent output.
Returns {"type": str, "principals": [str]} or {} if not found.

This is structural extraction — deterministic regex, no LLM.
"""

import re
from dataclasses import dataclass, field as dataclass_field
from typing import TypedDict


class Declaration(TypedDict, total=False):
    type: str
    principals: list[str]


# ── Extraction telemetry types (C-05) ────────────────────────────────────────


@dataclass
class ExtractionMeta:
    """Metadata about how a PhaseRecord was extracted."""
    source: str                    # "tool_submitted" | "structured_extract" | "regex_fallback"
    fields_in_schema: list[str]    # fields defined in bundle schema for this phase
    fields_extracted: list[str]    # fields successfully extracted (non-empty)
    fields_missing: list[str]     # fields in schema but not extracted
    fields_extra: list[str]       # fields extracted but not in schema


@dataclass
class FieldExtractionRecord:
    """Per-field extraction detail for telemetry."""
    field: str
    in_schema: bool
    prompted: bool
    present_in_response: bool
    extracted: bool
    extraction_source: str
    value_type: str                # "str" | "list" | "dict" | "empty"
    missing_reason: str            # "" | "not_in_response" | "no_phaserecord_field" | "extraction_failed"


@dataclass
class ExtractionTelemetry:
    """Full extraction telemetry for one phase output."""
    phase: str
    subtype: str
    extraction_source: str
    schema_field_count: int
    extracted_count: int
    missing_count: int
    extra_count: int
    fields: list[FieldExtractionRecord] = dataclass_field(default_factory=list)


_FIX_TYPE_RE = re.compile(r"FIX_TYPE:\s*([a-z_]+)", re.IGNORECASE)
_PRINCIPALS_RE = re.compile(r"PRINCIPALS:\s*([^\n]+)", re.IGNORECASE)
_PHASE_RE = re.compile(r"PHASE:\s*([a-z_]+)", re.IGNORECASE)

# Structured field regexes for causal binding (p23)
# Matches "FIELD_NAME: <content>" (same line or next line) until the next ALL_CAPS_FIELD: or end
# Agent may write "ROOT_CAUSE: The issue is..." (same line) or "ROOT_CAUSE:\n  The issue..." (next line)
_STRUCTURED_FIELD_RE = re.compile(
    r"^([A-Z_]{3,}):\s*(.*?)(?=\n[A-Z_]{3,}:|\Z)",
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

    Scans the last 2000 characters where declarations are expected to appear.
    Returns {} if FIX_TYPE is not found (opt-in gate).
    """
    if not agent_output:
        return {}

    tail = agent_output[-2000:]

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
        # Plan-C: skip structured_extract traj entries
        if m.get("extra", {}).get("type", "").startswith("structured_extract_"):
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


# ── Structured output extraction (p221) ────────────────────────────────────────


def extract_from_structured(parsed: dict) -> Declaration:
    """Extract Declaration from a structured (schema-enforced) JSON response.

    When STRUCTURED_OUTPUT_ENABLED=true, the LLM response is guaranteed to
    conform to a phase schema (see phase_schemas.py). This function maps
    the parsed JSON directly to a Declaration — no regex needed.

    Args:
        parsed: Parsed JSON dict from structured LLM output.

    Returns:
        Declaration dict with 'type' and 'principals' keys.
        Returns {} if parsed is empty or missing required fields.
    """
    if not parsed:
        return {}
    fix_type = parsed.get("fix_type", "")
    if not fix_type:
        return {}
    principals = [p.strip().lower() for p in (parsed.get("principals") or []) if p.strip()]
    return {"type": fix_type.strip().lower(), "principals": principals}


def _build_content_preview(parsed: dict, schema_fields: list[str] | None = None) -> str:
    """Build content preview by iterating over schema fields, not hardcoded names."""
    if schema_fields is None:
        schema_fields = list(parsed.keys())
    parts = []
    for field_name in schema_fields:
        val = parsed.get(field_name)
        if val is None or field_name in ("phase", "subtype", "principals"):
            continue
        if isinstance(val, str) and val.strip():
            parts.append(f"{field_name.upper()}: {val}")
        elif isinstance(val, list) and val:
            items = [str(v) for v in val[:3]]
            parts.append(f"{field_name.upper()}: {', '.join(items)}")
        elif isinstance(val, dict) and val:
            import json
            parts.append(f"{field_name.upper()}: {json.dumps(val, ensure_ascii=False)[:200]}")
    if not parts and parsed.get("content"):
        parts.append(str(parsed["content"]))
    return "\n".join(parts)[:500]


# Valid subtype values for validation — derived from SUBTYPE_CONTRACTS (SST2).
# Never hardcode; always derive from the canonical source.
def _get_valid_subtypes() -> set[str]:
    """Derive valid subtypes from canonical source (subtype_contracts)."""
    try:
        from subtype_contracts import SUBTYPE_CONTRACTS
        return set(SUBTYPE_CONTRACTS.keys())
    except ImportError:
        return set()  # SST2: fallback returns empty, not a stale copy


def _classify_repair_strategy(parsed: dict) -> str:
    """Deterministic classifier for repair_strategy_type from ANALYZE fields.

    Fallback for when structured_extract doesn't produce repair_strategy_type.
    Uses root_cause + causal_chain text to classify into REPAIR_STRATEGY_TYPES enum.
    Returns empty string if no confident match (gate will reject).
    """
    text = " ".join([
        (parsed.get("root_cause") or ""),
        (parsed.get("causal_chain") or ""),
    ]).lower()
    if not text.strip():
        return ""

    # Order matters: more specific patterns first
    if any(w in text for w in ("regex", "pattern", "re.compile", "re.match", "re.search", "regular expression")):
        return "REGEX_FIX"
    if any(w in text for w in ("pars", "tokeniz", "split(", "ast.", "grammar", "syntax tree")):
        return "PARSER_REWRITE"
    if any(w in text for w in ("copy()", "deepcopy", "shallow copy", "clone", "copied", "shared reference", "aliased")):
        return "STATE_COPY_FIX"
    if any(w in text for w in ("return value", "return type", "signature", "api contract", "callable", "argument")):
        return "API_CONTRACT_FIX"
    if any(w in text for w in ("propagat", "dataflow", "passed through", "not forwarded", "lost in", "overwritten")):
        return "DATAFLOW_FIX"
    if any(w in text for w in ("invariant", "boundary", "constraint", "must not", "must remain", "assertion")):
        return "INVARIANT_FIX"
    if any(w in text for w in ("secondary", "additional change", "also need", "incomplete fix", "two changes")):
        return "MISSING_SECONDARY_FIX"
    return ""


def build_phase_record_from_structured(
    parsed: dict,
    phase: str,
    from_steps: list[int] | None = None,
):
    """Build a PhaseRecord from structured (bundle-schema-enforced) JSON output.

    Primary constructor for PhaseRecord from bundle-schema-enforced JSON output.
    Bundle schemas output evidence_refs as [string], not [{file, line, observation}].
    ANALYZE bundle uses 'evidence' as [string] (from cognition_contracts).

    Args:
        parsed: Parsed JSON dict from structured LLM output (bundle schema shape).
        phase: Phase name (e.g. 'ANALYZE', 'EXECUTE', 'JUDGE').
        from_steps: Step indices this record derives from (for gate provenance).

    Returns:
        PhaseRecord with fields populated from parsed JSON.
    """
    from phase_record import PhaseRecord

    phase_upper = _PHASE_NORM.get(phase.upper(), phase.upper())

    # Subtype: prefer parsed value, validate against known subtypes, fallback to map
    raw_subtype = (parsed.get("subtype") or "").strip()
    if raw_subtype and raw_subtype in _get_valid_subtypes():
        subtype = raw_subtype
    else:
        subtype = _PHASE_SUBTYPE_MAP.get(phase_upper, "unknown")
        if subtype == "unknown":
            print(f"[declaration_extractor] WARNING: unknown subtype for phase={phase_upper}")

    raw_principals = [p.strip().lower() for p in (parsed.get("principals") or []) if p.strip()]
    # P1.4.c: filter out non-principal entries (descriptions, sentences)
    # Valid principals are short identifiers with underscores, not free-text descriptions
    principals = []
    _invalid_principals = []
    for _rp in raw_principals:
        # Principal names are short (< 40 chars), contain underscores, no spaces typically
        if len(_rp) > 50 or " " in _rp and "_" not in _rp:
            _invalid_principals.append(_rp)
        else:
            principals.append(_rp)
    if _invalid_principals:
        print(
            f"[declaration_extractor] WARNING: invalid principal entries filtered out"
            f" (descriptions instead of principal names): {[s[:60] for s in _invalid_principals]}",
            flush=True,
        )

    # Bundle schema: evidence_refs is [string], ANALYZE uses 'evidence' as [string]
    evidence_refs: list[str] = []
    raw_refs = parsed.get("evidence_refs") or parsed.get("evidence") or []
    for ref in raw_refs:
        if isinstance(ref, str) and ref.strip():
            evidence_refs.append(ref.strip())
        elif isinstance(ref, dict):
            # Legacy fallback: old {file, line, observation} shape
            f = ref.get("file", "")
            line = ref.get("line")
            if f:
                evidence_refs.append(f"{f}:{line}" if line else f)

    claims = [c for c in (parsed.get("claims") or []) if isinstance(c, str) and c.strip()]

    content = _build_content_preview(parsed)

    # P2 fix: synthesize testable_hypothesis from chosen/rationale if missing.
    # Agent often submits DECIDE with {chosen, rationale, options} but omits
    # testable_hypothesis — making prediction_error always return prediction_no_data.
    testable_hypothesis = parsed.get("testable_hypothesis", "")
    if not testable_hypothesis and phase_upper == "DECIDE":
        chosen = parsed.get("chosen", "")
        rationale = parsed.get("rationale", "")
        if chosen and rationale:
            testable_hypothesis = f"If we {chosen}, then tests will pass because {rationale}"[:500]
        elif chosen:
            testable_hypothesis = f"If we {chosen}, then the failing tests will pass"[:500]

    expected_tests = parsed.get("expected_tests_to_pass", [])[:5]

    # ── Extract all named fields with type-appropriate defaults (C-06) ────────

    # OBSERVE
    raw_observations = parsed.get("observations", [])
    observations = raw_observations if isinstance(raw_observations, list) else []

    # ANALYZE
    raw_alt_hyp = parsed.get("alternative_hypotheses", [])
    alternative_hypotheses = raw_alt_hyp if isinstance(raw_alt_hyp, list) else []
    raw_repair_strategy = parsed.get("repair_strategy_type", "")
    repair_strategy_type = raw_repair_strategy.strip() if isinstance(raw_repair_strategy, str) else ""
    # P0.3: deterministic fallback classifier for control-grade field
    if not repair_strategy_type:
        repair_strategy_type = _classify_repair_strategy(parsed)

    # P2: root_cause_location_files — explicit from agent, or fallback from root_cause text
    raw_rcf = parsed.get("root_cause_location_files", [])
    root_cause_location_files = raw_rcf if isinstance(raw_rcf, list) else []
    root_cause_location_files = [f for f in root_cause_location_files if isinstance(f, str) and f.strip()]
    # Deterministic fallback: extract file paths from root_cause if agent didn't declare
    if not root_cause_location_files and phase_upper == "ANALYZE":
        _rc_text = parsed.get("root_cause", "")
        if _rc_text:
            import re
            _file_patterns = re.findall(r'(?:/testbed/)?([a-zA-Z_][\w/]*\.(?:py|js|ts|go|rs|java|c|cpp|h|rb))\b', _rc_text)
            root_cause_location_files = list(dict.fromkeys(_file_patterns))[:5]  # dedupe, max 5

    # DECIDE
    raw_options = parsed.get("options", [])
    options = raw_options if isinstance(raw_options, list) else []
    raw_chosen = parsed.get("chosen", "")
    chosen = raw_chosen.strip() if isinstance(raw_chosen, str) else ""
    raw_rationale = parsed.get("rationale", "")
    rationale = raw_rationale.strip() if isinstance(raw_rationale, str) else ""

    # DESIGN
    raw_files_to_modify = parsed.get("files_to_modify", [])
    files_to_modify = raw_files_to_modify if isinstance(raw_files_to_modify, list) else []
    raw_scope_boundary = parsed.get("scope_boundary", "")
    scope_boundary = raw_scope_boundary.strip() if isinstance(raw_scope_boundary, str) else ""
    raw_invariants = parsed.get("invariants", [])
    invariants = raw_invariants if isinstance(raw_invariants, list) else []
    raw_design_comparison = parsed.get("design_comparison", {})
    design_comparison = raw_design_comparison if isinstance(raw_design_comparison, dict) else {}

    # EXECUTE
    raw_patch_description = parsed.get("patch_description", "")
    patch_description = raw_patch_description.strip() if isinstance(raw_patch_description, str) else ""
    raw_files_modified = parsed.get("files_modified", [])
    files_modified = raw_files_modified if isinstance(raw_files_modified, list) else []

    # JUDGE
    raw_test_results = parsed.get("test_results", {})
    test_results = raw_test_results if isinstance(raw_test_results, dict) else {}
    raw_success_criteria = parsed.get("success_criteria_met", [])
    success_criteria_met = raw_success_criteria if isinstance(raw_success_criteria, list) else []
    raw_residual_risks = parsed.get("residual_risks", [])
    residual_risks = raw_residual_risks if isinstance(raw_residual_risks, list) else []

    return PhaseRecord(
        phase=phase_upper,
        subtype=subtype,
        principals=principals,
        claims=claims,
        evidence_refs=evidence_refs,
        from_steps=from_steps if from_steps is not None else [],
        content=content,
        root_cause=parsed.get("root_cause", ""),
        causal_chain=parsed.get("causal_chain", ""),
        invariant_capture=parsed.get("invariant_capture", {}),
        plan=parsed.get("plan", ""),
        testable_hypothesis=testable_hypothesis,
        expected_tests_to_pass=expected_tests,
        expected_files_to_change=parsed.get("expected_files_to_change", []),
        risk_level=parsed.get("risk_level", ""),
        # C-06: all named fields
        observations=observations,
        alternative_hypotheses=alternative_hypotheses,
        repair_strategy_type=repair_strategy_type,
        root_cause_location_files=root_cause_location_files,
        options=options,
        chosen=chosen,
        rationale=rationale,
        files_to_modify=files_to_modify,
        scope_boundary=scope_boundary,
        invariants=invariants,
        design_comparison=design_comparison,
        patch_description=patch_description,
        files_modified=files_modified,
        test_results=test_results,
        success_criteria_met=success_criteria_met,
        residual_risks=residual_risks,
    )


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
# SST2 fallback: empty dict (returns "unknown" via .get default), not a stale copy.
try:
    from subtype_contracts import _PHASE_TO_SUBTYPE as _PHASE_SUBTYPE_MAP  # type: ignore[assignment]
except Exception:
    _PHASE_SUBTYPE_MAP: dict[str, str] = {}  # SST2: no hardcoded copy; all lookups yield "unknown"

# Phase normalization — derive from canonical_symbols (SST: single definition)
from canonical_symbols import _PHASE_ALIASES as _PHASE_NORM  # noqa: E402


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
    if subtype == "unknown":
        print(f"[declaration_extractor] WARNING: unknown subtype for phase={phase_upper}")
    principals = _extract_principals_from_message(agent_message)
    evidence_refs = _extract_evidence_refs(agent_message)

    structured = extract_structured_fields(agent_message or "")

    # p223: assemble content from structured fields so analysis_gate checkers
    # can find signals beyond the raw [:500] prefix (which is mostly boilerplate).
    content = (agent_message or "")[:500]
    if structured:
        parts: list[str] = []
        for fld in ("root_cause", "causal_chain", "alternatives", "uncertainty", "plan"):
            val = structured.get(fld, "")
            if val:
                parts.append(f"{fld.upper()}: {val}")
        if parts:
            content = "\n".join(parts)

    return PhaseRecord(
        phase=phase_upper or phase,
        subtype=subtype,
        principals=principals,
        claims=[],
        evidence_refs=evidence_refs,
        from_steps=from_steps if from_steps is not None else [],
        content=content,
        root_cause=structured.get("root_cause", ""),
        causal_chain=structured.get("causal_chain", ""),
        invariant_capture=structured.get("invariant_capture", {}),
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
    if subtype == "unknown":
        print(f"[declaration_extractor] WARNING: unknown subtype for phase={target_upper}")

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

    # p223: build content from structured fields so analysis_gate checkers
    # (_check_alternative_hypothesis, _check_invariant_capture) can find
    # signals.  The raw [:500] prefix is mostly PHASE/PRINCIPALS boilerplate;
    # the substantive reasoning (ALTERNATIVES, CAUSAL_CHAIN, UNCERTAINTY) is
    # often beyond char 500.  Assemble content from extracted fields first,
    # fall back to raw prefix when no fields were found.
    if not foreign_phase_declared and structured:
        parts: list[str] = []
        for fld in ("root_cause", "causal_chain", "alternatives", "uncertainty", "plan"):
            val = structured.get(fld, "")
            if val:
                parts.append(f"{fld.upper()}: {val}")
        content = "\n".join(parts) if parts else content
    # If no structured fields extracted, keep the raw [:500] fallback (content
    # already set above).

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
        invariant_capture=structured.get("invariant_capture", {}),
        plan=structured.get("plan", ""),
    )
    return record, declared_phase, foreign_phase_declared


# ── Unified extraction entry point (C-05) ────────────────────────────────────


def _classify_value(val) -> str:
    """Classify a PhaseRecord field value type for telemetry."""
    if isinstance(val, str):
        return "str" if val else "empty"
    if isinstance(val, list):
        return "list" if val else "empty"
    if isinstance(val, dict):
        return "dict" if val else "empty"
    return "empty"


def _is_non_empty(val) -> bool:
    """Check if a PhaseRecord field value is non-empty."""
    if isinstance(val, str):
        return bool(val.strip())
    if isinstance(val, (list, dict)):
        return bool(val)
    return False


def _compute_extraction_meta(
    record,
    schema_fields: list[str],
    source: str,
) -> ExtractionMeta:
    """Compute ExtractionMeta by comparing PhaseRecord fields against schema_fields.

    Args:
        record: PhaseRecord instance.
        schema_fields: Field names defined in the bundle schema for this phase.
        source: Extraction source identifier.

    Returns:
        ExtractionMeta with fields_extracted, fields_missing, fields_extra populated.
    """
    # Fields that are PhaseRecord metadata, not phase content fields
    _META_FIELDS = {"phase", "subtype", "principals", "claims", "evidence_refs", "from_steps", "content"}

    fields_extracted: list[str] = []
    fields_missing: list[str] = []

    for f in schema_fields:
        val = getattr(record, f, None)
        if val is not None and _is_non_empty(val):
            fields_extracted.append(f)
        else:
            fields_missing.append(f)

    # fields_extra: populated PhaseRecord content fields NOT in schema_fields
    schema_set = set(schema_fields)
    fields_extra: list[str] = []
    for f in vars(record):
        if f.startswith("_") or f in _META_FIELDS:
            continue
        if f in schema_set:
            continue
        val = getattr(record, f, None)
        if val is not None and _is_non_empty(val):
            fields_extra.append(f)

    return ExtractionMeta(
        source=source,
        fields_in_schema=list(schema_fields),
        fields_extracted=fields_extracted,
        fields_missing=fields_missing,
        fields_extra=fields_extra,
    )


def extract_phase_output(
    *,
    tool_submitted: dict | None,
    structured_parsed: dict | None,
    agent_message: str,
    phase: str,
    schema_fields: list[str],
    from_steps: list[int] | None = None,
) -> tuple:
    """Unified extraction entry point: try tool_submitted > structured_parsed > regex fallback.

    Priority:
    1. tool_submitted (agent used a tool to submit structured data)
    2. structured_parsed (structured_extract API call result)
    3. agent_message regex fallback

    Args:
        tool_submitted: Dict from agent tool submission, or None.
        structured_parsed: Dict from structured extraction API, or None.
        agent_message: Raw agent message text (for regex fallback).
        phase: Phase name (e.g. 'ANALYZE').
        schema_fields: Field names from bundle schema for this phase.
        from_steps: Step indices for provenance.

    Returns:
        Tuple of (PhaseRecord, ExtractionMeta).
    """
    if tool_submitted is not None:
        record = build_phase_record_from_structured(tool_submitted, phase, from_steps)
        source = "tool_submitted"
    elif structured_parsed is not None:
        record = build_phase_record_from_structured(structured_parsed, phase, from_steps)
        source = "structured_extract"
    else:
        record, _declared, _foreign = extract_record_for_phase(
            agent_message, phase, from_steps
        )
        source = "regex_fallback"

    meta = _compute_extraction_meta(record, schema_fields, source)
    return record, meta
