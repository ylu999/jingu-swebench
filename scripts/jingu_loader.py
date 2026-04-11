"""
jingu_loader.py — Read-only loader for pre-compiled ActiveContract bundles.

No compilation logic in Python. All contracts are pre-compiled by TS
(jingu-cognition) and serialized to a JSON bundle file.

The bundle is the cross-language bridge: TS compiles, writes JSON,
Python reads JSON. No cross-language imports.

Version mismatch -> ValueError (fail-fast).

Feature flag: USE_BUNDLE_LOADER environment variable controls
whether this loader is used or the legacy subtype_contracts.py path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


# ── Feature Flag ──────────────────────────────────────────────────────────────

USE_BUNDLE_LOADER: bool = os.environ.get("USE_BUNDLE_LOADER", "false").lower() == "true"

# Default bundle path (relative to this file)
_DEFAULT_BUNDLE_PATH = str(Path(__file__).parent.parent / "bundle.json")

# Supported bundle version range
_SUPPORTED_VERSION_MAJOR = 1


# ── Phase/Subtype Resolution ─────────────────────────────────────────────────

# Canonical phase -> subtype mapping (mirrors PHASE_TO_SUBTYPE in subtypes.ts)
_PHASE_TO_SUBTYPE: dict[str, str] = {
    "OBSERVE":  "observation.fact_gathering",
    "ANALYZE":  "analysis.root_cause",
    "DECIDE":   "decision.fix_direction",
    "DESIGN":   "design.solution_shape",
    "EXECUTE":  "execution.code_patch",
    "JUDGE":    "judge.verification",
}


def _resolve_subtype(phase: str, subtype: str = "") -> str:
    """Resolve a phase to its default subtype if subtype not given."""
    if subtype:
        return subtype
    phase_upper = phase.upper()
    resolved = _PHASE_TO_SUBTYPE.get(phase_upper)
    if not resolved:
        raise ValueError(
            f"Cannot resolve subtype for phase '{phase_upper}'. "
            f"Known phases: {list(_PHASE_TO_SUBTYPE.keys())}"
        )
    return resolved


# ── Runtime Capabilities (p220 — capability negotiation) ─────────────────────

# What this Python runtime (jingu-swebench) currently supports.
# Used by get_negotiated_contract() to strip unsupported fields.
RUNTIME_CAPABILITIES: dict[str, bool] = {
    "schema_enforced": False,    # Not yet using structured output
    "repair_view": True,         # Can use repair templates (SDG p217)
    "routing_view": False,       # Not yet using failure routing matrix
}


# ── Policy Lifecycle States ───────────────────────────────────────────────────

class PolicyLifecycle:
    """Policy lifecycle states (p218).

    A policy progresses through these states:
    DEFINED -> REGISTERED -> INJECTED -> ENFORCED -> REPAIRED
    """
    DEFINED = "defined"          # Policy exists in registry
    REGISTERED = "registered"    # Policy compiled into bundle
    INJECTED = "injected"        # Prompt slice includes policy
    ENFORCED = "enforced"        # Gate checks policy
    REPAIRED = "repaired"        # Repair template exists for violations


# ── JinguLoader ───────────────────────────────────────────────────────────────

class JinguLoader:
    """Read-only loader for pre-compiled ActiveContract bundles.

    No compilation logic in Python. All contracts are pre-compiled by TS.
    Version mismatch -> ValueError (fail-fast).

    Usage:
        loader = JinguLoader("bundle.json")
        contract = loader.get_active_contract("ANALYZE")
        prompt = loader.get_prompt("ANALYZE")
        principals = loader.get_required_principals("ANALYZE")
    """

    def __init__(self, bundle_path: str | None = None) -> None:
        """Load bundle from JSON file. Validates version.

        Args:
            bundle_path: Path to bundle.json. Defaults to project root bundle.json.

        Raises:
            FileNotFoundError: If bundle file does not exist.
            ValueError: If bundle version is incompatible.
            json.JSONDecodeError: If bundle is not valid JSON.
        """
        path = bundle_path or _DEFAULT_BUNDLE_PATH
        with open(path, "r", encoding="utf-8") as f:
            self._bundle: dict[str, Any] = json.load(f)

        self._validate_version()
        self._path = path

    def _validate_version(self) -> None:
        """Validate bundle version is compatible."""
        version = self._bundle.get("version", "")
        if not version:
            raise ValueError("Bundle has no version field")

        parts = version.split(".")
        if len(parts) < 2:
            raise ValueError(f"Invalid bundle version format: '{version}'")

        try:
            major = int(parts[0])
        except ValueError:
            raise ValueError(f"Invalid bundle version: '{version}'")

        if major != _SUPPORTED_VERSION_MAJOR:
            raise ValueError(
                f"Bundle version {version} is not compatible. "
                f"Supported major version: {_SUPPORTED_VERSION_MAJOR}"
            )

    # ── Contract Access ───────────────────────────────────────────────────────

    def get_active_contract(self, phase: str, subtype: str = "") -> dict[str, Any]:
        """Return ActiveContract for phase/subtype.

        Args:
            phase: Phase name (e.g. "ANALYZE").
            subtype: Optional subtype override (auto-resolved if omitted).

        Returns:
            Full ActiveContract dict from the bundle.

        Raises:
            KeyError: If no contract exists for the resolved subtype.
        """
        resolved = _resolve_subtype(phase, subtype)
        contracts = self._bundle.get("contracts", {})
        if resolved not in contracts:
            raise KeyError(
                f"No contract found for subtype '{resolved}' "
                f"(phase='{phase}'). Available: {list(contracts.keys())}"
            )
        return contracts[resolved]

    def get_required_principals(self, phase: str, subtype: str = "") -> list[str]:
        """Get required principal names for a phase/subtype.

        Args:
            phase: Phase name.
            subtype: Optional subtype override.

        Returns:
            List of required principal name strings.
        """
        contract = self.get_active_contract(phase, subtype)
        policy = contract.get("policy", {})
        return list(policy.get("required_principals", []))

    def get_prompt(self, phase: str, subtype: str = "") -> str:
        """Return prompt slice for agent injection.

        Args:
            phase: Phase name.
            subtype: Optional subtype override.

        Returns:
            Compiled prompt string.
        """
        contract = self.get_active_contract(phase, subtype)
        return str(contract.get("prompt", ""))

    def get_schema(self, phase: str, subtype: str = "") -> dict[str, Any]:
        """Return JSON schema for structured output enforcement.

        Args:
            phase: Phase name.
            subtype: Optional subtype override.

        Returns:
            JSON Schema dict.
        """
        contract = self.get_active_contract(phase, subtype)
        return dict(contract.get("schema", {}))

    def get_prompt_view(self, phase: str, subtype: str = "") -> dict[str, Any]:
        """Get prompt-focused view of the contract.

        Returns:
            Dict with prompt, required_fields, forbidden_moves, required_principals.
        """
        contract = self.get_active_contract(phase, subtype)
        policy = contract.get("policy", {})
        return {
            "prompt": contract.get("prompt", ""),
            "required_fields": policy.get("required_fields", []),
            "forbidden_moves": policy.get("forbidden_moves", []),
            "required_principals": policy.get("required_principals", []),
        }

    def get_repair_view(self, phase: str, subtype: str = "") -> dict[str, Any]:
        """Get repair-focused view of the contract.

        Returns:
            Dict with repair_templates and routing.
        """
        contract = self.get_active_contract(phase, subtype)
        return {
            "repair_templates": contract.get("repair_templates", {}),
            "routing": contract.get("routing", {}),
        }

    def get_negotiated_contract(
        self,
        phase: str,
        subtype: str = "",
        runtime_caps: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        """Return contract negotiated for runtime capabilities.

        Strips fields the runtime does not support:
        - schema: omitted if schema_enforced=False
        - repair_templates: omitted if repair_view=False
        - routing: omitted if routing_view=False
        - prompt: always included

        Args:
            phase: Phase name (e.g. "ANALYZE").
            subtype: Optional subtype override.
            runtime_caps: Override runtime capabilities. Defaults to RUNTIME_CAPABILITIES.

        Returns:
            Dict with only the fields the runtime supports.
        """
        caps = runtime_caps or RUNTIME_CAPABILITIES
        contract = self.get_active_contract(phase, subtype)

        negotiated: dict[str, Any] = {
            "prompt": contract.get("prompt", ""),
        }

        if caps.get("schema_enforced", False):
            schema = contract.get("schema")
            if schema:
                negotiated["schema"] = schema

        if caps.get("repair_view", False):
            repair = contract.get("repair_templates")
            if repair:
                negotiated["repair_templates"] = repair

        if caps.get("routing_view", False):
            routing = contract.get("routing")
            if routing:
                negotiated["routing"] = routing

        return negotiated

    def get_metadata(self) -> dict[str, Any]:
        """Return bundle metadata (version, capabilities, etc.)."""
        return {
            "version": self._bundle.get("version", ""),
            "compiler_version": self._bundle.get("compiler_version", ""),
            "generated_at": self._bundle.get("generated_at", ""),
            "generator_commit": self._bundle.get("generator_commit", ""),
            "capabilities": self._bundle.get("capabilities", []),
            "phases": self._bundle.get("phases", []),
            "contract_count": len(self._bundle.get("contracts", {})),
        }

    # ── Enumeration ───────────────────────────────────────────────────────────

    def list_contracts(self) -> list[str]:
        """List all available contract keys (subtypes)."""
        return list(self._bundle.get("contracts", {}).keys())

    def list_phases(self) -> list[str]:
        """List all phases in the bundle."""
        return list(self._bundle.get("phases", []))
