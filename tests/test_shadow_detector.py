"""
test_shadow_detector.py — Tests for shadow_detector AST-based scans.

Uses tmp_path fixtures to create synthetic Python files and verify
that each scan type detects the expected violations.
"""

import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from shadow_detector import (
    ShadowContractViolation,
    scan_file,
    scan_gate_private_fields,
    scan_regex_semantic_checks,
    scan_prompt_only_fields,
    scan_hardcoded_principals,
)


class TestScanGatePrivateFields:
    """Scan 1: detect dict/set/list literals defining non-contract fields."""

    def test_detect_gate_private_dict(self, tmp_path):
        gate_file = tmp_path / "test_gate.py"
        gate_file.write_text(
            '_MY_CONTRACT = {"ghost_field": {"type": "string"}}\n'
        )
        violations = scan_gate_private_fields(
            str(gate_file), contract_fields={"root_cause", "causal_chain"}
        )
        assert len(violations) >= 1
        assert any(v.item == "ghost_field" for v in violations)
        assert all(v.violation_type == "gate_private_field" for v in violations)

    def test_no_violation_for_known_fields(self, tmp_path):
        gate_file = tmp_path / "test_gate.py"
        gate_file.write_text(
            '_MY_FIELDS = {"root_cause": {"type": "string"}}\n'
        )
        violations = scan_gate_private_fields(
            str(gate_file), contract_fields={"root_cause"}
        )
        assert violations == []

    def test_detect_set_literal(self, tmp_path):
        gate_file = tmp_path / "test_gate.py"
        gate_file.write_text(
            '_REQUIRED_FIELDS = {"root_cause", "phantom_field"}\n'
        )
        violations = scan_gate_private_fields(
            str(gate_file), contract_fields={"root_cause"}
        )
        assert len(violations) == 1
        assert violations[0].item == "phantom_field"

    def test_detect_list_literal(self, tmp_path):
        gate_file = tmp_path / "test_gate.py"
        gate_file.write_text(
            '_SCHEMA_FIELDS = ["known", "unknown_field"]\n'
        )
        violations = scan_gate_private_fields(
            str(gate_file), contract_fields={"known"}
        )
        assert len(violations) == 1
        assert violations[0].item == "unknown_field"

    def test_ignores_non_matching_variable_names(self, tmp_path):
        gate_file = tmp_path / "test_gate.py"
        gate_file.write_text(
            'MY_DICT = {"ghost": "value"}\n'  # no _FIELDS/_CONTRACT etc in name
        )
        violations = scan_gate_private_fields(
            str(gate_file), contract_fields=set()
        )
        assert violations == []


class TestScanRegexSemanticChecks:
    """Scan 2: detect regex usage in gate files."""

    def test_detect_re_compile(self, tmp_path):
        gate_file = tmp_path / "test_gate.py"
        gate_file.write_text(
            'import re\npattern = re.compile(r"because.*hypothesis")\n'
        )
        violations = scan_regex_semantic_checks(str(gate_file))
        assert len(violations) == 1
        assert violations[0].violation_type == "regex_semantic_check"
        assert "re.compile()" in violations[0].item

    def test_detect_re_search(self, tmp_path):
        gate_file = tmp_path / "test_gate.py"
        gate_file.write_text(
            'import re\nresult = re.search(r"option\\s*1", text)\n'
        )
        violations = scan_regex_semantic_checks(str(gate_file))
        assert len(violations) == 1

    def test_no_violation_without_re(self, tmp_path):
        gate_file = tmp_path / "test_gate.py"
        gate_file.write_text('x = len("hello")\n')
        violations = scan_regex_semantic_checks(str(gate_file))
        assert violations == []


class TestScanPromptOnlyFields:
    """Scan 3: detect field-like tokens in prompt strings not in schema."""

    def test_detect_prompt_only_field(self, tmp_path):
        prompt_file = tmp_path / "phase_prompt.py"
        prompt_file.write_text(
            'GUIDANCE = "You must provide the ghost_evidence_field and explain your reasoning thoroughly in at least 30 characters."\n'
        )
        violations = scan_prompt_only_fields(
            str(prompt_file), contract_fields={"root_cause"}
        )
        ghost = [v for v in violations if v.item == "ghost_evidence_field"]
        assert len(ghost) == 1
        assert ghost[0].violation_type == "prompt_only_field"

    def test_no_violation_for_known_fields(self, tmp_path):
        prompt_file = tmp_path / "phase_prompt.py"
        prompt_file.write_text(
            'GUIDANCE = "You must provide the root_cause field with sufficient detail for causal analysis."\n'
        )
        violations = scan_prompt_only_fields(
            str(prompt_file), contract_fields={"root_cause"}
        )
        assert violations == []


class TestScanHardcodedPrincipals:
    """Scan 4: detect principal name literals outside authorized files."""

    def test_detect_hardcoded_principal(self, tmp_path):
        src_file = tmp_path / "some_module.py"
        src_file.write_text('p = "causal_grounding"\n')
        violations = scan_hardcoded_principals(
            str(src_file), principal_names={"causal_grounding"}
        )
        assert len(violations) == 1
        assert violations[0].item == "causal_grounding"
        assert violations[0].violation_type == "hardcoded_principal"

    def test_authorized_file_exempt(self, tmp_path):
        """principal_gate.py is an authorized consumer — no violations."""
        auth_file = tmp_path / "principal_gate.py"
        auth_file.write_text('p = "causal_grounding"\n')
        violations = scan_hardcoded_principals(
            str(auth_file), principal_names={"causal_grounding"}
        )
        assert violations == []

    def test_no_violation_for_unknown_string(self, tmp_path):
        src_file = tmp_path / "some_module.py"
        src_file.write_text('p = "not_a_principal"\n')
        violations = scan_hardcoded_principals(
            str(src_file), principal_names={"causal_grounding"}
        )
        assert violations == []


class TestScanFile:
    """Tests for the scan_file orchestrator."""

    def test_gate_file_gets_scanned(self, tmp_path):
        """Gate files trigger scan 1 + 2."""
        gate_file = tmp_path / "analysis_gate.py"
        gate_file.write_text(
            'import re\n'
            '_MY_FIELDS = {"ghost": "x"}\n'
            'p = re.compile(r"test")\n'
        )
        violations = scan_file(
            str(gate_file),
            contract_fields={"root_cause"},
            principal_names=set(),
        )
        types = {v.violation_type for v in violations}
        assert "gate_private_field" in types
        assert "regex_semantic_check" in types

    def test_prompt_file_gets_scanned(self, tmp_path):
        """Prompt files trigger scan 3."""
        prompt_file = tmp_path / "phase_prompt.py"
        prompt_file.write_text(
            'TEXT = "Provide the ghost_evidence_field with detailed explanation in at least 30 characters."\n'
        )
        violations = scan_file(
            str(prompt_file),
            contract_fields={"root_cause"},
            principal_names=set(),
        )
        prompt_violations = [
            v for v in violations if v.violation_type == "prompt_only_field"
        ]
        assert len(prompt_violations) >= 1

    def test_regular_file_only_principal_scan(self, tmp_path):
        """Non-gate, non-prompt files only get scan 4 (hardcoded principals)."""
        src_file = tmp_path / "utils.py"
        src_file.write_text(
            '_MY_FIELDS = {"ghost": "x"}\n'
            'p = "causal_grounding"\n'
        )
        violations = scan_file(
            str(src_file),
            contract_fields={"root_cause"},
            principal_names={"causal_grounding"},
        )
        # Should NOT have gate_private_field (not a gate file)
        types = {v.violation_type for v in violations}
        assert "gate_private_field" not in types
        # Should have hardcoded_principal
        assert "hardcoded_principal" in types
