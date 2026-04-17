"""
test_registries.py — Tests for SST canonical registries.

Verifies:
1. canonical_symbols: phase advance, normalization, phase→subtype mapping
2. strategy_registry: validation, normalization, prompt fragment
3. contract_registry: contract loading, accessor functions, cache
4. SST invariant: consumers derive from registries, no local copies
"""

import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))


# ── Phase Registry (canonical_symbols extensions) ────────────────────────────

class TestPhaseAdvance:
    def test_advance_table_has_all_phases(self):
        from canonical_symbols import PHASE_ADVANCE, ALL_PHASES
        for phase in ALL_PHASES:
            assert phase in PHASE_ADVANCE

    def test_judge_is_terminal(self):
        from canonical_symbols import PHASE_ADVANCE
        assert PHASE_ADVANCE["JUDGE"] is None

    def test_default_next_phase(self):
        from canonical_symbols import default_next_phase
        assert default_next_phase("ANALYZE") == "DECIDE"
        assert default_next_phase("EXECUTE") == "JUDGE"
        assert default_next_phase("JUDGE") is None

    def test_default_next_phase_invalid(self):
        from canonical_symbols import default_next_phase
        with pytest.raises(TypeError):
            default_next_phase("BOGUS")


class TestPhaseNormalization:
    def test_canonical_passthrough(self):
        from canonical_symbols import normalize_phase
        assert normalize_phase("ANALYZE") == "ANALYZE"

    def test_uppercase_passthrough(self):
        from canonical_symbols import normalize_phase
        assert normalize_phase("analyze") == "ANALYZE"

    def test_gerund_form(self):
        from canonical_symbols import normalize_phase
        assert normalize_phase("EXECUTION") == "EXECUTE"
        assert normalize_phase("OBSERVATION") == "OBSERVE"
        assert normalize_phase("ANALYSIS") == "ANALYZE"

    def test_lowercase_alias(self):
        from canonical_symbols import normalize_phase
        assert normalize_phase("observation") == "OBSERVE"
        assert normalize_phase("validation") == "JUDGE"
        assert normalize_phase("planning") == "DESIGN"

    def test_unknown_raises(self):
        from canonical_symbols import normalize_phase
        with pytest.raises(TypeError):
            normalize_phase("BOGUS_PHASE")

    def test_is_valid_phase(self):
        from canonical_symbols import is_valid_phase
        assert is_valid_phase("ANALYZE") is True
        assert is_valid_phase("analyze") is False
        assert is_valid_phase("EXECUTION") is False


class TestPhaseToSubtype:
    def test_all_subtypes_mapped(self):
        from canonical_symbols import PHASE_TO_SUBTYPE, ALL_SUBTYPES
        mapped_subtypes = set(PHASE_TO_SUBTYPE.values())
        assert mapped_subtypes == set(ALL_SUBTYPES)

    def test_understand_has_no_subtype(self):
        from canonical_symbols import PHASE_TO_SUBTYPE
        assert "UNDERSTAND" not in PHASE_TO_SUBTYPE


# ── Strategy Registry ────────────────────────────────────────────────────────

class TestStrategyRegistry:
    def test_all_strategies_nonempty(self):
        from strategy_registry import all_strategies
        strategies = all_strategies()
        assert len(strategies) >= 7
        assert "REGEX_FIX" in strategies

    def test_is_valid_strategy_case_insensitive(self):
        from strategy_registry import is_valid_strategy
        assert is_valid_strategy("REGEX_FIX") is True
        assert is_valid_strategy("regex_fix") is True
        assert is_valid_strategy("Regex_Fix") is True

    def test_is_valid_strategy_invalid(self):
        from strategy_registry import is_valid_strategy
        assert is_valid_strategy("BOGUS") is False
        assert is_valid_strategy("") is False
        assert is_valid_strategy(None) is False

    def test_normalize_strategy(self):
        from strategy_registry import normalize_strategy
        assert normalize_strategy("regex_fix") == "REGEX_FIX"
        assert normalize_strategy("DATAFLOW_FIX") == "DATAFLOW_FIX"

    def test_normalize_strategy_invalid(self):
        from strategy_registry import normalize_strategy
        with pytest.raises(ValueError):
            normalize_strategy("BOGUS")

    def test_validate_record_strategy(self):
        from strategy_registry import validate_record_strategy
        ok, val = validate_record_strategy({"repair_strategy_type": "regex_fix"})
        assert ok is True
        assert val == "REGEX_FIX"

        ok, msg = validate_record_strategy({"repair_strategy_type": ""})
        assert ok is False

        ok, msg = validate_record_strategy({})
        assert ok is False

    def test_prompt_fragment(self):
        from strategy_registry import strategy_prompt_fragment
        frag = strategy_prompt_fragment()
        assert "REGEX_FIX" in frag
        assert "|" in frag


# ── Contract Registry ────────────────────────────────────────────────────────

class TestContractRegistry:
    def test_get_by_phase(self):
        from contract_registry import get_contract_by_phase
        c = get_contract_by_phase("ANALYZE")
        assert c is not None
        assert c.phase == "ANALYZE"
        assert c.subtype == "analysis.root_cause"

    def test_get_by_subtype(self):
        from contract_registry import get_contract_by_subtype
        c = get_contract_by_subtype("analysis.root_cause")
        assert c.phase == "ANALYZE"

    def test_get_by_phase_understand_returns_none(self):
        from contract_registry import get_contract_by_phase
        assert get_contract_by_phase("UNDERSTAND") is None

    def test_required_principals(self):
        from contract_registry import get_required_principals
        rp = get_required_principals("ANALYZE")
        assert "causal_grounding" in rp
        assert "evidence_linkage" in rp

    def test_field_specs(self):
        from contract_registry import get_field_specs
        specs = get_field_specs("ANALYZE")
        names = [s.name for s in specs]
        assert "root_cause" in names
        assert "causal_chain" in names

    def test_gate_rules(self):
        from contract_registry import get_gate_rules
        rules = get_gate_rules("ANALYZE")
        names = [r.name for r in rules]
        assert "code_grounding" in names

    def test_all_contracts(self):
        from contract_registry import all_contracts
        contracts = all_contracts()
        assert len(contracts) >= 6
        assert "analysis.root_cause" in contracts
        assert "execution.code_patch" in contracts

    def test_cache_clear(self):
        from contract_registry import get_contract_by_phase, clear_cache, _CONTRACT_CACHE
        get_contract_by_phase("ANALYZE")
        assert len(_CONTRACT_CACHE) > 0
        clear_cache()
        assert len(_CONTRACT_CACHE) == 0


# ── SST Invariant: consumers derive from registries ─────────────────────────

class TestSSTInvariant:
    def test_phase_lifecycle_uses_canonical_advance(self):
        """phase_lifecycle._DEFAULT_ADVANCE is the same object as PHASE_ADVANCE."""
        from phase_lifecycle import _DEFAULT_ADVANCE
        from canonical_symbols import PHASE_ADVANCE
        assert _DEFAULT_ADVANCE is PHASE_ADVANCE

    def test_reasoning_state_uses_canonical_advance(self):
        """reasoning_state._ADVANCE_TABLE is the same object as PHASE_ADVANCE."""
        from control.reasoning_state import _ADVANCE_TABLE
        from canonical_symbols import PHASE_ADVANCE
        assert _ADVANCE_TABLE is PHASE_ADVANCE

    def test_declaration_extractor_uses_canonical_aliases(self):
        """declaration_extractor._PHASE_NORM is the same object as _PHASE_ALIASES."""
        from declaration_extractor import _PHASE_NORM
        from canonical_symbols import _PHASE_ALIASES
        assert _PHASE_NORM is _PHASE_ALIASES
