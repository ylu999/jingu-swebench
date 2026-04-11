"""
Tests for build_phase_record_from_structured() and _build_content_preview().

Covers all 6 phases with bundle schema field shapes:
  - evidence_refs as list[str] (bundle base schema)
  - evidence as list[str] (ANALYZE cognition_contracts schema)
  - subtype validation with fallback
  - content preview generation per phase
"""

import sys
import os

# Add scripts dir to path so imports resolve
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from declaration_extractor import build_phase_record_from_structured, _build_content_preview


class TestBuildPhaseRecordAnalyze:
    """ANALYZE phase: bundle uses 'evidence' as list[str]."""

    def test_evidence_as_string_list(self):
        parsed = {
            "phase": "analyze",
            "subtype": "analysis.root_cause",
            "principals": ["causal_grounding", "evidence_linkage"],
            "evidence": ["django/db/models.py:45", "tests/test_foo.py:12"],
            "root_cause": "The field validation skips null checks",
            "causal_chain": "test calls save() -> validate() -> missing null check",
            "claims": ["null values are not validated"],
        }
        pr = build_phase_record_from_structured(parsed, "ANALYZE")
        assert pr.phase == "ANALYZE"
        assert pr.subtype == "analysis.root_cause"
        assert pr.evidence_refs == ["django/db/models.py:45", "tests/test_foo.py:12"]
        assert pr.principals == ["causal_grounding", "evidence_linkage"]
        assert pr.root_cause == "The field validation skips null checks"
        assert pr.causal_chain == "test calls save() -> validate() -> missing null check"
        assert pr.claims == ["null values are not validated"]

    def test_evidence_refs_field_also_works(self):
        """evidence_refs field (base schema) should also work for ANALYZE."""
        parsed = {
            "phase": "analyze",
            "subtype": "analysis.root_cause",
            "principals": ["causal_grounding"],
            "evidence_refs": ["models.py:10"],
            "root_cause": "Missing check",
        }
        pr = build_phase_record_from_structured(parsed, "ANALYZE")
        assert pr.evidence_refs == ["models.py:10"]

    def test_legacy_evidence_objects_fallback(self):
        """Old {file, line, observation} evidence objects should still work."""
        parsed = {
            "phase": "analyze",
            "principals": ["causal_grounding"],
            "evidence": [
                {"file": "django/db/models.py", "line": 45, "observation": "null check missing"},
            ],
            "root_cause": "null check missing",
        }
        pr = build_phase_record_from_structured(parsed, "ANALYZE")
        assert pr.evidence_refs == ["django/db/models.py:45"]

    def test_content_preview_analyze(self):
        parsed = {
            "root_cause": "The validation is wrong",
            "alternatives_considered": [
                {"hypothesis": "caching issue", "why_rejected": "no cache involved"},
            ],
            "principals": [],
        }
        pr = build_phase_record_from_structured(parsed, "ANALYZE")
        assert "ROOT_CAUSE:" in pr.content
        assert "HYPOTHESIS: caching issue" in pr.content
        assert "RULED_OUT: no cache involved" in pr.content


class TestBuildPhaseRecordExecute:
    """EXECUTE phase: bundle uses evidence_refs as list[str]."""

    def test_basic_execute(self):
        parsed = {
            "phase": "execute",
            "subtype": "execution.code_patch",
            "principals": ["minimal_change", "action_grounding"],
            "evidence_refs": ["django/db/models.py:45"],
            "plan": "Add null check before save",
            "patch_description": "Add if value is None check",
            "change_scope": ["django/db/models.py"],
        }
        pr = build_phase_record_from_structured(parsed, "EXECUTE")
        assert pr.phase == "EXECUTE"
        assert pr.subtype == "execution.code_patch"
        assert pr.evidence_refs == ["django/db/models.py:45"]
        assert pr.plan == "Add null check before save"

    def test_content_preview_execute(self):
        parsed = {
            "plan": "Add null validation",
            "patch_description": "Insert None check in save()",
            "change_scope": ["models.py", "fields.py"],
            "principals": [],
        }
        pr = build_phase_record_from_structured(parsed, "EXECUTE")
        assert "PATCH:" in pr.content
        assert "FILES:" in pr.content


class TestBuildPhaseRecordJudge:
    """JUDGE phase."""

    def test_basic_judge(self):
        parsed = {
            "phase": "judge",
            "subtype": "judge.verification",
            "principals": ["result_verification"],
            "evidence_refs": ["tests/test_null.py:5"],
            "verification_result": "pass",
            "confidence": 0.95,
        }
        pr = build_phase_record_from_structured(parsed, "JUDGE")
        assert pr.phase == "JUDGE"
        assert pr.subtype == "judge.verification"
        assert pr.evidence_refs == ["tests/test_null.py:5"]

    def test_content_preview_judge(self):
        parsed = {
            "verification_result": "pass",
            "confidence": 0.9,
            "remaining_risks": ["edge case with empty string"],
            "principals": [],
        }
        pr = build_phase_record_from_structured(parsed, "JUDGE")
        assert "TESTS_PASSED: pass" in pr.content
        assert "CRITERION:" in pr.content


class TestBuildPhaseRecordObserve:
    """OBSERVE phase."""

    def test_basic_observe(self):
        parsed = {
            "phase": "observe",
            "subtype": "observation.fact_gathering",
            "principals": ["ontology_alignment"],
            "evidence_refs": ["django/db/models.py"],
            "content": "Observed the model field structure",
        }
        pr = build_phase_record_from_structured(parsed, "OBSERVE")
        assert pr.phase == "OBSERVE"
        assert pr.subtype == "observation.fact_gathering"
        assert pr.evidence_refs == ["django/db/models.py"]

    def test_content_preview_observe_list(self):
        parsed = {
            "observations": ["Found field class", "Read test file", "Checked migration"],
            "principals": [],
        }
        pr = build_phase_record_from_structured(parsed, "OBSERVE")
        assert pr.content.count("OBS:") == 3

    def test_content_preview_observe_string(self):
        parsed = {
            "observations": "Found the relevant field class",
            "principals": [],
        }
        pr = build_phase_record_from_structured(parsed, "OBSERVE")
        assert "OBS: Found the relevant field class" in pr.content


class TestBuildPhaseRecordDecide:
    """DECIDE phase."""

    def test_basic_decide(self):
        parsed = {
            "phase": "decide",
            "subtype": "decision.fix_direction",
            "principals": ["option_comparison"],
            "evidence_refs": [],
            "content": "Chose approach A over B",
            "chosen": "approach A",
            "rationale": "simpler and safer",
        }
        pr = build_phase_record_from_structured(parsed, "DECIDE")
        assert pr.phase == "DECIDE"
        assert pr.subtype == "decision.fix_direction"

    def test_content_preview_decide(self):
        parsed = {
            "chosen": "approach A",
            "rationale": "simpler and safer",
            "principals": [],
        }
        pr = build_phase_record_from_structured(parsed, "DECIDE")
        assert "CHOSEN: approach A" in pr.content
        assert "RATIONALE: simpler and safer" in pr.content


class TestBuildPhaseRecordDesign:
    """DESIGN phase."""

    def test_basic_design(self):
        parsed = {
            "phase": "design",
            "subtype": "design.solution_shape",
            "principals": ["scope_minimality"],
            "evidence_refs": ["models.py"],
            "content": "Solution scope: modify field validation",
        }
        pr = build_phase_record_from_structured(parsed, "DESIGN")
        assert pr.phase == "DESIGN"
        assert pr.subtype == "design.solution_shape"
        assert pr.evidence_refs == ["models.py"]

    def test_content_preview_design(self):
        parsed = {
            "scope": "field validation module",
            "change_scope": ["models.py", "fields.py"],
            "principals": [],
        }
        pr = build_phase_record_from_structured(parsed, "DESIGN")
        assert "SCOPE:" in pr.content
        assert "FILES:" in pr.content


class TestSubtypeValidation:
    """Subtype validation: parsed value must be in valid set, else fallback."""

    def test_valid_subtype_from_parsed(self):
        parsed = {
            "subtype": "analysis.root_cause",
            "principals": [],
        }
        pr = build_phase_record_from_structured(parsed, "ANALYZE")
        assert pr.subtype == "analysis.root_cause"

    def test_invalid_subtype_falls_back_to_map(self):
        parsed = {
            "subtype": "bogus.invalid",
            "principals": [],
        }
        pr = build_phase_record_from_structured(parsed, "EXECUTE")
        assert pr.subtype == "execution.code_patch"

    def test_missing_subtype_falls_back_to_map(self):
        parsed = {
            "principals": [],
        }
        pr = build_phase_record_from_structured(parsed, "JUDGE")
        assert pr.subtype == "judge.verification"

    def test_empty_subtype_falls_back_to_map(self):
        parsed = {
            "subtype": "",
            "principals": [],
        }
        pr = build_phase_record_from_structured(parsed, "OBSERVE")
        assert pr.subtype == "observation.fact_gathering"


class TestPhaseNormalization:
    """Phase name normalization (gerund variants)."""

    def test_execution_normalizes_to_execute(self):
        parsed = {"principals": [], "evidence_refs": ["a.py"]}
        pr = build_phase_record_from_structured(parsed, "EXECUTION")
        assert pr.phase == "EXECUTE"

    def test_observation_normalizes_to_observe(self):
        parsed = {"principals": []}
        pr = build_phase_record_from_structured(parsed, "OBSERVATION")
        assert pr.phase == "OBSERVE"

    def test_analysis_normalizes_to_analyze(self):
        parsed = {"principals": []}
        pr = build_phase_record_from_structured(parsed, "ANALYSIS")
        assert pr.phase == "ANALYZE"


class TestEvidenceRefsEdgeCases:
    """Edge cases for evidence_refs extraction."""

    def test_empty_strings_filtered(self):
        parsed = {
            "principals": [],
            "evidence_refs": ["a.py", "", "  ", "b.py"],
        }
        pr = build_phase_record_from_structured(parsed, "EXECUTE")
        assert pr.evidence_refs == ["a.py", "b.py"]

    def test_none_evidence_refs(self):
        parsed = {
            "principals": [],
            "evidence_refs": None,
        }
        pr = build_phase_record_from_structured(parsed, "EXECUTE")
        assert pr.evidence_refs == []

    def test_no_evidence_fields_at_all(self):
        parsed = {"principals": []}
        pr = build_phase_record_from_structured(parsed, "EXECUTE")
        assert pr.evidence_refs == []


class TestFromSteps:
    """from_steps parameter handling."""

    def test_from_steps_passed_through(self):
        parsed = {"principals": []}
        pr = build_phase_record_from_structured(parsed, "ANALYZE", from_steps=[1, 3, 5])
        assert pr.from_steps == [1, 3, 5]

    def test_from_steps_defaults_to_empty(self):
        parsed = {"principals": []}
        pr = build_phase_record_from_structured(parsed, "ANALYZE")
        assert pr.from_steps == []


class TestContentPreviewTruncation:
    """Content preview is truncated to 500 chars."""

    def test_long_content_truncated(self):
        parsed = {
            "root_cause": "x" * 600,
            "principals": [],
        }
        pr = build_phase_record_from_structured(parsed, "ANALYZE")
        assert len(pr.content) <= 500

    def test_fallback_to_content_field(self):
        """When no phase-specific fields, use 'content' field."""
        parsed = {
            "principals": [],
            "content": "Some generic content here",
        }
        pr = build_phase_record_from_structured(parsed, "EXECUTE")
        assert "Some generic content here" in pr.content
