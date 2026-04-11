"""
policy_onboarding.py — Policy onboarding API (p218)

Wraps JinguLoader with caching and provides:
- get_active_contract(): cached contract access
- inject_contract_into_prompt(): inject contract fields into agent prompt
- check_onboarding_completeness(): validate all required fields are onboarded

The invariant: if a gate can reject for field X, the agent MUST have been
told about field X in its prompt. This module ensures that invariant holds.

Feature flag: USE_BUNDLE_LOADER from jingu_loader.py controls whether
this module is active. When False, legacy subtype_contracts.py path is used.
"""

from __future__ import annotations

import logging
from typing import Any

from jingu_loader import JinguLoader, USE_BUNDLE_LOADER, PolicyLifecycle

logger = logging.getLogger(__name__)

# ── Cached Loader ─────────────────────────────────────────────────────────────

_loader_instance: JinguLoader | None = None


def _get_loader() -> JinguLoader:
    """Get or create a cached JinguLoader instance.

    Returns:
        Cached JinguLoader reading from the default bundle path.

    Raises:
        FileNotFoundError: If bundle.json does not exist.
        ValueError: If bundle version is incompatible.
    """
    global _loader_instance
    if _loader_instance is None:
        _loader_instance = JinguLoader()
        meta = _loader_instance.get_metadata()
        logger.info(
            "[policy_onboarding] Loaded bundle v%s (commit=%s, contracts=%d)",
            meta["version"],
            meta["generator_commit"],
            meta["contract_count"],
        )
    return _loader_instance


def reset_loader() -> None:
    """Reset the cached loader instance (for testing)."""
    global _loader_instance
    _loader_instance = None


# ── Contract Access ───────────────────────────────────────────────────────────

def get_active_contract(phase: str, subtype: str = "") -> dict[str, Any]:
    """Get an ActiveContract for the given phase/subtype.

    This is the primary entry point for contract access. It wraps
    JinguLoader with caching and logging.

    Args:
        phase: Phase name (e.g. "ANALYZE").
        subtype: Optional subtype override (auto-resolved if omitted).

    Returns:
        Full ActiveContract dict from the bundle.
    """
    loader = _get_loader()
    return loader.get_active_contract(phase, subtype)


def get_prompt_slice(phase: str, subtype: str = "") -> str:
    """Get the prompt slice for a phase/subtype.

    Returns the compiled prompt string from the ActiveContract.
    This includes: phase goal, required fields, forbidden moves,
    principal declarations, and success criteria.

    Args:
        phase: Phase name.
        subtype: Optional subtype override.

    Returns:
        Non-empty prompt string.
    """
    loader = _get_loader()
    return loader.get_prompt(phase, subtype)


def get_required_principals(phase: str, subtype: str = "") -> list[str]:
    """Get required principal names for a phase/subtype.

    Args:
        phase: Phase name.
        subtype: Optional subtype override.

    Returns:
        List of required principal name strings.
    """
    loader = _get_loader()
    return loader.get_required_principals(phase, subtype)


# ── Prompt Injection ──────────────────────────────────────────────────────────

def inject_contract_into_prompt(prompt: str, phase: str, subtype: str = "") -> str:
    """Inject contract fields into an agent prompt.

    Appends the contract prompt_slice to the existing prompt.
    The prompt_slice contains all the information the agent needs
    to satisfy the gate requirements for this phase.

    Args:
        prompt: Existing prompt string.
        phase: Phase name for contract lookup.
        subtype: Optional subtype override.

    Returns:
        Enhanced prompt with contract information appended.
    """
    try:
        prompt_slice = get_prompt_slice(phase, subtype)
        if not prompt_slice:
            logger.warning(
                "[policy_onboarding] Empty prompt_slice for phase=%s subtype=%s",
                phase, subtype,
            )
            return prompt

        return f"{prompt}\n\n--- Contract Requirements ---\n{prompt_slice}"
    except Exception as e:
        logger.warning(
            "[policy_onboarding] Failed to inject contract: %s (falling back to original prompt)",
            e,
        )
        return prompt


# ── Onboarding Completeness Check ────────────────────────────────────────────

def check_onboarding_completeness(loader: JinguLoader | None = None) -> list[str]:
    """Validate that every enforced policy has been onboarded into the agent prompt.

    For every phase/subtype with a registered policy:
    1. Does get_active_contract() return a non-empty prompt?
    2. Does the prompt mention all required_fields from the policy?
    3. Does a repair template exist for each required principal?

    Args:
        loader: Optional JinguLoader instance. Uses default if None.

    Returns:
        List of violation descriptions. Empty list = all onboarded.
    """
    if loader is None:
        loader = _get_loader()

    violations: list[str] = []
    contracts = loader._bundle.get("contracts", {})

    for key, contract in contracts.items():
        prompt = contract.get("prompt", "")
        policy = contract.get("policy", {})
        required_fields = policy.get("required_fields", [])
        required_principals = policy.get("required_principals", [])
        repair_templates = contract.get("repair_templates", {})

        # Check 1: prompt exists
        if not prompt:
            violations.append(
                f"{key}: no prompt (policy registered but not injected into agent prompt)"
            )
            continue

        # Check 2: prompt mentions required fields
        prompt_lower = prompt.lower()
        for field in required_fields:
            if field.lower() not in prompt_lower:
                violations.append(
                    f"{key}: prompt missing mention of required field '{field}'"
                )

        # Check 3: repair template exists for required principals
        for principal in required_principals:
            if principal not in repair_templates:
                violations.append(
                    f"{key}: missing repair template for required principal '{principal}'"
                )

    return violations


def get_lifecycle_state(phase: str, subtype: str = "") -> str:
    """Determine the policy lifecycle state for a phase/subtype.

    Checks which lifecycle conditions are met:
    - DEFINED: contract exists in bundle
    - REGISTERED: contract has policy spec
    - INJECTED: contract has non-empty prompt
    - ENFORCED: contract has required_principals
    - REPAIRED: contract has repair_templates for all required principals

    Args:
        phase: Phase name.
        subtype: Optional subtype override.

    Returns:
        The highest achieved lifecycle state string.
    """
    try:
        contract = get_active_contract(phase, subtype)
    except (KeyError, ValueError):
        return PolicyLifecycle.DEFINED  # exists conceptually but not in bundle

    if not contract.get("policy"):
        return PolicyLifecycle.DEFINED

    policy = contract["policy"]
    if not policy.get("required_principals") and not policy.get("required_fields"):
        return PolicyLifecycle.REGISTERED

    prompt = contract.get("prompt", "")
    if not prompt:
        return PolicyLifecycle.REGISTERED

    # Check if prompt mentions required fields
    prompt_lower = prompt.lower()
    required_fields = policy.get("required_fields", [])
    all_fields_mentioned = all(
        f.lower() in prompt_lower for f in required_fields
    )
    if not all_fields_mentioned:
        return PolicyLifecycle.REGISTERED

    # At this point, prompt exists and mentions required fields = INJECTED
    required_principals = policy.get("required_principals", [])
    if not required_principals:
        return PolicyLifecycle.INJECTED

    # Check if gate would enforce (has required principals)
    # This is always true if required_principals is non-empty
    # = ENFORCED

    # Check repair coverage
    repair_templates = contract.get("repair_templates", {})
    all_repaired = all(p in repair_templates for p in required_principals)
    if all_repaired:
        return PolicyLifecycle.REPAIRED

    return PolicyLifecycle.ENFORCED
