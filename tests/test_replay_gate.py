"""
test_replay_gate.py — Pipeline replay gate.

Run BEFORE every pipeline launch (smoke/batch/eval). Verifies the full
SST projection chain works end-to-end without any LLM calls:

  bundle.json compile → get_constrained_schema → _build_phase_record_tool
                                                → build_phase_prefix

Invariants checked:
  1. Bundle compiles without error
  2. Every schema property has a description (SST completeness)
  3. Tool description contains all schema field descriptions (projection A)
  4. Phase prefix contains renderer output (projection B)
  5. No duplication between behavioral prompt and field guidance
  6. Contract consistency tests pass (schema ↔ gate alignment)

Exit code 0 = safe to deploy. Non-zero = contract drift detected.
"""

import json
import sys
import os
import re

import pytest

# Path setup — same as other tests
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mini-swe-agent"))


def _compile_bundle():
    """Compile bundle and return governance object."""
    from bundle_compiler import compile_bundle
    bundle = compile_bundle(force_reload=True)
    return bundle.governance


# ── Stage 1: Bundle compiles ─────────────────────────────────────────────────

class TestBundleCompiles:
    """Bundle must compile without error. Catches broken JSON, missing files."""

    def test_bundle_compiles(self):
        gov = _compile_bundle()
        assert gov is not None
        assert hasattr(gov, "get_constrained_schema")
        assert hasattr(gov, "get_phase_prompt")


# ── Stage 2: Schema description completeness ─────────────────────────────────

class TestSchemaDescriptionCompleteness:
    """Every schema property must have a non-empty description (SST foundation)."""

    def test_analyze_schema_descriptions_complete(self):
        from schema_field_guidance import validate_schema_descriptions
        gov = _compile_bundle()
        schema = gov.get_constrained_schema("ANALYZE")
        assert schema is not None, "ANALYZE has no constrained schema"
        missing = validate_schema_descriptions(schema, phase="ANALYZE")
        assert missing == [], f"Missing descriptions: {missing}"

    def test_all_phase_schemas_descriptions_complete(self):
        from schema_field_guidance import validate_schema_descriptions
        gov = _compile_bundle()
        all_missing = []
        for phase in ["ANALYZE", "EXECUTE", "JUDGE"]:
            schema = gov.get_constrained_schema(phase)
            if schema is None:
                continue
            missing = validate_schema_descriptions(schema, phase=phase)
            all_missing.extend(missing)
        assert all_missing == [], f"Missing descriptions: {all_missing}"


# ── Stage 3: Tool description projection (projection A) ─────────────────────

class TestToolDescriptionProjection:
    """Tool description must contain all schema field descriptions."""

    def test_analyze_tool_contains_all_field_descriptions(self):
        from jingu_model import _build_phase_record_tool
        gov = _compile_bundle()
        schema = gov.get_constrained_schema("ANALYZE")
        tool = _build_phase_record_tool("ANALYZE", schema)
        desc = tool["function"]["description"]

        props = schema.get("properties", {})
        if "json_schema" in schema:
            props = schema["json_schema"]["schema"].get("properties", {})
        elif "schema" in schema and "properties" not in schema:
            props = schema["schema"].get("properties", {})

        for field_name, field_schema in props.items():
            field_desc = field_schema.get("description", "")
            if field_desc and len(field_desc) > 10:
                assert field_desc in desc, (
                    f"Schema field '{field_name}' description not in tool desc: "
                    f"'{field_desc[:60]}...'"
                )

    def test_analyze_tool_mentions_all_required_fields(self):
        from jingu_model import _build_phase_record_tool
        gov = _compile_bundle()
        schema = gov.get_constrained_schema("ANALYZE")
        tool = _build_phase_record_tool("ANALYZE", schema)
        desc = tool["function"]["description"]

        required = schema.get("required", [])
        if "json_schema" in schema:
            required = schema["json_schema"]["schema"].get("required", [])
        elif "schema" in schema and "required" not in schema:
            required = schema["schema"].get("required", [])

        auto_fields = {"phase", "subtype"}
        for field in required:
            if field in auto_fields:
                continue
            assert field in desc, (
                f"Required field '{field}' not mentioned in tool description"
            )


# ── Stage 4: Phase prefix projection (projection B) ─────────────────────────

class TestPhasePrefixProjection:
    """Phase prefix must contain schema-derived field guidance."""

    def test_analyze_prefix_contains_field_guidance(self):
        from phase_prompt import build_phase_prefix
        prefix = build_phase_prefix("ANALYZE")
        assert prefix, "ANALYZE phase prefix is empty"
        # Must contain renderer output markers
        assert "root_cause" in prefix
        assert "causal_chain" in prefix
        assert "evidence_refs" in prefix
        # Must mention tool call
        assert "submit_phase_record" in prefix

    def test_analyze_prefix_no_duplication(self):
        """Field descriptions should appear once (from renderer), not also in behavioral prompt."""
        from phase_prompt import build_phase_prefix
        prefix = build_phase_prefix("ANALYZE")
        # These patterns indicate duplicated field descriptions
        duplication_patterns = [
            "### Required Fields",
            "### Optional Fields",
            "root_cause (string):",
            "causal_chain (string):",
        ]
        for pattern in duplication_patterns:
            assert pattern not in prefix, (
                f"Phase prefix contains duplicated field description '{pattern}'"
            )


# ── Stage 5: No conflicting format instructions ─────────────────────────────

class TestNoConflictingFormats:
    """ANALYZE must not have conflicting text-section vs tool-call instructions."""

    def test_no_text_section_checker_for_analyze(self):
        """step_sections.py must NOT check ANALYZE for text sections."""
        from step_sections import PHASE_REQUIRED_FIELDS
        assert "ANALYZE" not in PHASE_REQUIRED_FIELDS, (
            "ANALYZE still in PHASE_REQUIRED_FIELDS — conflicts with tool-call path"
        )

    def test_prompt_guidance_no_natural_language_format(self):
        """PROMPT_GUIDANCE must not tell agent to write 'ROOT_CAUSE:\\n<text>'."""
        from cognition_contracts.analysis_root_cause import PROMPT_GUIDANCE
        conflicting = [
            r"ROOT_CAUSE:\n",
            r"CAUSAL_CHAIN:\n",
            r"EVIDENCE:\n",
            r"You MUST produce your analysis in this exact format",
        ]
        for pattern in conflicting:
            assert not re.search(pattern, PROMPT_GUIDANCE), (
                f"PROMPT_GUIDANCE has conflicting format instruction: '{pattern}'"
            )


# ── Stage 6: Renderer is the sole projection path ───────────────────────────

class TestRendererIsSolePath:
    """_build_phase_record_tool must use the renderer, not hardcode."""

    def test_tool_builder_uses_renderer(self):
        import inspect
        from jingu_model import _build_phase_record_tool
        source = inspect.getsource(_build_phase_record_tool)
        assert "render_schema_field_guidance" in source, (
            "_build_phase_record_tool does not call render_schema_field_guidance"
        )
        assert "For ANALYZE:" not in source
        assert "For EXECUTE:" not in source


# ── Stage 7: Field onboarding completeness ────────────────────────────────

class TestFieldOnboarding:
    """Control-grade fields must be fully onboarded: defined + prompted + extracted + gated + consumed.

    Build fails if any control field is declared-but-not-wired.
    """

    def test_analyze_repair_strategy_type_fully_onboarded(self):
        """repair_strategy_type must be onboarded across all 4 layers."""
        from cognition_contracts.analysis_root_cause import (
            SCHEMA_PROPERTIES, SCHEMA_REQUIRED, PROMPT_GUIDANCE,
            REPAIR_STRATEGY_TYPES,
        )
        from phase_record import PhaseRecord
        import inspect

        errors = []

        # 1. Defined: in schema
        if "repair_strategy_type" not in SCHEMA_PROPERTIES:
            errors.append("DEFINED: not in SCHEMA_PROPERTIES")
        if "repair_strategy_type" not in SCHEMA_REQUIRED:
            errors.append("DEFINED: not in SCHEMA_REQUIRED")

        # 2. Requested: in prompt
        if "REPAIR_STRATEGY_TYPE" not in PROMPT_GUIDANCE:
            errors.append("REQUESTED: not mentioned in PROMPT_GUIDANCE")

        # 3. Produced: PhaseRecord has the field + extractor sets it
        if not hasattr(PhaseRecord, "repair_strategy_type"):
            errors.append("PRODUCED: PhaseRecord missing repair_strategy_type field")
        from declaration_extractor import build_phase_record_from_structured
        src = inspect.getsource(build_phase_record_from_structured)
        if "repair_strategy_type" not in src:
            errors.append("PRODUCED: build_phase_record_from_structured doesn't extract repair_strategy_type")

        # 4. Consumed: analysis_gate checks it
        from analysis_gate import evaluate_analysis
        gate_src = inspect.getsource(evaluate_analysis)
        if "repair_strategy_type" not in gate_src:
            errors.append("CONSUMED: analysis_gate doesn't check repair_strategy_type")

        assert errors == [], (
            f"repair_strategy_type NOT fully onboarded:\n" +
            "\n".join(f"  - {e}" for e in errors)
        )

    def test_control_fields_in_schema_have_gate_check(self):
        """Every required field in ANALYZE schema must be checked by the gate module."""
        import inspect
        from cognition_contracts.analysis_root_cause import SCHEMA_REQUIRED
        import analysis_gate

        # Check the entire module source, not just evaluate_analysis
        gate_src = inspect.getsource(analysis_gate)
        # These are auto-filled metadata, not content fields
        auto_fields = {"phase", "subtype", "principals"}
        missing_gate = []
        for field in SCHEMA_REQUIRED:
            if field in auto_fields:
                continue
            if field not in gate_src:
                missing_gate.append(field)
        assert missing_gate == [], (
            f"Required schema fields without gate check: {missing_gate}"
        )


# ── Stage 8: Onboarding audit (L0 static verification) ─────────────────────

class TestOnboardingAudit:
    """Build-time onboarding completeness: declared fields must be fully wired."""

    def test_onboarding_audit_passes(self):
        from onboarding_audit import run_audit
        errors = run_audit()
        if errors:
            msgs = [f"{e.code}: {e.field_name} ({e.phase}) — {e.message}" for e in errors]
            assert False, "Onboarding audit failed:\n" + "\n".join(msgs)


# ── Stage 9: Protocol Compiler (build-time protocol enforcement) ──────────

class TestProtocolCompiler:
    """Protocol compilation: FieldSpec -> tool/prompt/gate/consumer/replay all wired."""

    def test_protocol_compiles_clean(self):
        from protocol_compiler import compile_protocol
        specs, errors = compile_protocol()
        if errors:
            msgs = [f"{e.code}: {e.field_name} ({e.phase}) — {e.message}" for e in errors]
            assert False, "Protocol compile failed:\n" + "\n".join(msgs)

    def test_control_fields_protocol_required(self):
        """R2: every control field must be protocol_required."""
        from protocol_compiler import _get_protocol_specs
        for spec in _get_protocol_specs():
            if spec.is_control_field:
                assert spec.protocol_required, (
                    f"{spec.name} ({spec.phase}) is control but not protocol_required"
                )
