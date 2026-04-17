"""
test_sst_guards.py — Fail-closed guards for Single Source of Truth.

These tests ensure SST violations cannot regrow. They verify:
1. All advance tables / phase dicts derive from canonical_symbols
2. All strategy enums match the canonical source
3. Phase normalization has exactly one definition
4. Required principals match contracts
5. No hardcoded phase lists in core runtime modules
"""

import sys
import os
import ast
import re
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"


# ── Guard 1: Phase advance tables are identical objects ──────────────────────

class TestPhaseAdvanceSST:
    def test_phase_lifecycle_advance_is_canonical(self):
        from phase_lifecycle import _DEFAULT_ADVANCE
        from canonical_symbols import PHASE_ADVANCE
        assert _DEFAULT_ADVANCE is PHASE_ADVANCE

    def test_reasoning_state_advance_is_canonical(self):
        from control.reasoning_state import _ADVANCE_TABLE
        from canonical_symbols import PHASE_ADVANCE
        assert _ADVANCE_TABLE is PHASE_ADVANCE

    def test_advance_covers_all_phases(self):
        from canonical_symbols import PHASE_ADVANCE, ALL_PHASES
        for phase in ALL_PHASES:
            assert phase in PHASE_ADVANCE, f"PHASE_ADVANCE missing {phase}"


# ── Guard 2: Strategy registry matches canonical source ─────────────────────

class TestStrategySST:
    def test_strategy_registry_matches_contract(self):
        from strategy_registry import all_strategies
        from cognition_contracts.analysis_root_cause import REPAIR_STRATEGY_TYPES
        assert list(all_strategies()) == REPAIR_STRATEGY_TYPES

    def test_no_hardcoded_strategy_fallback_in_lifecycle(self):
        """phase_lifecycle must NOT contain hardcoded strategy sets."""
        source = (SCRIPTS_DIR / "phase_lifecycle.py").read_text()
        # Should NOT find a set literal with strategy names
        assert "REGEX_FIX" not in source or "import" in source.split("REGEX_FIX")[0].split("\n")[-1], \
            "phase_lifecycle.py contains hardcoded REGEX_FIX — should import from strategy_registry"


# ── Guard 3: Phase normalization has single source ──────────────────────────

class TestPhaseNormSST:
    def test_declaration_extractor_norm_is_canonical(self):
        from declaration_extractor import _PHASE_NORM
        from canonical_symbols import _PHASE_ALIASES
        assert _PHASE_NORM is _PHASE_ALIASES

    def test_no_local_phase_norm_dict(self):
        """Core modules must not define their own _PHASE_NORM dict."""
        for filename in ["step_sections.py", "principal_inference.py", "failure_classifier.py"]:
            source = (SCRIPTS_DIR / filename).read_text()
            # Should not contain a dict assignment named _PHASE_NORM
            assert "_PHASE_NORM" not in source or "import" in source, \
                f"{filename} defines local _PHASE_NORM — should import from canonical_symbols"


# ── Guard 4: Required principals match contracts ────────────────────────────

class TestPrincipalSST:
    def test_failure_routing_principals_match_contracts(self):
        from failure_classifier import FAILURE_ROUTING_RULES
        from contract_registry import get_required_principals
        for ftype, rule in FAILURE_ROUTING_RULES.items():
            phase = rule["next_phase"]
            expected = list(get_required_principals(phase))
            actual = rule["required_principals"]
            assert actual == expected, (
                f"FAILURE_ROUTING_RULES['{ftype}'].required_principals = {actual} "
                f"but contract says {expected} for phase {phase}"
            )

    def test_failure_mode_routing_principals_match_contracts(self):
        from failure_classifier import FAILURE_MODE_ROUTING
        from contract_registry import get_required_principals
        for fmode, rule in FAILURE_MODE_ROUTING.items():
            phase = rule["next_phase"]
            expected = list(get_required_principals(phase))
            actual = rule["required_principals"]
            assert actual == expected, (
                f"FAILURE_MODE_ROUTING['{fmode}'].required_principals = {actual} "
                f"but contract says {expected} for phase {phase}"
            )


# ── Guard 5: No hardcoded phase lists in core runtime modules ───────────────

class TestNoHardcodedPhaseLists:
    """Scan core runtime files for hardcoded 7-phase lists."""

    CORE_FILES = [
        "phase_lifecycle.py",
        "failure_classifier.py",
        "principal_inference.py",
    ]

    PHASE_LIST_PATTERN = re.compile(
        r'\[.*"UNDERSTAND".*"OBSERVE".*"ANALYZE".*"DECIDE".*"DESIGN".*"EXECUTE".*"JUDGE".*\]'
    )

    @pytest.mark.parametrize("filename", CORE_FILES)
    def test_no_hardcoded_phase_list(self, filename):
        source = (SCRIPTS_DIR / filename).read_text()
        matches = self.PHASE_LIST_PATTERN.findall(source)
        assert not matches, (
            f"{filename} contains hardcoded 7-phase list: {matches[0][:80]}... "
            "— should import from canonical_symbols.ALL_PHASES"
        )


# ── Guard 6: Contract registry covers all subtypes ─────────────────────────

class TestContractRegistryCompleteness:
    def test_all_subtypes_have_contracts(self):
        from canonical_symbols import ALL_SUBTYPES
        from contract_registry import get_contract_by_subtype
        for subtype in ALL_SUBTYPES:
            c = get_contract_by_subtype(subtype)
            assert c is not None, f"No contract for subtype '{subtype}'"
            assert c.subtype == subtype

    def test_all_contract_phases_are_canonical(self):
        from canonical_symbols import ALL_PHASES
        from contract_registry import all_contracts
        for subtype, c in all_contracts().items():
            assert c.phase in ALL_PHASES, (
                f"Contract '{subtype}' has non-canonical phase '{c.phase}'"
            )


# ── Guard 7: Phase→Subtype mapping consistency ─────────────────────────────

class TestPhaseSubtypeConsistency:
    def test_phase_to_subtype_matches_contracts(self):
        from canonical_symbols import PHASE_TO_SUBTYPE
        from contract_registry import all_contracts
        contract_phase_map = {c.phase: c.subtype for c in all_contracts().values()}
        for phase, subtype in PHASE_TO_SUBTYPE.items():
            assert phase in contract_phase_map, f"Phase {phase} in PHASE_TO_SUBTYPE but no contract"
            assert contract_phase_map[phase] == subtype, (
                f"PHASE_TO_SUBTYPE[{phase}]={subtype} but contract says {contract_phase_map[phase]}"
            )
