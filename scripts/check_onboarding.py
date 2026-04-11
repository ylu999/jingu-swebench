#!/usr/bin/env python3
"""
check_onboarding.py — CI script to validate onboarding completeness.

Validates that:
1. Bundle file exists and is valid JSON
2. All phases with subtypes have contracts
3. All required principals have lifecycle metadata
4. No orphan references (principal referenced but not in registry)
5. Every enforced policy has a non-empty prompt mentioning required fields
6. Every required principal has a repair template

Usage:
    python scripts/check_onboarding.py --bundle path/to/bundle.json
    python scripts/check_onboarding.py  (uses default bundle.json)

Exit codes:
    0 — all checks pass
    1 — one or more checks failed
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure scripts/ is importable
sys.path.insert(0, str(Path(__file__).parent))

from jingu_loader import JinguLoader
from policy_onboarding import check_onboarding_completeness


# ── Expected Phases ───────────────────────────────────────────────────────────

# Phases that MUST have contracts in the bundle.
# UNDERSTAND is excluded because it has no subtype.
_REQUIRED_PHASES = {"OBSERVE", "ANALYZE", "DECIDE", "DESIGN", "EXECUTE", "JUDGE"}

# Phase -> expected default subtype
_EXPECTED_SUBTYPES = {
    "OBSERVE":  "observation.fact_gathering",
    "ANALYZE":  "analysis.root_cause",
    "DECIDE":   "decision.fix_direction",
    "DESIGN":   "design.solution_shape",
    "EXECUTE":  "execution.code_patch",
    "JUDGE":    "judge.verification",
}


# ── Check Functions ───────────────────────────────────────────────────────────

def check_bundle_valid(bundle_path: str) -> list[str]:
    """Check 1: Bundle file exists and is valid JSON.

    Returns list of error strings. Empty = pass.
    """
    errors: list[str] = []
    path = Path(bundle_path)

    if not path.exists():
        errors.append(f"Bundle file not found: {bundle_path}")
        return errors

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        errors.append(f"Bundle is not valid JSON: {e}")
        return errors

    # Check required top-level fields
    for field in ("version", "phases", "contracts"):
        if field not in data:
            errors.append(f"Bundle missing required field: '{field}'")

    return errors


def check_all_phases_have_contracts(loader: JinguLoader) -> list[str]:
    """Check 2: Every required phase has a contract.

    Returns list of error strings. Empty = pass.
    """
    errors: list[str] = []
    available = set(loader.list_contracts())

    for phase, subtype in _EXPECTED_SUBTYPES.items():
        if subtype not in available:
            errors.append(
                f"Phase {phase} missing contract for subtype '{subtype}'"
            )

    return errors


def check_principal_lifecycle(loader: JinguLoader) -> list[str]:
    """Check 3: All required principals have lifecycle metadata.

    Every principal in a contract's required_principals list should:
    - Exist in the principals array of that contract
    - Have inference_rule_exists and fake_check_eligible fields

    Returns list of error strings. Empty = pass.
    """
    errors: list[str] = []
    contracts = loader._bundle.get("contracts", {})

    for key, contract in contracts.items():
        policy = contract.get("policy", {})
        required_principals = set(policy.get("required_principals", []))
        principals_array = contract.get("principals", [])
        principal_names = {p.get("name", "") for p in principals_array}

        # Check that all required principals exist in the principals array
        for rp in required_principals:
            if rp not in principal_names:
                errors.append(
                    f"{key}: required principal '{rp}' not in principals array"
                )

        # Check that principals have lifecycle fields
        for p in principals_array:
            name = p.get("name", "unknown")
            if "inference_rule_exists" not in p:
                errors.append(
                    f"{key}: principal '{name}' missing 'inference_rule_exists' field"
                )
            if "fake_check_eligible" not in p:
                errors.append(
                    f"{key}: principal '{name}' missing 'fake_check_eligible' field"
                )

    return errors


def check_no_orphan_references(loader: JinguLoader) -> list[str]:
    """Check 4: No orphan principal references.

    A principal referenced in required_principals or forbidden_principals
    should not reference a name that does not exist anywhere in the bundle.

    Returns list of error strings. Empty = pass.
    """
    errors: list[str] = []
    contracts = loader._bundle.get("contracts", {})

    # Collect all known principal names across all contracts
    all_known_principals: set[str] = set()
    for contract in contracts.values():
        for p in contract.get("principals", []):
            name = p.get("name", "")
            if name:
                all_known_principals.add(name)

    # Check references
    for key, contract in contracts.items():
        policy = contract.get("policy", {})
        for ref_type in ("required_principals", "forbidden_principals"):
            for name in policy.get(ref_type, []):
                if name not in all_known_principals:
                    errors.append(
                        f"{key}: {ref_type} references unknown principal '{name}'"
                    )

    return errors


# ── Main ──────────────────────────────────────────────────────────────────────

def run_all_checks(bundle_path: str) -> tuple[int, list[str]]:
    """Run all onboarding checks and return (error_count, messages).

    Returns:
        Tuple of (total_error_count, list_of_error_messages).
    """
    all_errors: list[str] = []

    # Check 1: Bundle valid
    print("[check_onboarding] Check 1: Bundle file validity...")
    errors = check_bundle_valid(bundle_path)
    if errors:
        all_errors.extend(errors)
        # Cannot continue if bundle is invalid
        return len(all_errors), all_errors

    # Load bundle
    try:
        loader = JinguLoader(bundle_path)
    except Exception as e:
        all_errors.append(f"Failed to load bundle: {e}")
        return len(all_errors), all_errors

    meta = loader.get_metadata()
    print(f"  Bundle v{meta['version']} loaded ({meta['contract_count']} contracts)")

    # Check 2: All phases have contracts
    print("[check_onboarding] Check 2: Phase coverage...")
    errors = check_all_phases_have_contracts(loader)
    all_errors.extend(errors)
    print(f"  {len(_REQUIRED_PHASES) - len(errors)}/{len(_REQUIRED_PHASES)} phases covered")

    # Check 3: Principal lifecycle metadata
    print("[check_onboarding] Check 3: Principal lifecycle metadata...")
    errors = check_principal_lifecycle(loader)
    all_errors.extend(errors)
    if not errors:
        print("  All principals have lifecycle metadata")

    # Check 4: No orphan references
    print("[check_onboarding] Check 4: Orphan reference check...")
    errors = check_no_orphan_references(loader)
    all_errors.extend(errors)
    if not errors:
        print("  No orphan references found")

    # Check 5: Onboarding completeness (prompt + repair coverage)
    print("[check_onboarding] Check 5: Onboarding completeness (prompt + repair)...")
    errors = check_onboarding_completeness(loader)
    all_errors.extend(errors)
    if not errors:
        print("  All policies onboarded (prompt + repair templates present)")

    return len(all_errors), all_errors


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CI check for onboarding completeness",
    )
    parser.add_argument(
        "--bundle",
        default=str(Path(__file__).parent.parent / "bundle.json"),
        help="Path to bundle.json (default: project root bundle.json)",
    )
    args = parser.parse_args()

    error_count, errors = run_all_checks(args.bundle)

    print()
    if error_count == 0:
        print("ONBOARDING CHECK PASSED: all policies onboarded")
        sys.exit(0)
    else:
        print(f"ONBOARDING CHECK FAILED: {error_count} error(s)")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
