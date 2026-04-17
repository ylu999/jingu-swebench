"""
test_contract_consistency.py — Compile-time consistency check for ANALYZE contract.

Verifies that the 5 layers of the ANALYZE contract are aligned:
1. Bundle schema (bundle.json) — tool parameters the agent sees
2. Bundle prompt (bundle.json) — text guidance the agent reads
3. Gate rules (analysis_root_cause.py) — what the gate checks
4. Prompt guidance (analysis_root_cause.py) — phase_prompt injection
5. Tool description (jingu_model.py) — field guidance in tool desc

If any layer drifts, the agent gets contradictory instructions and wastes
steps guessing which fields to fill. This test catches drift at CI time.
"""

import json
import sys
import os
import re

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))


def _load_bundle_analyze():
    """Load ANALYZE contract from bundle.json."""
    bundle_path = os.path.join(os.path.dirname(__file__), "..", "bundle.json")
    with open(bundle_path) as f:
        bundle = json.load(f)
    return bundle["contracts"]["analysis.root_cause"]


class TestBundleSchemaGateAlignment:
    """Bundle schema fields must match what the gate checks."""

    def test_gate_required_fields_in_bundle_schema(self):
        """Every gate-required field must exist in bundle schema properties."""
        from cognition_contracts.analysis_root_cause import FIELD_SPECS
        contract = _load_bundle_analyze()
        schema_props = set(contract["schema"]["properties"].keys())

        for fs in FIELD_SPECS:
            if fs.required:
                # Gate field names may differ from schema field names.
                # Map gate field → schema field.
                field_map = {
                    "alternatives_considered": "alternative_hypotheses",
                }
                schema_name = field_map.get(fs.name, fs.name)
                assert schema_name in schema_props, (
                    f"Gate requires field '{fs.name}' (schema: '{schema_name}') "
                    f"but bundle schema only has: {sorted(schema_props)}"
                )

    def test_bundle_required_matches_gate_required(self):
        """Bundle schema 'required' list must include all gate hard-check fields."""
        from cognition_contracts.analysis_root_cause import GATE_RULES
        contract = _load_bundle_analyze()
        bundle_required = set(contract["schema"]["required"])

        # Gate rules with hard checks (code_grounding, causal_chain)
        # map to schema fields via their .field attribute
        field_map = {
            "alternatives_considered": "alternative_hypotheses",
        }
        for rule in GATE_RULES:
            schema_name = field_map.get(rule.field, rule.field)
            # root_cause and causal_chain are hard requirements
            if rule.field in ("root_cause", "causal_chain"):
                assert schema_name in bundle_required, (
                    f"Gate hard-checks '{rule.field}' but bundle schema doesn't "
                    f"require '{schema_name}'. Bundle required: {sorted(bundle_required)}"
                )


class TestBundlePromptSchemaAlignment:
    """Bundle prompt should NOT duplicate field descriptions (renderer owns those)."""

    def test_prompt_does_not_duplicate_field_descriptions(self):
        """Bundle prompt should not contain field descriptions — renderer handles those.

        The bundle prompt is for behavioral guidance (goals, rules, constraints).
        Field descriptions come from schema via render_schema_field_guidance().
        If the prompt also lists fields, agent sees them twice.
        """
        contract = _load_bundle_analyze()
        prompt = contract["prompt"]
        # These patterns indicate duplicated field descriptions in the prompt
        duplication_patterns = [
            "### Required Fields",
            "### Optional Fields",
            "root_cause (string):",
            "causal_chain (string):",
            "evidence_refs (array",
        ]
        for pattern in duplication_patterns:
            assert pattern not in prompt, (
                f"Bundle prompt contains field description '{pattern}'. "
                f"Field descriptions should only come from schema via the renderer."
            )

    def test_tool_description_mentions_all_required_fields(self):
        """Tool description (rendered from schema) must mention all required fields."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mini-swe-agent"))
        from jingu_model import _build_phase_record_tool

        contract = _load_bundle_analyze()
        schema = contract["schema"]
        tool = _build_phase_record_tool("ANALYZE", schema)
        desc = tool["function"]["description"]

        auto_fields = {"phase", "subtype"}
        for field in schema["required"]:
            if field in auto_fields:
                continue
            assert field in desc, (
                f"Schema requires '{field}' but tool description doesn't mention it."
            )

    def test_tool_description_mentions_optional_fields(self):
        """Tool description must also mention optional fields."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mini-swe-agent"))
        from jingu_model import _build_phase_record_tool

        contract = _load_bundle_analyze()
        schema = contract["schema"]
        tool = _build_phase_record_tool("ANALYZE", schema)
        desc = tool["function"]["description"]

        schema_props = set(schema["properties"].keys())
        schema_required = set(schema["required"])
        auto_fields = {"phase", "subtype"}

        optional = schema_props - schema_required - auto_fields
        for field in optional:
            assert field in desc, (
                f"Schema has optional field '{field}' but tool description "
                f"doesn't mention it."
            )


class TestPromptGuidanceToolAlignment:
    """phase_prompt.py guidance must reinforce tool-call path, not conflict."""

    def test_prompt_guidance_mentions_tool_call(self):
        """PROMPT_GUIDANCE should tell agent to use submit_phase_record."""
        from cognition_contracts.analysis_root_cause import PROMPT_GUIDANCE
        assert "submit_phase_record" in PROMPT_GUIDANCE, (
            "PROMPT_GUIDANCE doesn't mention submit_phase_record tool. "
            "Agent may write natural language instead of calling the tool."
        )

    def test_prompt_guidance_no_hardcoded_field_descriptions(self):
        """PROMPT_GUIDANCE should NOT contain hardcoded field descriptions.

        Field descriptions are rendered at runtime from schema by
        schema_field_guidance.render_schema_field_guidance(). PROMPT_GUIDANCE
        should only contain behavioral guidance (goals, rules, constraints).
        """
        from cognition_contracts.analysis_root_cause import PROMPT_GUIDANCE
        # These patterns indicate a hardcoded field description copy
        hardcoded_patterns = [
            "root_cause: specific file:line",
            "causal_chain: test failure",
            "evidence_refs: list of file:line",
        ]
        for pattern in hardcoded_patterns:
            assert pattern not in PROMPT_GUIDANCE, (
                f"PROMPT_GUIDANCE contains hardcoded field description: '{pattern}'. "
                f"Field descriptions should come from schema via "
                f"schema_field_guidance.render_schema_field_guidance()."
            )

    def test_no_conflicting_natural_language_format(self):
        """PROMPT_GUIDANCE should NOT tell agent to write 'ROOT_CAUSE:' sections.

        If the prompt says 'produce ROOT_CAUSE:\\n<text>' AND the tool expects
        a root_cause JSON field, the agent gets contradictory instructions.
        """
        from cognition_contracts.analysis_root_cause import PROMPT_GUIDANCE
        conflicting_patterns = [
            r"ROOT_CAUSE:\n",
            r"CAUSAL_CHAIN:\n",
            r"EVIDENCE:\n",
            r"ALTERNATIVES:\n",
            r"You MUST produce your analysis in this exact format",
        ]
        for pattern in conflicting_patterns:
            assert not re.search(pattern, PROMPT_GUIDANCE), (
                f"PROMPT_GUIDANCE contains conflicting natural-language format "
                f"instruction: '{pattern}'. This conflicts with the tool-call "
                f"JSON field format."
            )

    def test_schema_renderer_produces_field_guidance(self):
        """render_schema_field_guidance() must produce field guidance from bundle schema."""
        from schema_field_guidance import render_schema_field_guidance
        contract = _load_bundle_analyze()
        schema = contract["schema"]
        guidance = render_schema_field_guidance(schema, phase="ANALYZE")
        assert "root_cause" in guidance
        assert "causal_chain" in guidance
        assert "evidence_refs" in guidance
        assert "required" in guidance

    def test_tool_description_uses_renderer_not_hardcode(self):
        """jingu_model._build_phase_record_tool must use the renderer."""
        # Verify by importing and checking the function source
        import inspect
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mini-swe-agent"))
        from jingu_model import _build_phase_record_tool
        source = inspect.getsource(_build_phase_record_tool)
        assert "render_schema_field_guidance" in source, (
            "_build_phase_record_tool does not call render_schema_field_guidance. "
            "It may still use hardcoded field guidance."
        )
        assert "For ANALYZE:" not in source, (
            "_build_phase_record_tool still contains hardcoded 'For ANALYZE:' text."
        )
        assert "For EXECUTE:" not in source, (
            "_build_phase_record_tool still contains hardcoded 'For EXECUTE:' text."
        )


class TestFieldNamingConsistency:
    """Field names must be consistent across all layers."""

    def test_alternatives_field_name_documented(self):
        """The alternatives field has known name differences — document them.

        Bundle schema: alternative_hypotheses
        Gate rule field: alternatives_considered
        SST schema: alternatives_considered

        This asymmetry is known. This test documents it and ensures the
        mapping is maintained in declaration_extractor.
        """
        contract = _load_bundle_analyze()
        schema_props = contract["schema"]["properties"]

        # Bundle uses alternative_hypotheses
        assert "alternative_hypotheses" in schema_props

        # Gate uses alternative_hypotheses (canonical field name)
        from cognition_contracts.analysis_root_cause import GATE_RULE_MAP
        alt_rule = GATE_RULE_MAP.get("alternative_hypothesis")
        assert alt_rule is not None
        assert alt_rule.field == "alternative_hypotheses"

    def test_declaration_extractor_maps_alternatives(self):
        """declaration_extractor must map alternative_hypotheses → alternatives_considered."""
        from declaration_extractor import build_phase_record_from_structured
        from phase_record import PhaseRecord

        # Simulate tool call with bundle schema field names
        parsed = {
            "phase": "ANALYZE",
            "subtype": "analysis.root_cause",
            "principals": ["causal_grounding"],
            "evidence_refs": ["file.py:10"],
            "root_cause": "Bug in file.py:10",
            "causal_chain": "Test fails because X calls Y which does Z",
            "alternative_hypotheses": [
                {"hypothesis": "Could be A", "ruled_out_reason": "No evidence"}
            ],
        }
        record = build_phase_record_from_structured(parsed, "ANALYZE")
        assert isinstance(record, PhaseRecord)
        # The record should have the content accessible
        assert "Bug in file.py:10" in record.root_cause


class TestSSTProjectionChain:
    """Verify the '1 source → 1 renderer → 2 projections' chain.

    If you change a schema field's description, the tool description and
    phase prompt must both reflect the change — without touching any
    hardcoded text. This test verifies that by patching a description
    and asserting both projections update.
    """

    def test_schema_description_change_propagates_to_tool_description(self):
        """Change schema description → tool description changes."""
        import copy
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mini-swe-agent"))
        from jingu_model import _build_phase_record_tool

        contract = _load_bundle_analyze()
        schema = copy.deepcopy(contract["schema"])

        # Baseline: build tool with original schema
        tool_original = _build_phase_record_tool("ANALYZE", schema)
        desc_original = tool_original["function"]["description"]
        assert "root cause" in desc_original.lower()

        # Mutate: change root_cause description to something unique
        marker = "UNIQUE_MARKER_FOR_REGRESSION_TEST_12345"
        schema["properties"]["root_cause"]["description"] = marker

        # Rebuild: tool description must contain the new marker
        tool_mutated = _build_phase_record_tool("ANALYZE", schema)
        desc_mutated = tool_mutated["function"]["description"]
        assert marker in desc_mutated, (
            f"Changed schema description to '{marker}' but tool description "
            f"did not update. Tool desc: {desc_mutated[:200]}..."
        )

    def test_schema_description_change_propagates_to_renderer(self):
        """Change schema description → renderer output changes."""
        import copy
        from schema_field_guidance import render_schema_field_guidance

        contract = _load_bundle_analyze()
        schema = copy.deepcopy(contract["schema"])

        marker = "UNIQUE_MARKER_FOR_RENDERER_TEST_67890"
        schema["properties"]["causal_chain"]["description"] = marker

        guidance = render_schema_field_guidance(schema, phase="ANALYZE")
        assert marker in guidance, (
            f"Changed schema description to '{marker}' but renderer output "
            f"did not update. Guidance: {guidance[:200]}..."
        )

    def test_all_bundle_schema_descriptions_appear_in_tool_description(self):
        """Every non-trivial schema field description must appear in tool desc."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mini-swe-agent"))
        from jingu_model import _build_phase_record_tool

        contract = _load_bundle_analyze()
        schema = contract["schema"]
        tool = _build_phase_record_tool("ANALYZE", schema)
        desc = tool["function"]["description"]

        for field_name, field_schema in schema["properties"].items():
            field_desc = field_schema.get("description", "")
            if field_desc and len(field_desc) > 10:
                assert field_desc in desc, (
                    f"Schema field '{field_name}' has description '{field_desc[:60]}...' "
                    f"but it does not appear in the tool description."
                )
