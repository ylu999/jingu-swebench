"""Tests for SDG repair prompt generation (p217 w1-04).

Verifies that GateRejection objects produce repair hints containing
field names, hints, and contract information.
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from gate_rejection import (
    GateRejection, ContractView, FieldSpec, FieldFailure,
    build_gate_rejection, build_repair_from_rejection,
)
from repair_prompts import build_sdg_repair_prompt


def _make_analysis_rejection():
    """Build a sample analysis gate rejection for testing."""
    contract = ContractView(
        required_fields=["root_cause", "causal_chain", "evidence_refs"],
        field_specs={
            "root_cause": FieldSpec(
                description="Identified root cause with specific code reference",
                required=True,
                min_length=10,
                semantic_check="grounded_in_code",
            ),
            "causal_chain": FieldSpec(
                description="Causal chain: test failure -> condition -> code -> why",
                required=True,
                min_length=20,
                semantic_check="connects_test_to_code",
            ),
        },
    )
    failures = [
        FieldFailure(
            field="root_cause",
            reason="missing",
            hint="Point to exact code location causing the issue",
            expected="Identified root cause with specific code reference",
            actual=None,
        ),
        FieldFailure(
            field="causal_chain",
            reason="too_short",
            hint="Explain step-by-step: test failure -> condition -> code -> why",
            expected="Causal chain: test failure -> condition -> code -> why",
            actual="incomplete chain",
        ),
    ]
    return build_gate_rejection(
        gate_name="analysis_gate",
        contract=contract,
        extracted={"root_cause": "", "causal_chain": "incomplete chain"},
        failures=failures,
    )


class TestBuildRepairFromRejection(unittest.TestCase):
    """Test build_repair_from_rejection() output format."""

    def test_contains_gate_name(self):
        rej = _make_analysis_rejection()
        result = build_repair_from_rejection(rej)
        self.assertIn("analysis_gate", result)

    def test_contains_field_names(self):
        rej = _make_analysis_rejection()
        result = build_repair_from_rejection(rej)
        self.assertIn("root_cause", result)
        self.assertIn("causal_chain", result)

    def test_contains_hints(self):
        rej = _make_analysis_rejection()
        result = build_repair_from_rejection(rej)
        self.assertIn("Point to exact code location", result)
        self.assertIn("step-by-step", result)

    def test_contains_expected(self):
        rej = _make_analysis_rejection()
        result = build_repair_from_rejection(rej)
        self.assertIn("Identified root cause", result)

    def test_non_empty(self):
        rej = _make_analysis_rejection()
        result = build_repair_from_rejection(rej)
        self.assertTrue(len(result.strip()) > 0)


class TestBuildSdgRepairPrompt(unittest.TestCase):
    """Test build_sdg_repair_prompt() wrapper in repair_prompts.py."""

    def test_with_real_rejection(self):
        rej = _make_analysis_rejection()
        result = build_sdg_repair_prompt(rej)
        self.assertIn("analysis_gate", result)
        self.assertIn("root_cause", result)

    def test_contains_failure_field_names(self):
        """Verify field names from FieldFailure appear in repair prompt."""
        rej = _make_analysis_rejection()
        result = build_sdg_repair_prompt(rej)
        for f in rej.failures:
            self.assertIn(f.field, result, f"Field '{f.field}' missing from repair prompt")

    def test_fallback_on_none_rejection(self):
        """build_sdg_repair_prompt with a mock object that has no failures."""
        class FakeRejection:
            gate_name = "test_gate"
            failures = []
        result = build_sdg_repair_prompt(FakeRejection())
        self.assertIn("test_gate", result)


class TestAdmissionRejectionRepair(unittest.TestCase):
    """Test SDG repair for principal_gate rejection."""

    def test_principal_gate_rejection(self):
        contract = ContractView(
            required_fields=["principals"],
            field_specs={
                "principals": FieldSpec(
                    description="Required principals for EXECUTE phase",
                    required=True,
                ),
            },
        )
        failures = [
            FieldFailure(
                field="principals",
                reason="missing",
                hint="Declare minimal_change principal",
                expected="Required principals for EXECUTE phase",
                actual="declared=[]",
            ),
        ]
        rej = build_gate_rejection(
            gate_name="principal_gate",
            contract=contract,
            extracted={"declared": [], "required": ["minimal_change"]},
            failures=failures,
        )
        result = build_repair_from_rejection(rej)
        self.assertIn("principal_gate", result)
        self.assertIn("principals", result)
        self.assertIn("minimal_change", result)


if __name__ == "__main__":
    unittest.main()
