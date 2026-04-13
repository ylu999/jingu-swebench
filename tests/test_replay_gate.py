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
