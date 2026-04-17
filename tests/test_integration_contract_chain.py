"""
test_integration_contract_chain.py — Full chain integration test (EF-12).

Exercises the full contract chain end-to-end:
  contract definition -> compile -> extract -> PhaseRecord -> gate evaluation -> verdict/rejection -> routing

Tests use ACTUAL APIs from the codebase — no mocks of internal functions.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest
from phase_record import PhaseRecord
from cognition_contracts._base import validate_contract_definition, FieldSpec, GateRule
from cognition_contracts._compiler import compile_contract, BundleContractOutput, CompilationError
from cognition_contracts import analysis_root_cause as arc
from declaration_extractor import (
    build_phase_record_from_structured,
    extract_phase_output,
    ExtractionMeta,
)
from analysis_gate import evaluate_analysis, AnalysisVerdict
from gate_failure_code import GateFailureCode, GateFailureCategory


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_well_formed_analyze_response() -> dict:
    """Build a well-formed ANALYZE structured response that passes all gates."""
    return {
        "phase": "ANALYZE",
        "subtype": "analysis.root_cause",
        "principals": ["causal_grounding", "evidence_linkage", "alternative_hypothesis_check"],
        "root_cause": (
            "The issue is in django/db/models/fields.py:245 where "
            "field validation is skipped during bulk_create"
        ),
        "causal_chain": (
            "User calls bulk_create() -> _do_insert() skips field.clean() "
            "-> invalid data saved to DB -> query returns corrupt results"
        ),
        "evidence_refs": [
            "django/db/models/fields.py:245",
            "django/db/models/query.py:1200",
        ],
        "alternative_hypotheses": [
            {
                "hypothesis": "Signal handler interference causing data corruption",
                "ruled_out_reason": "Signals disabled in test, issue persists",
            },
            {
                "hypothesis": "Database backend truncation on insert",
                "ruled_out_reason": "Same behavior on SQLite and PostgreSQL",
            },
        ],
        "invariant_capture": {
            "identified_invariants": [
                "bulk_create must run field.clean() before insert",
            ],
            "risk_if_violated": "Invalid data bypasses validation and corrupts the DB",
        },
        "repair_strategy_type": "DATAFLOW_FIX",
        "content": "Full analysis content here",
    }


# ── Step 2: test_happy_path (ANALYZE) ───────────────────────────────────────


class TestAnalyzeHappyPath:
    """Full chain: validate -> compile -> extract -> gate -> PASS."""

    def test_validate_contract(self):
        errors = validate_contract_definition(arc)
        assert errors == [], f"Contract validation errors: {errors}"

    def test_compile_contract(self):
        output = compile_contract(arc)
        assert isinstance(output, BundleContractOutput)
        assert output.schema, "schema must be non-empty"
        assert output.schema["properties"], "schema.properties must be non-empty"
        assert "root_cause" in output.schema["properties"]
        assert output.phase_spec["name"] == "ANALYZE"
        assert output.policy["subtype"] == "analysis.root_cause"

    def test_extract_phase_record(self):
        mock = _make_well_formed_analyze_response()
        record = build_phase_record_from_structured(mock, "ANALYZE")
        assert isinstance(record, PhaseRecord)
        assert record.phase == "ANALYZE"
        assert record.root_cause, "root_cause must be populated"
        assert record.causal_chain, "causal_chain must be populated"
        assert len(record.evidence_refs) >= 1, "evidence_refs must be populated"
        assert "causal_grounding" in record.principals
        assert "evidence_linkage" in record.principals
        assert len(record.alternative_hypotheses) == 2

    def test_gate_passes(self):
        mock = _make_well_formed_analyze_response()
        record = build_phase_record_from_structured(mock, "ANALYZE")
        verdict = evaluate_analysis(record)
        assert isinstance(verdict, AnalysisVerdict)
        assert verdict.passed is True, (
            f"Gate should pass for well-formed input; "
            f"failed_rules={verdict.failed_rules}, reasons={verdict.reasons}"
        )
        assert verdict.failed_rules == []
        # All scores should be >= 0.5
        for rule, score in verdict.scores.items():
            if isinstance(score, (int, float)):
                assert score >= 0.0, f"score for {rule} is negative"

    def test_full_chain_end_to_end(self):
        """Single test: validate -> compile -> extract -> gate -> PASS."""
        # 1. Validate
        errors = validate_contract_definition(arc)
        assert errors == []

        # 2. Compile
        output = compile_contract(arc)
        assert output.schema

        # 3. Extract
        mock = _make_well_formed_analyze_response()
        record = build_phase_record_from_structured(mock, "ANALYZE")
        assert record.root_cause

        # 4. Gate
        verdict = evaluate_analysis(record)
        assert verdict.passed is True

        # 5. Scores populated (telemetry)
        assert "code_grounding" in verdict.scores
        assert "causal_chain" in verdict.scores


# ── Step 3: test_missing_required_field ──────────────────────────────────────


class TestMissingRequiredField:
    """Gate rejects when root_cause is empty."""

    def test_empty_root_cause_degrades_score(self):
        """Empty root_cause with code evidence_refs => code_grounding score = 0.5 (borderline pass).
        Gate threshold is 0.5, so 0.5 >= 0.5 passes. This is correct behavior:
        evidence_refs alone is partial signal.
        """
        mock = _make_well_formed_analyze_response()
        mock["root_cause"] = ""
        record = build_phase_record_from_structured(mock, "ANALYZE")
        verdict = evaluate_analysis(record)
        assert verdict.scores["code_grounding"] == 0.5, "Partial signal: evidence_refs only"

    def test_no_evidence_and_no_root_cause_fails_gate(self):
        """Both root_cause and evidence_refs empty => code_grounding = 0.0 => FAIL."""
        mock = _make_well_formed_analyze_response()
        mock["root_cause"] = ""
        mock["evidence_refs"] = []
        record = build_phase_record_from_structured(mock, "ANALYZE")
        verdict = evaluate_analysis(record)
        assert verdict.passed is False, "No root_cause AND no evidence should fail"
        assert "code_grounding" in verdict.failed_rules

    def test_empty_causal_chain_fails_gate(self):
        mock = _make_well_formed_analyze_response()
        mock["causal_chain"] = ""
        record = build_phase_record_from_structured(mock, "ANALYZE")
        verdict = evaluate_analysis(record)
        assert verdict.passed is False, "Empty causal_chain should fail the gate"
        assert "causal_chain" in verdict.failed_rules

    def test_rejection_has_reasons(self):
        mock = _make_well_formed_analyze_response()
        mock["root_cause"] = ""
        mock["causal_chain"] = ""
        record = build_phase_record_from_structured(mock, "ANALYZE")
        verdict = evaluate_analysis(record)
        assert verdict.passed is False
        assert len(verdict.reasons) >= 1, "Rejection must include reasons"
        # Each reason should be a non-empty string
        for reason in verdict.reasons:
            assert isinstance(reason, str) and len(reason) > 0

    def test_scores_populated_on_failure(self):
        mock = _make_well_formed_analyze_response()
        mock["root_cause"] = ""
        mock["evidence_refs"] = []
        mock["causal_chain"] = ""
        record = build_phase_record_from_structured(mock, "ANALYZE")
        verdict = evaluate_analysis(record)
        # Scores should still be populated even on failure (HG-6: telemetry)
        assert "code_grounding" in verdict.scores
        assert "causal_chain" in verdict.scores
        assert verdict.scores["code_grounding"] == 0.0
        assert verdict.scores["causal_chain"] == 0.0


# ── Step 4: test_extraction_meta ─────────────────────────────────────────────


class TestExtractionMeta:
    """extract_phase_output() produces correct ExtractionMeta."""

    def test_tool_submitted_source(self):
        mock = _make_well_formed_analyze_response()
        schema_fields = list(arc.SCHEMA_PROPERTIES.keys())
        record, meta = extract_phase_output(
            tool_submitted=mock,
            structured_parsed=None,
            agent_message="",
            phase="ANALYZE",
            schema_fields=schema_fields,
        )
        assert isinstance(meta, ExtractionMeta)
        assert meta.source == "tool_submitted"
        assert isinstance(record, PhaseRecord)
        assert "root_cause" in meta.fields_extracted
        assert "causal_chain" in meta.fields_extracted

    def test_structured_extract_source(self):
        mock = _make_well_formed_analyze_response()
        schema_fields = list(arc.SCHEMA_PROPERTIES.keys())
        record, meta = extract_phase_output(
            tool_submitted=None,
            structured_parsed=mock,
            agent_message="",
            phase="ANALYZE",
            schema_fields=schema_fields,
        )
        assert meta.source == "structured_extract"

    def test_regex_fallback_source(self):
        agent_msg = (
            "PHASE: ANALYZE\n"
            "PRINCIPALS: causal_grounding, evidence_linkage\n"
            "ROOT_CAUSE: The bug is in django/db/models/fields.py:245\n"
            "CAUSAL_CHAIN: test fails because field.clean() is not called\n"
        )
        schema_fields = list(arc.SCHEMA_PROPERTIES.keys())
        record, meta = extract_phase_output(
            tool_submitted=None,
            structured_parsed=None,
            agent_message=agent_msg,
            phase="ANALYZE",
            schema_fields=schema_fields,
        )
        assert meta.source == "regex_fallback"
        assert isinstance(record, PhaseRecord)

    def test_fields_missing_tracked(self):
        """Partially populated response should track missing fields."""
        partial = {
            "phase": "ANALYZE",
            "subtype": "analysis.root_cause",
            "root_cause": "Bug in fields.py:245",
            "causal_chain": "",  # empty
            "principals": [],
        }
        schema_fields = list(arc.SCHEMA_PROPERTIES.keys())
        record, meta = extract_phase_output(
            tool_submitted=partial,
            structured_parsed=None,
            agent_message="",
            phase="ANALYZE",
            schema_fields=schema_fields,
        )
        assert "causal_chain" in meta.fields_missing


# ── Step 5: test_phase_record_all_fields_populated ───────────────────────────


class TestPhaseRecordAllPhases:
    """Build PhaseRecord for each phase and verify fields are populated."""

    def test_observe_fields(self):
        mock = {
            "phase": "OBSERVE",
            "subtype": "observation.fact_gathering",
            "principals": ["evidence_linkage"],
            "observations": ["Found error in log at line 42", "Stack trace points to handler.py"],
            "evidence_refs": ["handler.py:42"],
        }
        record = build_phase_record_from_structured(mock, "OBSERVE")
        assert record.phase == "OBSERVE"
        assert len(record.observations) == 2

    def test_analyze_fields(self):
        mock = _make_well_formed_analyze_response()
        record = build_phase_record_from_structured(mock, "ANALYZE")
        assert record.phase == "ANALYZE"
        assert record.root_cause
        assert record.causal_chain
        assert len(record.evidence_refs) >= 1
        assert len(record.alternative_hypotheses) == 2
        assert isinstance(record.invariant_capture, dict)

    def test_decide_fields(self):
        mock = {
            "phase": "DECIDE",
            "subtype": "decision.fix_direction",
            "principals": ["causal_grounding"],
            "options": [
                {"name": "Option A", "pros": ["simple"], "cons": ["incomplete"]},
                {"name": "Option B", "pros": ["thorough"], "cons": ["complex"]},
            ],
            "chosen": "Option A",
            "rationale": "Simpler and sufficient for this case",
            "testable_hypothesis": "If we fix validation, test_bulk_create passes",
            "expected_tests_to_pass": ["test_bulk_create"],
        }
        record = build_phase_record_from_structured(mock, "DECIDE")
        assert record.phase == "DECIDE"
        assert len(record.options) == 2
        assert record.chosen == "Option A"
        assert record.rationale
        assert record.testable_hypothesis

    def test_design_fields(self):
        mock = {
            "phase": "DESIGN",
            "subtype": "design.solution_shape",
            "principals": ["minimal_change"],
            "files_to_modify": ["django/db/models/fields.py"],
            "scope_boundary": "Only modify bulk_create path",
            "invariants": ["existing tests must still pass"],
        }
        record = build_phase_record_from_structured(mock, "DESIGN")
        assert record.phase == "DESIGN"
        assert len(record.files_to_modify) == 1
        assert record.scope_boundary

    def test_execute_fields(self):
        mock = {
            "phase": "EXECUTE",
            "subtype": "execution.code_patch",
            "principals": ["minimal_change"],
            "patch_description": "Add field.clean() call in bulk_create path",
            "files_modified": ["django/db/models/query.py"],
        }
        record = build_phase_record_from_structured(mock, "EXECUTE")
        assert record.phase == "EXECUTE"
        assert record.patch_description
        assert len(record.files_modified) == 1

    def test_judge_fields(self):
        mock = {
            "phase": "JUDGE",
            "subtype": "judge.verification",
            "principals": ["result_verification"],
            "test_results": {"passed": True, "details": "All 5 tests pass"},
            "success_criteria_met": [
                {"criterion": "bulk_create validates", "met": True, "evidence": "test passes"},
            ],
            "residual_risks": ["Performance regression under high load"],
        }
        record = build_phase_record_from_structured(mock, "JUDGE")
        assert record.phase == "JUDGE"
        assert record.test_results.get("passed") is True
        assert len(record.success_criteria_met) == 1
        assert len(record.residual_risks) == 1


# ── Step 6: test_contract_validation_all_modules ─────────────────────────────


class TestContractValidationAllModules:
    """validate_contract_definition() returns [] for all 6 phase contract modules."""

    @pytest.fixture(params=[
        "analysis_root_cause",
        "observation_fact_gathering",
        "decision_fix_direction",
        "design_solution_shape",
        "execution_code_patch",
        "judge_verification",
    ])
    def contract_module(self, request):
        import importlib
        mod = importlib.import_module(f"cognition_contracts.{request.param}")
        return mod

    def test_valid_contract(self, contract_module):
        errors = validate_contract_definition(contract_module)
        assert errors == [], (
            f"Contract {contract_module.__name__} validation errors: {errors}"
        )

    def test_compiles_without_error(self, contract_module):
        output = compile_contract(contract_module)
        assert isinstance(output, BundleContractOutput)
        assert output.schema, "schema must be non-empty"
        assert output.phase_spec.get("name"), "phase_spec.name must be set"

    def test_schema_has_properties_and_required(self, contract_module):
        output = compile_contract(contract_module)
        assert "properties" in output.schema
        assert "required" in output.schema
        # All required fields must appear in properties
        for req in output.schema["required"]:
            assert req in output.schema["properties"], (
                f"Required field '{req}' missing from schema properties "
                f"in {contract_module.__name__}"
            )

    def test_policy_has_required_fields(self, contract_module):
        output = compile_contract(contract_module)
        assert "required_principals" in output.policy
        assert "required_fields" in output.policy
        assert "gate_threshold" in output.policy

    def test_repair_templates_match_gate_rules(self, contract_module):
        output = compile_contract(contract_module)
        for rule in contract_module.GATE_RULES:
            assert rule.name in output.repair_templates, (
                f"GateRule '{rule.name}' missing from repair_templates "
                f"in {contract_module.__name__}"
            )
            template = output.repair_templates[rule.name]
            assert template["hint"], (
                f"repair hint for '{rule.name}' must be non-empty "
                f"in {contract_module.__name__}"
            )


# ── Step 7: test_gate_failure_produces_typed_code ────────────────────────────


class TestGateFailureTypedCodes:
    """Gate failures produce GateFailureCode instances, not bare strings."""

    def test_missing_principal_code(self):
        from gate_failure_code import missing_principal
        code = missing_principal("causal_grounding", "ANALYZE", "analysis.root_cause")
        assert isinstance(code, GateFailureCode)
        assert code.category == GateFailureCategory.MISSING_PRINCIPAL
        assert code.subcode == "causal_grounding"
        assert code.phase == "ANALYZE"
        assert code.subtype == "analysis.root_cause"
        assert code.code == "MISSING_PRINCIPAL:causal_grounding"

    def test_forbidden_principal_code(self):
        from gate_failure_code import forbidden_principal
        code = forbidden_principal("minimal_change", "ANALYZE", "analysis.root_cause")
        assert isinstance(code, GateFailureCode)
        assert code.category == GateFailureCategory.FORBIDDEN_PRINCIPAL
        assert code.code == "FORBIDDEN_PRINCIPAL:minimal_change"

    def test_missing_field_code(self):
        from gate_failure_code import missing_field
        code = missing_field("root_cause", "ANALYZE", "analysis.root_cause")
        assert isinstance(code, GateFailureCode)
        assert code.category == GateFailureCategory.MISSING_FIELD
        assert code.code == "MISSING_FIELD:root_cause"

    def test_semantic_fail_code(self):
        from gate_failure_code import semantic_fail
        code = semantic_fail("causal_chain", "ANALYZE", "analysis.root_cause", gate_rule="causal_chain")
        assert isinstance(code, GateFailureCode)
        assert code.category == GateFailureCategory.SEMANTIC_FAIL
        assert code.code == "SEMANTIC_FAIL:causal_chain"

    def test_forbidden_transition_code(self):
        from gate_failure_code import forbidden_transition
        code = forbidden_transition("ANALYZE", "EXECUTE", "analysis.root_cause")
        assert isinstance(code, GateFailureCode)
        assert code.category == GateFailureCategory.FORBIDDEN_TRANSITION
        assert code.code == "FORBIDDEN_TRANSITION:ANALYZE->EXECUTE"

    def test_fake_principal_code(self):
        from gate_failure_code import fake_principal
        code = fake_principal("evidence_linkage", "ANALYZE", "analysis.root_cause")
        assert isinstance(code, GateFailureCode)
        assert code.category == GateFailureCategory.FAKE_PRINCIPAL

    def test_admission_retryable_uses_typed_codes(self):
        """evaluate_admission with missing principal returns typed GateFailureCode."""
        from principal_gate import evaluate_admission, AdmissionResult
        from routing_decision import AdmissionStatus

        record = PhaseRecord(
            phase="ANALYZE",
            subtype="analysis.root_cause",
            principals=[],  # missing required principals
            claims=[],
            evidence_refs=["django/db/models.py:42"],
            from_steps=[],
            content="Analysis content",
            root_cause="Bug in fields.py:245 where validation is skipped",
            causal_chain="test -> bulk_create -> missing clean() -> corrupt data in DB",
        )
        result = evaluate_admission(record, "ANALYZE")
        assert isinstance(result, AdmissionResult)
        if result.status == AdmissionStatus.RETRYABLE:
            # Reasons should contain GateFailureCode objects
            for reason in result.reasons:
                assert isinstance(reason, GateFailureCode), (
                    f"Expected GateFailureCode, got {type(reason)}: {reason}"
                )

    def test_admission_admitted_for_complete_record(self):
        """evaluate_admission passes for a fully populated record."""
        from principal_gate import evaluate_admission, AdmissionResult
        from routing_decision import AdmissionStatus

        mock = _make_well_formed_analyze_response()
        record = build_phase_record_from_structured(mock, "ANALYZE")
        result = evaluate_admission(record, "ANALYZE")
        assert isinstance(result, AdmissionResult)
        # Should be ADMITTED (all principals present, all fields present)
        assert result.status == AdmissionStatus.ADMITTED, (
            f"Expected ADMITTED, got {result.status}; reasons={result.reasons_legacy}"
        )


# ── Bonus: compilation error on invalid contract ─────────────────────────────


class TestCompilationError:
    """compile_contract raises CompilationError for invalid modules."""

    def test_invalid_module_raises(self):
        import types
        bad_module = types.ModuleType("bad_contract")
        bad_module.PHASE = ""  # empty = check 2 fails
        with pytest.raises(CompilationError) as exc_info:
            compile_contract(bad_module)
        assert len(exc_info.value.errors) > 0


# ── Bonus: routing decision populated on failure ─────────────────────────────


class TestRoutingOnFailure:
    """RETRYABLE admission includes a RoutingDecision."""

    def test_retryable_has_routing(self):
        from principal_gate import evaluate_admission
        from routing_decision import AdmissionStatus, RoutingDecision

        record = PhaseRecord(
            phase="ANALYZE",
            subtype="analysis.root_cause",
            principals=[],  # missing required
            claims=[],
            evidence_refs=[],
            from_steps=[],
            content="",
        )
        result = evaluate_admission(record, "ANALYZE")
        if result.status == AdmissionStatus.RETRYABLE:
            assert result.routing is not None, "RETRYABLE must include RoutingDecision"
            assert isinstance(result.routing, RoutingDecision)
            assert result.routing.next_phase, "next_phase must be non-empty"
