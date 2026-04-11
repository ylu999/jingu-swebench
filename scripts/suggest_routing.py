"""Suggest Routing Matrix from Failure Statistics (p216 — Wave 3).

Generates a FailureMatrix from aggregated routing statistics.
The matrix maps (phase, principal) to (next_phase, strategy, confidence).

Output format matches the FailureMatrix type from the design doc:
    key: "PHASE:principal"
    value: {next_phase, strategy, confidence, sample_count}

Strategy selection rules:
    - Phase-specific strategies map known failure patterns to repair approaches
    - Confidence is derived from resolution_rate of the best next_phase
    - Minimum sample count threshold ensures statistical significance
"""

from __future__ import annotations

import json
from typing import Any

from compute_routing_stats import compute_routing_stats, get_best_next_phase, get_top_failures
from extract_failure_events import FailureEvent


# Minimum sample count for a routing suggestion to be "confident"
MIN_SAMPLE_COUNT = 3

# Phase -> principal -> strategy mapping
# These are the known repair strategies that the routing engine can apply.
_STRATEGY_MAP: dict[str, dict[str, str]] = {
    "ANALYZE": {
        "causal_grounding": "complete_causal_chain",
        "evidence_linkage": "gather_code_evidence",
        "ontology_alignment": "realign_phase_ontology",
        "alternative_hypothesis_check": "add_alternative",
        "uncertainty_honesty": "acknowledge_gaps",
        "execution_correctness": "rethink_root_cause",
    },
    "EXECUTE": {
        "action_grounding": "realign_with_analysis",
        "minimal_change": "reidentify_scope",
        "execution_correctness": "fix_execution_errors",
        "constraint_satisfaction": "check_constraints",
    },
    "DESIGN": {
        "option_comparison": "compare_alternatives",
        "constraint_satisfaction": "check_constraints",
        "evidence_linkage": "gather_code_evidence",
    },
    "JUDGE": {
        "result_verification": "verify_test_coverage",
        "evidence_linkage": "gather_code_evidence",
        "residual_risk_detection": "check_regressions",
    },
}

# Default strategies when no specific mapping exists
_DEFAULT_STRATEGIES: dict[str, str] = {
    "ANALYZE": "rethink_root_cause",
    "EXECUTE": "realign_with_analysis",
    "DESIGN": "compare_alternatives",
    "JUDGE": "verify_test_coverage",
    "OBSERVE": "gather_code_evidence",
    "DECIDE": "compare_alternatives",
    "UNDERSTAND": "gather_code_evidence",
}


def _select_strategy(phase: str, principal: str) -> str:
    """Select the best repair strategy for a (phase, principal) pair.

    Looks up the strategy map first, then falls back to
    phase-default strategies.
    """
    phase_strategies = _STRATEGY_MAP.get(phase, {})
    if principal in phase_strategies:
        return phase_strategies[principal]
    return _DEFAULT_STRATEGIES.get(phase, "rethink_root_cause")


def suggest_routing(
    stats: dict[str, Any],
    top_n: int = 20,
    min_samples: int = MIN_SAMPLE_COUNT,
) -> dict[str, dict]:
    """Generate routing matrix from observed failure statistics.

    For the top-N failure patterns by frequency, suggests:
      - best next_phase (highest resolution rate from data)
      - repair strategy (from strategy map)
      - confidence (resolution rate, capped by sample count)
      - sample_count

    Args:
        stats: output from compute_routing_stats()
        top_n: number of top failure patterns to include
        min_samples: minimum observations for confident routing

    Returns:
        FailureMatrix dict: key -> {next_phase, strategy, confidence, sample_count}
    """
    top_failures = get_top_failures(stats, top_n)
    matrix: dict[str, dict] = {}

    for key, entry in top_failures:
        parts = key.split(":", 1)
        phase = parts[0] if parts else "UNKNOWN"
        principal = parts[1] if len(parts) > 1 else "unknown"

        best_next, resolution_rate = get_best_next_phase(entry)
        if not best_next:
            best_next = phase  # default: stay in current phase

        strategy = _select_strategy(phase, principal)
        sample_count = entry["count"]

        # Confidence is resolution_rate, but capped if sample count is low
        confidence = resolution_rate
        if sample_count < min_samples:
            confidence = min(confidence, 0.5)  # low confidence below threshold

        matrix[key] = {
            "next_phase": best_next,
            "strategy": strategy,
            "confidence": round(confidence, 3),
            "sample_count": sample_count,
        }

    return matrix


def suggest_routing_from_events(
    events: list[FailureEvent],
    top_n: int = 20,
    min_samples: int = MIN_SAMPLE_COUNT,
) -> dict[str, dict]:
    """Convenience: compute stats + suggest routing in one call.

    Args:
        events: list of FailureEvent objects
        top_n: number of top failure patterns
        min_samples: minimum observations for confident routing

    Returns:
        FailureMatrix dict.
    """
    stats = compute_routing_stats(events)
    return suggest_routing(stats, top_n=top_n, min_samples=min_samples)


def save_matrix(matrix: dict[str, dict], output_path: str) -> None:
    """Save routing matrix to JSON file.

    Args:
        matrix: FailureMatrix dict from suggest_routing()
        output_path: path to write JSON file
    """
    with open(output_path, "w") as f:
        json.dump(matrix, f, indent=2)


def load_matrix(input_path: str) -> dict[str, dict]:
    """Load routing matrix from JSON file.

    Args:
        input_path: path to JSON file

    Returns:
        FailureMatrix dict.
    """
    with open(input_path) as f:
        return json.load(f)
