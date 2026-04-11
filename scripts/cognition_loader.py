"""
cognition_loader.py — Loads cognition definitions from bundle.json cognition section.

The cognition section contains phase definitions, subtype definitions,
principal mappings, and transition rules — all compiled by TS (jingu-cognition)
and consumed read-only by Python.

This replaces hardcoded contracts in subtype_contracts.py when
COGNITION_EXECUTION_ENABLED=true.

Feature flag: COGNITION_EXECUTION_ENABLED (env var, default false)
"""

from __future__ import annotations

import os
from typing import Any, Optional


# Feature flag: controls whether cognition validation is active
COGNITION_EXECUTION_ENABLED: bool = (
    os.environ.get("COGNITION_EXECUTION_ENABLED", "false").lower() == "true"
)


class CognitionLoader:
    """Loads cognition definitions from bundle.json cognition section.

    Usage:
        loader = CognitionLoader(bundle_dict)
        principals = loader.get_required_principals("analysis.root_cause")
        allowed = loader.is_transition_allowed("ANALYZE", "DECIDE")
    """

    def __init__(self, bundle: dict[str, Any]) -> None:
        """Initialize from a loaded bundle dict.

        Args:
            bundle: Parsed bundle.json dict (must have "cognition" key).

        Raises:
            ValueError: If bundle has no cognition section.
        """
        cog = bundle.get("cognition")
        if cog is None:
            raise ValueError(
                "Bundle has no 'cognition' section. "
                "Regenerate bundle with jingu-cognition >= p222."
            )

        self.phases: dict[str, dict[str, Any]] = {
            p["name"]: p for p in cog.get("phases", [])
        }
        self.subtypes: dict[str, dict[str, Any]] = {
            s["name"]: s for s in cog.get("subtypes", [])
        }
        self.principal_mapping: dict[str, list[str]] = cog.get(
            "principal_mapping", {}
        )
        self.transitions: list[dict[str, Any]] = cog.get("transitions", [])

    def get_required_principals(self, subtype: str) -> list[str]:
        """Get required principals for a subtype.

        Args:
            subtype: Subtype identifier (e.g. "analysis.root_cause").

        Returns:
            List of required principal name strings. Empty if subtype unknown.
        """
        return list(self.principal_mapping.get(subtype, []))

    def get_forbidden_principals(self, subtype: str) -> list[str]:
        """Get forbidden principals for a subtype.

        Args:
            subtype: Subtype identifier.

        Returns:
            List of forbidden principal names. Empty if subtype unknown.
        """
        defn = self.subtypes.get(subtype)
        if not defn:
            return []
        return list(defn.get("forbidden_principals", []))

    def is_transition_allowed(self, from_phase: str, to_phase: str) -> bool:
        """Check if a phase transition is allowed.

        Args:
            from_phase: Source phase (e.g. "ANALYZE").
            to_phase: Target phase (e.g. "DECIDE").

        Returns:
            True if the transition is explicitly allowed, False otherwise.
        """
        return any(
            t["from"] == from_phase
            and t["to"] == to_phase
            and t["allowed"]
            for t in self.transitions
        )

    def get_phase_definition(self, phase: str) -> Optional[dict[str, Any]]:
        """Get phase definition by name.

        Args:
            phase: Phase name (e.g. "ANALYZE").

        Returns:
            Phase definition dict or None if not found.
        """
        return self.phases.get(phase)

    def get_subtype_definition(self, subtype: str) -> Optional[dict[str, Any]]:
        """Get subtype definition by name.

        Args:
            subtype: Subtype identifier (e.g. "analysis.root_cause").

        Returns:
            Subtype definition dict or None if not found.
        """
        return self.subtypes.get(subtype)

    def get_all_phases(self) -> list[str]:
        """List all phase names in canonical order."""
        return list(self.phases.keys())

    def get_subtypes_for_phase(self, phase: str) -> list[dict[str, Any]]:
        """Get all subtype definitions for a given phase.

        Args:
            phase: Phase name (e.g. "ANALYZE").

        Returns:
            List of subtype definition dicts belonging to this phase.
        """
        return [
            s for s in self.subtypes.values() if s.get("phase") == phase
        ]

    def get_phase_for_subtype(self, subtype: str) -> Optional[str]:
        """Get the phase that owns this subtype.

        Args:
            subtype: Subtype identifier.

        Returns:
            Phase name string or None.
        """
        defn = self.subtypes.get(subtype)
        return defn["phase"] if defn else None
