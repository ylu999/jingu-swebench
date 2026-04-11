"""Tests for p216 failure routing engine and strategy prompts (w3-04).

Tests cover:
  - failure_routing: route_failure, get_routing_entry, load_matrix, seed matrix
  - strategy_prompts: get_strategy_prompt, STRATEGY_PROMPTS coverage
  - Feature flag: is_data_driven_routing_enabled
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Add scripts/ to sys.path so imports work
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from failure_routing import (
    SEED_FAILURE_MATRIX,
    get_routing_entry,
    is_data_driven_routing_enabled,
    load_matrix,
    route_failure,
)
from strategy_prompts import (
    STRATEGY_PROMPTS,
    get_strategy_prompt,
)


# ── Tests: route_failure ─────────────────────────────────────────────────


class TestRouteFailure:

    def test_exact_match(self):
        """Exact (phase, principal) should return the seed entry."""
        next_phase, strategy = route_failure("ANALYZE", "causal_grounding")
        assert next_phase == "ANALYZE"
        assert strategy == "complete_causal_chain"

    def test_exact_match_execute(self):
        """EXECUTE phase exact match."""
        next_phase, strategy = route_failure("EXECUTE", "execution_correctness")
        assert next_phase == "EXECUTE"
        assert strategy == "fix_execution_errors"

    def test_exact_match_design(self):
        """DESIGN phase exact match."""
        next_phase, strategy = route_failure("DESIGN", "option_comparison")
        assert next_phase == "DESIGN"
        assert strategy == "compare_alternatives"

    def test_exact_match_judge(self):
        """JUDGE phase exact match."""
        next_phase, strategy = route_failure("JUDGE", "result_verification")
        assert next_phase == "EXECUTE"
        assert strategy == "verify_test_coverage"

    def test_wildcard_fallback(self):
        """Unknown principal should fall back to phase wildcard."""
        next_phase, strategy = route_failure("ANALYZE", "unknown_principal_xyz")
        assert next_phase == "ANALYZE"
        assert strategy == "rethink_root_cause"

    def test_wildcard_execute(self):
        """EXECUTE wildcard."""
        next_phase, strategy = route_failure("EXECUTE", "unknown_principal_xyz")
        assert next_phase == "ANALYZE"
        assert strategy == "realign_with_analysis"

    def test_default_fallback(self):
        """Completely unknown phase should use default."""
        next_phase, strategy = route_failure("UNKNOWN_PHASE", "unknown_principal")
        assert next_phase == "UNKNOWN_PHASE"
        assert strategy == "rethink_root_cause"

    def test_case_insensitive_phase(self):
        """Phase should be normalized to uppercase."""
        next_phase, strategy = route_failure("analyze", "causal_grounding")
        assert next_phase == "ANALYZE"
        assert strategy == "complete_causal_chain"

    def test_all_seed_entries_routable(self):
        """Every seed matrix entry should be routable."""
        for key, entry in SEED_FAILURE_MATRIX.items():
            if key.endswith(":*"):
                continue
            phase, principal = key.split(":", 1)
            next_phase, strategy = route_failure(phase, principal)
            assert next_phase == entry["next_phase"]
            assert strategy == entry["strategy"]


# ── Tests: get_routing_entry ─────────────────────────────────────────────


class TestGetRoutingEntry:

    def test_exact_match_returns_full_entry(self):
        """Full entry should include confidence and sample_count."""
        entry = get_routing_entry("ANALYZE", "causal_grounding")
        assert entry["next_phase"] == "ANALYZE"
        assert entry["strategy"] == "complete_causal_chain"
        assert "confidence" in entry
        assert "sample_count" in entry

    def test_wildcard_returns_entry(self):
        """Wildcard match should return the wildcard entry."""
        entry = get_routing_entry("ANALYZE", "nonexistent_principal")
        assert entry["next_phase"] == "ANALYZE"
        assert entry["strategy"] == "rethink_root_cause"
        assert entry["confidence"] == 0.4

    def test_default_entry(self):
        """Unknown phase+principal should return default entry."""
        entry = get_routing_entry("UNKNOWN", "unknown")
        assert entry["next_phase"] == "UNKNOWN"
        assert entry["strategy"] == "rethink_root_cause"
        assert entry["confidence"] == 0.0
        assert entry["sample_count"] == 0


# ── Tests: load_matrix / data-driven routing ─────────────────────────────


class TestLoadMatrix:

    def test_load_merges_with_seed(self, tmp_path):
        """Data-driven matrix should override seed entries."""
        data_matrix = {
            "ANALYZE:causal_grounding": {
                "next_phase": "DESIGN",
                "strategy": "compare_alternatives",
                "confidence": 0.9,
                "sample_count": 50,
            },
            "NEW_PHASE:new_principal": {
                "next_phase": "ANALYZE",
                "strategy": "gather_code_evidence",
                "confidence": 0.6,
                "sample_count": 10,
            },
        }
        matrix_file = tmp_path / "matrix.json"
        matrix_file.write_text(json.dumps(data_matrix))

        merged = load_matrix(str(matrix_file))

        # Data-driven entry overrides seed
        assert merged["ANALYZE:causal_grounding"]["next_phase"] == "DESIGN"
        assert merged["ANALYZE:causal_grounding"]["confidence"] == 0.9

        # New entry added
        assert "NEW_PHASE:new_principal" in merged

        # Seed entries preserved
        assert "EXECUTE:execution_correctness" in merged

    def test_load_preserves_seed_when_no_overlap(self, tmp_path):
        """Non-overlapping data entries should not remove seed entries."""
        data_matrix = {
            "CUSTOM:custom_principal": {
                "next_phase": "ANALYZE",
                "strategy": "rethink_root_cause",
                "confidence": 0.5,
                "sample_count": 5,
            },
        }
        matrix_file = tmp_path / "matrix.json"
        matrix_file.write_text(json.dumps(data_matrix))

        merged = load_matrix(str(matrix_file))
        assert len(merged) == len(SEED_FAILURE_MATRIX) + 1


class TestDataDrivenRouting:

    def test_feature_flag_default_off(self):
        """DATA_DRIVEN_ROUTING should default to off."""
        os.environ.pop("DATA_DRIVEN_ROUTING", None)
        assert is_data_driven_routing_enabled() is False

    def test_feature_flag_on(self):
        """DATA_DRIVEN_ROUTING=true should enable."""
        os.environ["DATA_DRIVEN_ROUTING"] = "true"
        try:
            assert is_data_driven_routing_enabled() is True
        finally:
            os.environ.pop("DATA_DRIVEN_ROUTING", None)

    def test_feature_flag_case_insensitive(self):
        """Feature flag should be case-insensitive."""
        os.environ["DATA_DRIVEN_ROUTING"] = "True"
        try:
            assert is_data_driven_routing_enabled() is True
        finally:
            os.environ.pop("DATA_DRIVEN_ROUTING", None)

    def test_feature_flag_false(self):
        """Explicit false should disable."""
        os.environ["DATA_DRIVEN_ROUTING"] = "false"
        try:
            assert is_data_driven_routing_enabled() is False
        finally:
            os.environ.pop("DATA_DRIVEN_ROUTING", None)


# ── Tests: strategy_prompts ──────────────────────────────────────────────


class TestStrategyPrompts:

    def test_all_seed_strategies_have_prompts(self):
        """Every strategy in the seed matrix should have a prompt."""
        strategies_used = {
            entry["strategy"] for entry in SEED_FAILURE_MATRIX.values()
        }
        for strategy in strategies_used:
            prompt = get_strategy_prompt(strategy)
            assert prompt, f"No prompt for strategy: {strategy}"
            assert len(prompt) > 20, f"Prompt too short for: {strategy}"

    def test_known_strategy_returns_specific_prompt(self):
        """Known strategy should return its specific prompt."""
        prompt = get_strategy_prompt("complete_causal_chain")
        assert "[ROUTING: COMPLETE CAUSAL CHAIN]" in prompt
        assert "causal chain" in prompt.lower()

    def test_unknown_strategy_returns_generic(self):
        """Unknown strategy should return a generic fallback."""
        prompt = get_strategy_prompt("nonexistent_strategy_xyz")
        assert "[ROUTING: NONEXISTENT STRATEGY XYZ]" in prompt
        assert "different strategy" in prompt.lower()

    def test_all_prompts_non_empty(self):
        """Every prompt in STRATEGY_PROMPTS should be non-empty."""
        for name, prompt in STRATEGY_PROMPTS.items():
            assert prompt, f"Empty prompt for: {name}"
            assert len(prompt) > 20, f"Prompt too short for: {name}"

    def test_prompt_contains_routing_header(self):
        """Every prompt should start with [ROUTING: ...]."""
        for name, prompt in STRATEGY_PROMPTS.items():
            assert prompt.startswith("[ROUTING:"), (
                f"Prompt for {name} should start with [ROUTING:]"
            )

    def test_prompt_count(self):
        """Should have at least 10 strategy prompts."""
        assert len(STRATEGY_PROMPTS) >= 10

    def test_specific_strategies(self):
        """Spot-check specific strategy prompts."""
        assert "gather_code_evidence" in STRATEGY_PROMPTS
        assert "rethink_root_cause" in STRATEGY_PROMPTS
        assert "fix_execution_errors" in STRATEGY_PROMPTS
        assert "verify_test_coverage" in STRATEGY_PROMPTS
        assert "check_regressions" in STRATEGY_PROMPTS


# ── Tests: seed matrix structure ─────────────────────────────────────────


class TestSeedMatrix:

    def test_all_entries_have_required_fields(self):
        """Every seed entry must have next_phase, strategy, confidence, sample_count."""
        for key, entry in SEED_FAILURE_MATRIX.items():
            assert "next_phase" in entry, f"Missing next_phase in {key}"
            assert "strategy" in entry, f"Missing strategy in {key}"
            assert "confidence" in entry, f"Missing confidence in {key}"
            assert "sample_count" in entry, f"Missing sample_count in {key}"

    def test_seed_sample_count_is_zero(self):
        """Seed entries should have sample_count=0 (not data-derived)."""
        for key, entry in SEED_FAILURE_MATRIX.items():
            assert entry["sample_count"] == 0, f"{key} has non-zero sample_count"

    def test_wildcard_entries_exist(self):
        """Phase wildcards should exist for major phases."""
        assert "ANALYZE:*" in SEED_FAILURE_MATRIX
        assert "EXECUTE:*" in SEED_FAILURE_MATRIX
        assert "DESIGN:*" in SEED_FAILURE_MATRIX
        assert "JUDGE:*" in SEED_FAILURE_MATRIX

    def test_confidence_range(self):
        """Confidence should be between 0 and 1."""
        for key, entry in SEED_FAILURE_MATRIX.items():
            assert 0 <= entry["confidence"] <= 1, (
                f"{key} confidence {entry['confidence']} out of range"
            )

    def test_next_phase_is_valid(self):
        """next_phase should be a known phase name."""
        valid_phases = {"ANALYZE", "EXECUTE", "DESIGN", "JUDGE", "OBSERVE"}
        for key, entry in SEED_FAILURE_MATRIX.items():
            assert entry["next_phase"] in valid_phases, (
                f"{key} has invalid next_phase: {entry['next_phase']}"
            )
