"""
test_gate_rejection.py -- Unit tests for p217 SDG types.

Verifies:
- GateRejection construction with all fields
- FieldFailure with each reason type
- ContractView with field_specs
- build_repair_from_rejection() output format
- build_gate_rejection() convenience constructor
- SDG_ENABLED feature flag import
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest
from gate_rejection import (
    GateRejection,
    ContractView,
    FieldSpec,
    FieldFailure,
    build_repair_from_rejection,
    build_gate_rejection,
    SDG_ENABLED,
)


# -- Helpers --

def _make_contract() -> ContractView:
    return ContractView(
        required_fields=["root_cause", "causal_chain", "evidence_refs"],
        field_specs={
            "root_cause": FieldSpec(
                description="Identified root cause with code reference",
                required=True,
                min_length=10,
                semantic_check="grounded_in_code",
            ),
            "causal_chain": FieldSpec(
                description="Causal chain from test failure to code bug",
                required=True,
                min_length=20,
            ),
            "evidence_refs": FieldSpec(
                description="Code/test references supporting the analysis",
                required=True,
            ),
        },
    )


def _make_failures() -> list[FieldFailure]:
    return [
        FieldFailure(
            field="root_cause",
            reason="missing",
            hint="Provide a root cause with specific code reference (file:line)",
            expected="Non-empty string with code reference",
            actual=None,
        ),
        FieldFailure(
            field="causal_chain",
            reason="too_short",
            hint="Explain the causal chain: test failure -> condition -> code -> why",
            expected="At least 20 characters of causal reasoning",
            actual="a -> b",
        ),
    ]


def _make_rejection() -> GateRejection:
    return GateRejection(
        gate_name="analysis_gate",
        contract=_make_contract(),
        failures=_make_failures(),
        extracted={"root_cause": "", "causal_chain": "a -> b"},
    )


# -- Tests: GateRejection construction --

class TestGateRejection:

    def test_basic_construction(self):
        r = _make_rejection()
        assert r.gate_name == "analysis_gate"
        assert len(r.failures) == 2
        assert r.timestamp != ""

    def test_timestamp_auto_filled(self):
        r = GateRejection(
            gate_name="test", contract=ContractView(required_fields=[]),
            failures=[], extracted={},
        )
        assert r.timestamp != ""
        assert "T" in r.timestamp  # ISO format

    def test_explicit_timestamp(self):
        r = GateRejection(
            gate_name="test", contract=ContractView(required_fields=[]),
            failures=[], extracted={}, timestamp="2026-04-10T00:00:00Z",
        )
        assert r.timestamp == "2026-04-10T00:00:00Z"


# -- Tests: FieldFailure reason types --

class TestFieldFailure:

    def test_missing_reason(self):
        f = FieldFailure(field="root_cause", reason="missing",
                         hint="provide root cause", expected="non-empty")
        assert f.reason == "missing"
        assert f.actual is None

    def test_too_short_reason(self):
        f = FieldFailure(field="causal_chain", reason="too_short",
                         hint="extend causal chain", expected="20+ chars",
                         actual="short")
        assert f.actual == "short"

    def test_semantic_fail_reason(self):
        f = FieldFailure(field="root_cause", reason="semantic_fail",
                         hint="must reference code", expected="code ref",
                         actual="vague statement")
        assert f.reason == "semantic_fail"

    def test_format_invalid_reason(self):
        f = FieldFailure(field="principals", reason="format_invalid",
                         hint="use comma-separated list", expected="list",
                         actual="single string")
        assert f.reason == "format_invalid"

    def test_principal_violation_reason(self):
        f = FieldFailure(field="causal_grounding", reason="principal_violation",
                         hint="declare causal_grounding principal",
                         expected="principal declared")
        assert f.reason == "principal_violation"


# -- Tests: ContractView --

class TestContractView:

    def test_with_field_specs(self):
        c = _make_contract()
        assert "root_cause" in c.field_specs
        assert c.field_specs["root_cause"].required is True
        assert c.field_specs["root_cause"].min_length == 10

    def test_empty_contract(self):
        c = ContractView(required_fields=[])
        assert c.required_fields == []
        assert c.field_specs == {}


# -- Tests: build_repair_from_rejection --

class TestBuildRepairFromRejection:

    def test_contains_gate_name(self):
        r = _make_rejection()
        output = build_repair_from_rejection(r)
        assert "analysis_gate" in output

    def test_contains_required_fields(self):
        r = _make_rejection()
        output = build_repair_from_rejection(r)
        assert "root_cause" in output
        assert "causal_chain" in output

    def test_contains_failure_hints(self):
        r = _make_rejection()
        output = build_repair_from_rejection(r)
        assert "Provide a root cause" in output
        assert "Explain the causal chain" in output

    def test_contains_extracted_values(self):
        r = _make_rejection()
        output = build_repair_from_rejection(r)
        assert "Extracted values" in output

    def test_non_empty_output(self):
        r = _make_rejection()
        output = build_repair_from_rejection(r)
        assert len(output) > 0

    def test_empty_rejection_still_produces_output(self):
        r = GateRejection(
            gate_name="test", contract=ContractView(required_fields=[]),
            failures=[], extracted={},
        )
        output = build_repair_from_rejection(r)
        assert "[GATE REJECT: test]" in output

    def test_long_actual_truncated(self):
        f = FieldFailure(
            field="content", reason="too_short",
            hint="extend", expected="long text",
            actual="x" * 200,
        )
        r = GateRejection(
            gate_name="test", contract=ContractView(required_fields=["content"]),
            failures=[f], extracted={},
        )
        output = build_repair_from_rejection(r)
        # actual should be truncated to 80 chars
        assert "x" * 80 in output
        assert "x" * 200 not in output


# -- Tests: build_gate_rejection convenience --

class TestBuildGateRejection:

    def test_convenience_constructor(self):
        contract = _make_contract()
        failures = _make_failures()
        r = build_gate_rejection("my_gate", contract, {"key": "val"}, failures)
        assert r.gate_name == "my_gate"
        assert r.extracted == {"key": "val"}
        assert len(r.failures) == 2
        assert r.timestamp != ""


# -- Tests: SDG_ENABLED flag --

class TestSDGEnabled:

    def test_sdg_enabled_is_bool(self):
        assert isinstance(SDG_ENABLED, bool)
