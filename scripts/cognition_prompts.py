"""
cognition_prompts.py — Phase-specific prompt templates from CognitionLoader (p222).

Generates phase-specific prompt sections from the cognition bundle:
  - Required fields for the current phase
  - Forbidden outputs for the current phase
  - Required principals for the current subtype
  - Allowed transitions from current phase

When COGNITION_EXECUTION_ENABLED=true, these prompts are used to tell the
agent what is expected, ensuring the agent knows about all gate requirements
before the gate enforces them (onboarding invariant from p218).

Feature flag: COGNITION_EXECUTION_ENABLED from cognition_loader.py
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

# Phase 3: cognition_loader deleted. COGNITION_EXECUTION_ENABLED is always False.
# CognitionLoader stub preserves the interface for any remaining import references.
COGNITION_EXECUTION_ENABLED: bool = False


class CognitionLoader:  # type: ignore[no-redef]
    """Stub — cognition_loader deleted in Phase 3. Flag is always False."""
    def __init__(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("CognitionLoader is not available (COGNITION_EXECUTION_ENABLED=False)")

logger = logging.getLogger(__name__)

# ── Cached loader ────────────────────────────────────────────────────────────

_cached_loader: Optional[CognitionLoader] = None


def _get_cognition_loader() -> Optional[CognitionLoader]:
    """Get or create cached CognitionLoader from bundle.json.

    Returns None if bundle cannot be loaded (non-fatal).
    """
    global _cached_loader
    if _cached_loader is not None:
        return _cached_loader
    try:
        from jingu_loader import JinguLoader
        jl = JinguLoader()
        _cached_loader = CognitionLoader(jl._bundle)
        return _cached_loader
    except Exception as e:
        logger.warning("[cognition_prompts] Failed to load CognitionLoader: %s", e)
        return None


def reset_loader() -> None:
    """Reset cached loader (for testing)."""
    global _cached_loader
    _cached_loader = None


# ── Prompt Builders ──────────────────────────────────────────────────────────

def build_phase_requirements(phase: str, loader: Optional[CognitionLoader] = None) -> str:
    """Build a prompt section listing requirements for a phase.

    Includes:
      - Required fields from phase definition
      - Forbidden outputs from phase definition
      - Required principals from subtype contract

    Args:
        phase: Phase name (e.g. "ANALYZE").
        loader: Optional CognitionLoader override (uses cached if None).

    Returns:
        Formatted prompt section string. Empty if loader unavailable or phase unknown.
    """
    if loader is None:
        loader = _get_cognition_loader()
    if loader is None:
        return ""

    phase_upper = phase.upper()
    phase_def = loader.get_phase_definition(phase_upper)
    if not phase_def:
        return ""

    parts: list[str] = []

    # Phase header
    parts.append(f"[Phase: {phase_upper}]")

    # Required fields
    required_fields = phase_def.get("required_fields", [])
    if required_fields:
        fields_str = ", ".join(required_fields)
        parts.append(f"Required fields: {fields_str}")

    # Forbidden outputs
    forbidden = phase_def.get("forbidden_outputs", [])
    if forbidden:
        parts.append("Forbidden actions:")
        for f in forbidden:
            parts.append(f"  - {f}")

    # Required principals from subtype
    subtypes = loader.get_subtypes_for_phase(phase_upper)
    if subtypes:
        # Use the first (primary) subtype
        primary = subtypes[0]
        subtype_name = primary["name"]
        required_principals = loader.get_required_principals(subtype_name)
        if required_principals:
            principals_str = ", ".join(required_principals)
            parts.append(f"You MUST declare PRINCIPALS: {principals_str}")

        forbidden_principals = loader.get_forbidden_principals(subtype_name)
        if forbidden_principals:
            forbidden_str = ", ".join(forbidden_principals)
            parts.append(f"You must NOT declare: {forbidden_str}")

    return "\n".join(parts)


def build_transition_guidance(phase: str, loader: Optional[CognitionLoader] = None) -> str:
    """Build a prompt section listing allowed next phases.

    Args:
        phase: Current phase name.
        loader: Optional CognitionLoader override.

    Returns:
        Formatted string listing allowed transitions.
    """
    if loader is None:
        loader = _get_cognition_loader()
    if loader is None:
        return ""

    phase_upper = phase.upper()
    allowed_next: list[str] = []
    for t in loader.transitions:
        if t["from"] == phase_upper and t["allowed"]:
            allowed_next.append(t["to"])

    if not allowed_next:
        return ""

    return f"Allowed next phases: {', '.join(allowed_next)}"


def build_cognition_prompt_prefix(phase: str, loader: Optional[CognitionLoader] = None) -> str:
    """Build complete cognition-aware prompt prefix for a phase.

    Combines phase requirements + transition guidance into a single
    prompt section ready for injection.

    Args:
        phase: Phase name (e.g. "ANALYZE").
        loader: Optional CognitionLoader override.

    Returns:
        Complete prompt prefix string. Empty if not available.
    """
    if loader is None:
        loader = _get_cognition_loader()
    if loader is None:
        return ""

    parts = []

    requirements = build_phase_requirements(phase, loader)
    if requirements:
        parts.append(requirements)

    transitions = build_transition_guidance(phase, loader)
    if transitions:
        parts.append(transitions)

    if not parts:
        return ""

    return "\n\n".join(parts) + "\n"


def build_subtype_contract_prompt(subtype: str, loader: Optional[CognitionLoader] = None) -> str:
    """Build prompt section for a specific subtype contract.

    More detailed than build_phase_requirements — includes all contract
    fields for a specific subtype.

    Args:
        subtype: Subtype identifier (e.g. "analysis.root_cause").
        loader: Optional CognitionLoader override.

    Returns:
        Formatted contract prompt string.
    """
    if loader is None:
        loader = _get_cognition_loader()
    if loader is None:
        return ""

    defn = loader.get_subtype_definition(subtype)
    if not defn:
        return ""

    parts: list[str] = []
    parts.append(f"[Contract: {subtype}]")
    parts.append(f"Phase: {defn['phase']}")

    required = defn.get("required_principals", [])
    if required:
        parts.append(f"Required principals: {', '.join(required)}")

    forbidden = defn.get("forbidden_principals", [])
    if forbidden:
        parts.append(f"Forbidden principals: {', '.join(forbidden)}")

    upstream = defn.get("required_upstream_phases", [])
    if upstream:
        parts.append(f"Prerequisites: {', '.join(upstream)}")

    return "\n".join(parts)
