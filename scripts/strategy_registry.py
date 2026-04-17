"""
strategy_registry.py — Single source of truth for repair strategy taxonomy.

All consumers derive strategy names, validation, and prompt fragments from here.
Consumers: phase_lifecycle._route_analyze, analysis_gate, declaration_extractor,
           protocol_compiler, cognition_contracts/analysis_root_cause.

The canonical definition lives in analysis_root_cause.REPAIR_STRATEGY_TYPES.
This module provides the access layer: validation, normalization, prompt rendering.
"""

from __future__ import annotations

from typing import Any


def _load_strategies() -> tuple[str, ...]:
    """Load strategy types from the canonical source (analysis_root_cause)."""
    from cognition_contracts.analysis_root_cause import REPAIR_STRATEGY_TYPES
    return tuple(REPAIR_STRATEGY_TYPES)


def all_strategies() -> tuple[str, ...]:
    """Return all valid repair strategy type names."""
    return _load_strategies()


def is_valid_strategy(value: str) -> bool:
    """Check if value is a valid repair strategy type (case-insensitive)."""
    if not value or not isinstance(value, str) or not value.strip():
        return False
    return value.strip().upper() in {s.upper() for s in _load_strategies()}


def normalize_strategy(value: str) -> str:
    """Normalize strategy to canonical uppercase form. Raises ValueError if invalid."""
    if not value or not isinstance(value, str) or not value.strip():
        raise ValueError("Empty or non-string strategy value")
    upper = value.strip().upper()
    strategies = _load_strategies()
    canonical = {s.upper(): s for s in strategies}
    if upper not in canonical:
        raise ValueError(
            f"Unknown strategy '{value}'. Valid: {', '.join(strategies)}"
        )
    return canonical[upper]


def strategy_prompt_fragment() -> str:
    """Return the prompt fragment listing all strategies for agent guidance."""
    strategies = _load_strategies()
    return " | ".join(strategies)


def validate_record_strategy(record: dict[str, Any]) -> tuple[bool, str]:
    """Validate repair_strategy_type in an admitted record.

    Returns (valid, normalized_strategy_or_error_message).
    """
    raw = record.get("repair_strategy_type", "")
    if not raw or not isinstance(raw, str) or not raw.strip():
        return False, "repair_strategy_type is missing or empty"
    try:
        normalized = normalize_strategy(raw)
        return True, normalized
    except ValueError as e:
        return False, str(e)
