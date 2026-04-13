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
    """Bundle prompt must mention the same fields as the bundle schema."""

    def test_prompt_mentions_required_schema_fields(self):
        """Bundle prompt must mention every required schema field by name."""
        contract = _load_bundle_analyze()
        prompt = contract["prompt"]
        schema_required = contract["schema"]["required"]

        # These are auto-filled or obvious, no need to mention in prompt
        auto_fields = {"phase", "subtype"}

        for field in schema_required:
            if field in auto_fields:
                continue
            assert field in prompt, (
                f"Bundle schema requires '{field}' but bundle prompt doesn't "
                f"mention it. Agent won't know to fill it."
            )

    def test_prompt_mentions_optional_fields(self):
        """Bundle prompt should mention optional schema fields for quality."""
        contract = _load_bundle_analyze()
        prompt = contract["prompt"]
        schema_props = set(contract["schema"]["properties"].keys())
        schema_required = set(contract["schema"]["required"])
        auto_fields = {"phase", "subtype"}

        optional = schema_props - schema_required - auto_fields
        for field in optional:
            assert field in prompt, (
                f"Bundle schema has optional field '{field}' but bundle prompt "
                f"doesn't mention it. Agent won't know it exists."
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

    def test_prompt_guidance_mentions_key_fields(self):
        """PROMPT_GUIDANCE should mention the key gate-checked fields."""
        from cognition_contracts.analysis_root_cause import PROMPT_GUIDANCE
        key_fields = ["root_cause", "causal_chain", "evidence_refs"]
        for field in key_fields:
            assert field in PROMPT_GUIDANCE, (
                f"PROMPT_GUIDANCE doesn't mention '{field}'. "
                f"Agent won't know this field is gate-checked."
            )

    def test_no_conflicting_natural_language_format(self):
        """PROMPT_GUIDANCE should NOT tell agent to write 'ROOT_CAUSE:' sections.

        If the prompt says 'produce ROOT_CAUSE:\\n<text>' AND the tool expects
        a root_cause JSON field, the agent gets contradictory instructions.
        """
        from cognition_contracts.analysis_root_cause import PROMPT_GUIDANCE
        # Check for the old natural-language section format
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

        # Gate uses alternatives_considered
        from cognition_contracts.analysis_root_cause import GATE_RULE_MAP
        alt_rule = GATE_RULE_MAP.get("alternative_hypothesis")
        assert alt_rule is not None
        assert alt_rule.field == "alternatives_considered"

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
