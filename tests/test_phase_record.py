"""
test_phase_record.py — Unit tests for p190 per-phase record extraction.

Verifies:
- PhaseRecord dataclass fields are correct and serializable
- extract_phase_record() returns correct subtype for each phase
- extract_phase_record() extracts principals from agent message
- extract_phase_record() extracts evidence refs from agent message
- extract_phase_record() truncates content to 500 chars
- extract_phase_record() handles empty input gracefully
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest
from phase_record import PhaseRecord
from declaration_extractor import extract_phase_record, _extract_principals_from_message, _extract_evidence_refs


# ── Tests: PhaseRecord dataclass ──────────────────────────────────────────────

def test_phase_record_dataclass_fields():
    """PhaseRecord has all required fields and can be constructed."""
    rec = PhaseRecord(
        phase="ANALYZE",
        subtype="root_cause_analysis",
        principals=["causality", "evidence_based"],
        claims=[],
        evidence_refs=["django/db/models.py:45"],
        from_steps=[],
        content="Agent analyzed the root cause",
    )
    assert rec.phase == "ANALYZE"
    assert rec.subtype == "root_cause_analysis"
    assert rec.principals == ["causality", "evidence_based"]
    assert rec.claims == []
    assert rec.evidence_refs == ["django/db/models.py:45"]
    assert rec.from_steps == []
    assert "analyzed" in rec.content


def test_phase_record_as_dict_serializable():
    """PhaseRecord.as_dict() returns a plain dict serializable to JSON."""
    import json
    rec = PhaseRecord(
        phase="EXECUTE",
        subtype="patch_writing",
        principals=["scope_control"],
        claims=[],
        evidence_refs=[],
        from_steps=[],
        content="x" * 600,  # longer than 100 chars to test truncation
    )
    d = rec.as_dict()
    # Must be JSON-serializable
    json.dumps(d)
    # content_preview must be truncated to 100 chars
    assert len(d["content_preview"]) <= 100
    assert d["phase"] == "EXECUTE"
    assert d["subtype"] == "patch_writing"
    assert d["principals"] == ["scope_control"]


# ── Tests: extract_phase_record() subtype mapping ─────────────────────────────

def test_extract_phase_record_observe():
    """OBSERVE phase -> subtype=observation."""
    rec = extract_phase_record("I will read the codebase", "OBSERVE")
    assert rec.phase == "OBSERVE"
    assert rec.subtype == "observation"


def test_extract_phase_record_analyze():
    """ANALYZE phase -> subtype=root_cause_analysis."""
    rec = extract_phase_record("The root cause is in models.py:45", "ANALYZE")
    assert rec.phase == "ANALYZE"
    assert rec.subtype == "root_cause_analysis"


def test_extract_phase_record_execute():
    """EXECUTE phase -> subtype=patch_writing."""
    rec = extract_phase_record("Applying minimal change to fix.py", "EXECUTE")
    assert rec.phase == "EXECUTE"
    assert rec.subtype == "patch_writing"


def test_extract_phase_record_judge():
    """JUDGE phase -> subtype=verification."""
    rec = extract_phase_record("Verified: tests pass", "JUDGE")
    assert rec.phase == "JUDGE"
    assert rec.subtype == "verification"


def test_extract_phase_record_unknown_phase():
    """Unknown phase -> subtype=unknown (safe fallback)."""
    rec = extract_phase_record("some output", "UNKNOWN_PHASE")
    assert rec.subtype == "unknown"


# ── Tests: principals extraction ──────────────────────────────────────────────

def test_extract_principals_from_message_present():
    """PRINCIPALS: line is extracted from agent message."""
    msg = "I reviewed the code.\nPRINCIPALS: causality evidence_based minimal_change\nDone."
    principals = _extract_principals_from_message(msg)
    assert "causality" in principals
    assert "evidence_based" in principals
    assert "minimal_change" in principals


def test_extract_principals_from_message_empty():
    """No PRINCIPALS: in message -> returns []."""
    principals = _extract_principals_from_message("No declaration here")
    assert principals == []


def test_extract_principals_from_message_empty_input():
    """Empty string input -> returns []."""
    assert _extract_principals_from_message("") == []


def test_extract_phase_record_with_principals():
    """extract_phase_record picks up PRINCIPALS: from message."""
    msg = (
        "Found the root cause in models.py:45\n"
        "PRINCIPALS: causality scope_control\n"
        "Will fix the logic."
    )
    rec = extract_phase_record(msg, "ANALYZE")
    assert "causality" in rec.principals
    assert "scope_control" in rec.principals


# ── Tests: evidence_refs extraction ──────────────────────────────────────────

def test_extract_evidence_refs_file_line():
    """file.py:lineno patterns are extracted."""
    msg = "The bug is at django/db/models.py:123 and tests/test_foo.py:45"
    refs = _extract_evidence_refs(msg)
    assert any("models.py" in r for r in refs)


def test_extract_evidence_refs_no_refs():
    """No file patterns -> returns []."""
    refs = _extract_evidence_refs("plain text without any file references here")
    assert refs == []


def test_extract_evidence_refs_empty():
    """Empty string -> returns []."""
    assert _extract_evidence_refs("") == []


# ── Tests: content truncation ─────────────────────────────────────────────────

def test_extract_phase_record_content_truncated():
    """content field is truncated to 500 chars."""
    long_msg = "A" * 1000
    rec = extract_phase_record(long_msg, "OBSERVE")
    assert len(rec.content) == 500


def test_extract_phase_record_empty_message():
    """Empty agent message -> returns PhaseRecord with empty principals and refs."""
    rec = extract_phase_record("", "ANALYZE")
    assert rec.phase == "ANALYZE"
    assert rec.subtype == "root_cause_analysis"
    assert rec.principals == []
    assert rec.evidence_refs == []
    assert rec.content == ""
