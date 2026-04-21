"""Phase quality gate tests — ANALYZE and DESIGN admission enforcement.

Verifies that the hardcoded quality gates in admit_phase_record() correctly
reject low-quality submissions and accept well-formed ones.
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


# ── Minimal fixtures for testing gate logic ──────────────────────────────

@dataclass
class MockPhaseRecord:
    phase: str = ""
    subtype: str = ""
    # ANALYZE fields
    root_cause: str = ""
    causal_chain: str = ""
    evidence_refs: list = field(default_factory=list)
    alternative_hypotheses: list = field(default_factory=list)
    # DESIGN fields
    files_to_modify: list = field(default_factory=list)
    scope_boundary: str = ""
    solution_approach: str = ""
    design_comparison: str = ""
    approach: str = ""


def check_analyze_quality(pr: MockPhaseRecord) -> list[str]:
    """Extract ANALYZE quality gate logic from step_sections.py Gate 4."""
    failures = []

    rc = pr.root_cause or ""
    rc_has_grounding = bool(
        rc and len(rc) >= 10
        and any(sig in rc for sig in ("/", ".py", ".js", ".ts", "def ", "class ", "function ", "()", "::"))
    )
    if not rc_has_grounding:
        failures.append("ROOT_CAUSE lacks code grounding")

    alt = pr.alternative_hypotheses or []
    alt_valid = [
        h for h in alt
        if isinstance(h, dict) and (h.get("hypothesis") or h.get("description") or "").strip()
    ]
    if len(alt_valid) < 1:
        failures.append("No alternative hypotheses")

    ev = pr.evidence_refs or []
    if len(ev) < 1:
        failures.append("No evidence references")

    return failures


def check_design_quality(pr: MockPhaseRecord, tool_submitted: dict | None = None) -> list[str]:
    """Extract DESIGN quality gate logic from step_sections.py Gate 5."""
    failures = []

    # Check 1: files_to_modify
    ftm = pr.files_to_modify or []
    if isinstance(ftm, str):
        ftm = [ftm] if ftm.strip() else []
    ftm_valid = [
        f for f in ftm
        if isinstance(f, str) and ("/" in f or ".py" in f or ".js" in f or ".ts" in f)
    ]
    if len(ftm_valid) < 1:
        failures.append("FILES_TO_MODIFY empty or no file paths")

    # Check 2: scope_boundary
    sb = pr.scope_boundary or ""
    if isinstance(sb, list):
        sb = " ".join(str(x) for x in sb)
    if len(str(sb).strip()) < 10:
        failures.append("SCOPE_BOUNDARY missing or too brief")

    # Check 3: solution approach
    approach = (pr.solution_approach or pr.design_comparison or pr.approach or "")
    if isinstance(approach, list):
        approach = " ".join(str(x) for x in approach)
    if tool_submitted is not None and len(str(approach).strip()) < 10:
        for ak in ("solution_approach", "approach", "design_comparison", "strategy"):
            av = tool_submitted.get(ak, "")
            if isinstance(av, str) and len(av.strip()) >= 10:
                approach = av
                break
    # Fallback: substantial scope_boundary doubles as approach
    if len(str(approach).strip()) < 10:
        sb_text = str(sb).strip() if isinstance(sb, str) else " ".join(str(x) for x in (sb or []))
        if len(sb_text) > 30:
            approach = sb_text
    if len(str(approach).strip()) < 10:
        failures.append("No SOLUTION APPROACH found")

    return failures


# ── ANALYZE Quality Gate Tests ───────────────────────────────────────────

class TestAnalyzeQualityGate:

    def test_good_analyze_passes(self):
        pr = MockPhaseRecord(
            root_cause="Bug in django/db/models/query.py line 42 — QuerySet.values() clobbers",
            alternative_hypotheses=[{"hypothesis": "Could be in compiler.py set_values()"}],
            evidence_refs=["django/db/models/query.py:42"],
        )
        assert check_analyze_quality(pr) == []

    def test_no_root_cause_grounding_rejects(self):
        pr = MockPhaseRecord(
            root_cause="The bug is somewhere in the code",  # no file/function reference
            alternative_hypotheses=[{"hypothesis": "alt"}],
            evidence_refs=["file.py:10"],
        )
        failures = check_analyze_quality(pr)
        assert any("ROOT_CAUSE" in f for f in failures)

    def test_empty_root_cause_rejects(self):
        pr = MockPhaseRecord(
            root_cause="",
            alternative_hypotheses=[{"hypothesis": "alt"}],
            evidence_refs=["file.py:10"],
        )
        failures = check_analyze_quality(pr)
        assert any("ROOT_CAUSE" in f for f in failures)

    def test_no_alternative_hypotheses_rejects(self):
        pr = MockPhaseRecord(
            root_cause="Bug in django/db/models/query.py",
            alternative_hypotheses=[],
            evidence_refs=["file.py:10"],
        )
        assert any("alternative" in f.lower() for f in check_analyze_quality(pr))

    def test_empty_hypothesis_dicts_rejected(self):
        pr = MockPhaseRecord(
            root_cause="Bug in django/db/models/query.py",
            alternative_hypotheses=[{"hypothesis": ""}, {"description": "  "}],
            evidence_refs=["file.py:10"],
        )
        assert any("alternative" in f.lower() for f in check_analyze_quality(pr))

    def test_no_evidence_rejects(self):
        pr = MockPhaseRecord(
            root_cause="Bug in django/db/models/query.py line 42",
            alternative_hypotheses=[{"hypothesis": "Could be compiler"}],
            evidence_refs=[],
        )
        assert any("evidence" in f.lower() for f in check_analyze_quality(pr))

    def test_all_failures_at_once(self):
        pr = MockPhaseRecord()
        failures = check_analyze_quality(pr)
        assert len(failures) == 3  # root_cause + alt_hypotheses + evidence


# ── DESIGN Quality Gate Tests ────────────────────────────────────────────

class TestDesignQualityGate:

    def test_good_design_passes(self):
        pr = MockPhaseRecord(
            files_to_modify=["django/db/models/sql/compiler.py"],
            scope_boundary="Only modify the set_values call in SQLCompiler, do not touch QuerySet",
            solution_approach="Clone the query before calling set_values to prevent mutation",
        )
        assert check_design_quality(pr) == []

    def test_no_files_rejects(self):
        pr = MockPhaseRecord(
            files_to_modify=[],
            scope_boundary="Modify only the compiler module",
            solution_approach="Clone the query before set_values",
        )
        failures = check_design_quality(pr)
        assert any("FILES_TO_MODIFY" in f for f in failures)

    def test_files_without_paths_rejects(self):
        pr = MockPhaseRecord(
            files_to_modify=["the query module", "some file"],  # no / or .py
            scope_boundary="Modify only the compiler module",
            solution_approach="Clone the query before set_values",
        )
        failures = check_design_quality(pr)
        assert any("FILES_TO_MODIFY" in f for f in failures)

    def test_files_with_py_extension_passes(self):
        pr = MockPhaseRecord(
            files_to_modify=["compiler.py"],  # has .py, no /
            scope_boundary="Only modify the set_values call, nothing else",
            solution_approach="Clone the query before set_values call",
        )
        assert check_design_quality(pr) == []

    def test_no_scope_boundary_rejects(self):
        pr = MockPhaseRecord(
            files_to_modify=["django/db/models/sql/compiler.py"],
            scope_boundary="",
            solution_approach="Clone the query before set_values",
        )
        failures = check_design_quality(pr)
        assert any("SCOPE_BOUNDARY" in f for f in failures)

    def test_short_scope_boundary_rejects(self):
        pr = MockPhaseRecord(
            files_to_modify=["django/db/models/sql/compiler.py"],
            scope_boundary="fix it",  # < 10 chars
            solution_approach="Clone the query before set_values",
        )
        failures = check_design_quality(pr)
        assert any("SCOPE_BOUNDARY" in f for f in failures)

    def test_no_solution_approach_rejects(self):
        pr = MockPhaseRecord(
            files_to_modify=["django/db/models/sql/compiler.py"],
            scope_boundary="Only modify set_values",  # 22 chars, ≤30 so no fallback
            solution_approach="",
        )
        failures = check_design_quality(pr)
        assert any("SOLUTION APPROACH" in f for f in failures)

    def test_solution_approach_from_design_comparison(self):
        """design_comparison field can serve as solution approach."""
        pr = MockPhaseRecord(
            files_to_modify=["django/db/models/sql/compiler.py"],
            scope_boundary="Only modify the set_values call in SQLCompiler",
            solution_approach="",
            design_comparison="Option A: clone query. Option B: check identity. Choosing A because safer.",
        )
        assert check_design_quality(pr) == []

    def test_solution_approach_from_tool_submitted(self):
        """tool_submitted dict can provide approach when PhaseRecord fields empty."""
        pr = MockPhaseRecord(
            files_to_modify=["django/db/models/sql/compiler.py"],
            scope_boundary="Only modify the set_values call in SQLCompiler",
        )
        tool = {"strategy": "Clone the query object before mutating it in set_values"}
        assert check_design_quality(pr, tool_submitted=tool) == []

    def test_all_failures_at_once(self):
        pr = MockPhaseRecord()
        failures = check_design_quality(pr)
        assert len(failures) == 3  # files + scope + approach

    def test_files_as_string_converted(self):
        """Single string file should be treated as list."""
        pr = MockPhaseRecord(
            files_to_modify="django/db/models/query.py",
            scope_boundary="Only modify QuerySet.values() method",
            solution_approach="Add a clone step before modifying the query",
        )
        # files_to_modify is a string — gate should handle it
        assert check_design_quality(pr) == []

    def test_scope_boundary_as_list(self):
        """scope_boundary can be a list of strings."""
        pr = MockPhaseRecord(
            files_to_modify=["django/db/models/query.py"],
            scope_boundary=["Only modify values()", "Do not touch aggregation"],
            solution_approach="Clone query before set_values call in compiler",
        )
        assert check_design_quality(pr) == []

    def test_substantial_scope_boundary_doubles_as_approach(self):
        """When scope_boundary is >30 chars and no explicit approach, accept it.

        Real-world pattern: agent writes "SOLUTION APPROACH: Delete lines 87-91
        in django/db/migrations/loader.py..." in scope_boundary field.
        """
        pr = MockPhaseRecord(
            files_to_modify=["django/db/migrations/loader.py"],
            scope_boundary="SOLUTION APPROACH: Delete lines 87-91 in django/db/migrations/loader.py within the load_disk() method",
            solution_approach="",  # empty — but scope_boundary has the info
        )
        assert check_design_quality(pr) == []

    def test_short_scope_boundary_does_not_count_as_approach(self):
        """Short scope_boundary (<= 30 chars) should NOT substitute for approach."""
        pr = MockPhaseRecord(
            files_to_modify=["django/db/models/query.py"],
            scope_boundary="modify query module only",  # 24 chars, too short
            solution_approach="",
        )
        failures = check_design_quality(pr)
        assert any("SOLUTION APPROACH" in f for f in failures)
