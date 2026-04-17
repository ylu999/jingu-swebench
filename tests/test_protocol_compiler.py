"""
test_protocol_compiler.py — Protocol Compiler build-time verification.

Ensures:
  1. All protocol specs compile without errors
  2. Enforcement rules catch violations correctly
  3. Tool schema contains all protocol-required fields
  4. Control fields are protocol-required (R2)
  5. Fail-closed fields have gate rules (R3)
  6. Runtime get_control_field rejects missing fields
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mini-swe-agent"))


class TestProtocolCompilesPasses:
    """Full protocol compilation must pass with zero errors."""

    def test_compile_passes(self):
        from protocol_compiler import compile_protocol
        specs, errors = compile_protocol()
        if errors:
            msgs = [f"{e.code}: {e.field_name} ({e.phase}) — {e.message}" for e in errors]
            assert False, "Protocol compile failed:\n" + "\n".join(msgs)
        assert len(specs) > 0

    def test_analyze_has_control_fields(self):
        from protocol_compiler import _get_protocol_specs
        specs = _get_protocol_specs()
        control = [s for s in specs if s.is_control_field and s.phase == "ANALYZE"]
        assert len(control) >= 1
        assert any(s.name == "repair_strategy_type" for s in control)

    def test_all_control_fields_are_protocol_required(self):
        """R2: every control field must be protocol_required."""
        from protocol_compiler import _get_protocol_specs
        specs = _get_protocol_specs()
        for spec in specs:
            if spec.is_control_field:
                assert spec.protocol_required, (
                    f"{spec.name} ({spec.phase}) is control but not protocol_required"
                )


class TestEnforcementRules:
    """Enforcement rules must catch known violation patterns."""

    def test_r1_tool_field_missing(self):
        """R1: protocol_required field missing from tool schema = error."""
        from protocol_compiler import ProtocolFieldSpec, enforce_protocol_rules
        specs = [ProtocolFieldSpec(
            name="test_field",
            phase="ANALYZE",
            required=True,
            protocol_required=True,
        )]
        # Tool schema has no fields for ANALYZE
        tool_schemas = {"ANALYZE": set()}
        errors = enforce_protocol_rules(specs, tool_schemas)
        assert any(e.code == "TOOL_FIELD_MISSING" for e in errors)

    def test_r1_tool_field_present_passes(self):
        """R1: protocol_required field in tool schema = no error."""
        from protocol_compiler import ProtocolFieldSpec, enforce_protocol_rules
        specs = [ProtocolFieldSpec(
            name="test_field",
            phase="ANALYZE",
            required=True,
            protocol_required=True,
        )]
        tool_schemas = {"ANALYZE": {"test_field"}}
        errors = enforce_protocol_rules(specs, tool_schemas)
        r1_errors = [e for e in errors if e.code == "TOOL_FIELD_MISSING"]
        assert len(r1_errors) == 0

    def test_r2_control_not_protocol(self):
        """R2: control field without protocol_required = error."""
        from protocol_compiler import ProtocolFieldSpec, enforce_protocol_rules
        specs = [ProtocolFieldSpec(
            name="test_field",
            phase="ANALYZE",
            is_control_field=True,
            protocol_required=False,  # violation
        )]
        errors = enforce_protocol_rules(specs)
        assert any(e.code == "CONTROL_FIELD_NOT_PROTOCOL_ENFORCED" for e in errors)

    def test_r4_consumer_without_protocol(self):
        """R4: field with consumers but not protocol_required = error."""
        from protocol_compiler import ProtocolFieldSpec, enforce_protocol_rules
        specs = [ProtocolFieldSpec(
            name="test_field",
            phase="ANALYZE",
            consumers=("nprg",),
            protocol_required=False,  # violation
        )]
        errors = enforce_protocol_rules(specs)
        assert any(e.code == "CONSUMER_WITHOUT_PROTOCOL" for e in errors)


class TestToolSchemaVerification:
    """Verify that actual tool schema contains all protocol-required fields."""

    def test_analyze_tool_has_all_protocol_fields(self):
        from protocol_compiler import _get_protocol_specs
        from jingu_model import _build_phase_record_tool
        from bundle_compiler import compile_bundle

        bundle = compile_bundle(force_reload=True)
        schema = bundle.governance.get_constrained_schema("ANALYZE")
        assert schema is not None

        tool = _build_phase_record_tool("ANALYZE", schema)
        tool_params = set(tool["function"]["parameters"]["properties"].keys())

        specs = _get_protocol_specs()
        for spec in specs:
            if spec.phase == "ANALYZE" and spec.protocol_required:
                assert spec.name in tool_params, (
                    f"Protocol-required field '{spec.name}' missing from "
                    f"submit_phase_record tool parameters"
                )


class TestRecordValidation:
    """validate_record_protocol must reject incomplete submissions."""

    def test_complete_record_passes(self):
        from protocol_compiler import validate_record_protocol, _get_protocol_specs
        specs = _get_protocol_specs()
        record = {
            "repair_strategy_type": "REGEX_FIX",
            "root_cause": "Something in file.py:42",
            "causal_chain": "Test fails because regex doesn't match negative",
            "evidence_refs": ["file.py:42"],
            "alternative_hypotheses": [{"hypothesis": "a", "ruled_out_reason": "b"}],
            "invariant_capture": {"identified_invariants": ["x"], "risk_if_violated": "y"},
        }
        missing = validate_record_protocol(record, "ANALYZE", specs)
        assert missing == []

    def test_missing_control_field_rejected(self):
        from protocol_compiler import validate_record_protocol, _get_protocol_specs
        specs = _get_protocol_specs()
        record = {
            "root_cause": "Something",
            "causal_chain": "Chain here",
            "evidence_refs": ["file.py:42"],
            "alternative_hypotheses": [{"hypothesis": "a", "ruled_out_reason": "b"}],
            "invariant_capture": {"identified_invariants": ["x"], "risk_if_violated": "y"},
            # repair_strategy_type MISSING
        }
        missing = validate_record_protocol(record, "ANALYZE", specs)
        assert "repair_strategy_type" in missing

    def test_invalid_enum_rejected(self):
        from protocol_compiler import validate_record_protocol, _get_protocol_specs
        specs = _get_protocol_specs()
        record = {
            "repair_strategy_type": "MAGIC_FIX",  # invalid enum
            "root_cause": "Something in file.py:42",
            "causal_chain": "Test fails because X",
            "evidence_refs": ["file.py:42"],
            "alternative_hypotheses": [{"hypothesis": "a", "ruled_out_reason": "b"}],
            "invariant_capture": {"identified_invariants": ["x"], "risk_if_violated": "y"},
        }
        missing = validate_record_protocol(record, "ANALYZE", specs)
        assert "repair_strategy_type" in missing

    def test_empty_string_rejected(self):
        from protocol_compiler import validate_record_protocol, _get_protocol_specs
        specs = _get_protocol_specs()
        record = {
            "repair_strategy_type": "REGEX_FIX",
            "root_cause": "",  # empty
            "causal_chain": "Chain",
            "evidence_refs": ["file.py:42"],
            "alternative_hypotheses": [{"hypothesis": "a", "ruled_out_reason": "b"}],
            "invariant_capture": {"identified_invariants": ["x"], "risk_if_violated": "y"},
        }
        missing = validate_record_protocol(record, "ANALYZE", specs)
        assert "root_cause" in missing


class TestGetControlField:
    """get_control_field must raise on missing, never fallback."""

    def test_present_returns_value(self):
        from protocol_compiler import get_control_field
        val = get_control_field({"repair_strategy_type": "REGEX_FIX"}, "repair_strategy_type")
        assert val == "REGEX_FIX"

    def test_missing_raises(self):
        from protocol_compiler import get_control_field, ControlFieldMissing
        with pytest.raises(ControlFieldMissing):
            get_control_field({"root_cause": "x"}, "repair_strategy_type")

    def test_none_record_raises(self):
        from protocol_compiler import get_control_field, ControlFieldMissing
        with pytest.raises(ControlFieldMissing):
            get_control_field(None, "repair_strategy_type")

    def test_empty_string_raises(self):
        from protocol_compiler import get_control_field, ControlFieldMissing
        with pytest.raises(ControlFieldMissing):
            get_control_field({"repair_strategy_type": ""}, "repair_strategy_type")


class TestPromptGeneration:
    """Prompt fragment must mention all protocol-required fields."""

    def test_analyze_prompt_has_strategy(self):
        from protocol_compiler import build_prompt_fragment, _get_protocol_specs
        specs = _get_protocol_specs()
        prompt = build_prompt_fragment("ANALYZE", specs)
        assert "REPAIR_STRATEGY_TYPE" in prompt
        assert "submit_phase_record" in prompt

    def test_analyze_prompt_has_all_protocol_keys(self):
        from protocol_compiler import build_prompt_fragment, _get_protocol_specs
        specs = _get_protocol_specs()
        prompt = build_prompt_fragment("ANALYZE", specs)
        for spec in specs:
            if spec.phase == "ANALYZE" and spec.protocol_required and spec.prompt_key:
                assert spec.prompt_key in prompt, (
                    f"Protocol-required field '{spec.prompt_key}' not in prompt"
                )


class TestConsumerRegistry:
    """Consumer registry must map all declared consumers."""

    def test_nprg_gets_repair_strategy(self):
        from protocol_compiler import build_consumer_registry, _get_protocol_specs
        specs = _get_protocol_specs()
        registry = build_consumer_registry(specs)
        assert "nprg" in registry
        assert "repair_strategy_type" in registry["nprg"]

    def test_analysis_gate_gets_required_fields(self):
        from protocol_compiler import build_consumer_registry, _get_protocol_specs
        specs = _get_protocol_specs()
        registry = build_consumer_registry(specs)
        assert "analysis_gate" in registry
        gate_fields = registry["analysis_gate"]
        assert "root_cause" in gate_fields
        assert "causal_chain" in gate_fields


class TestReplaySchema:
    """Replay schema must include all control fields."""

    def test_control_fields_in_replay(self):
        from protocol_compiler import build_replay_schema, _get_protocol_specs
        specs = _get_protocol_specs()
        replay = build_replay_schema(specs)
        assert "repair_strategy_type" in replay
        assert replay["repair_strategy_type"]["required"] is True
        assert replay["repair_strategy_type"]["source"] == "protocol_record"
