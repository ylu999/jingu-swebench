#!/usr/bin/env python3
"""
CI check: detect shadow contracts — gate fields not defined in the bundle schema.

Shadow contract = a gate rule evaluates a field that is not in the contract's
SCHEMA_PROPERTIES. This means the agent has no way to know about the requirement,
producing unexplainable rejections.

Three checks:
  1. Every GateRule.field must exist in the contract's SCHEMA_PROPERTIES keys.
  2. Every required FieldSpec.name must exist in the contract's SCHEMA_PROPERTIES keys.
  3. If bundle.json exists, every GateRule.field must exist in the bundle's
     schema.properties for that subtype.

Exit 0 = PASS (no shadow contracts).
Exit 1 = FAIL (shadow contracts detected).
"""
from __future__ import annotations

import json
import os
import sys

# ---------------------------------------------------------------------------
# Ensure scripts/ is on sys.path so cognition_contracts can be imported.
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

_REPO_ROOT = os.path.dirname(_SCRIPTS_DIR)

# ---------------------------------------------------------------------------
# Contract module registry: module_name -> subtype string
# ---------------------------------------------------------------------------
_CONTRACT_MODULES: list[tuple[str, str]] = [
    ("cognition_contracts.analysis_root_cause", "analysis.root_cause"),
    ("cognition_contracts.design_solution_shape", "design.solution_shape"),
    ("cognition_contracts.decision_fix_direction", "decision.fix_direction"),
    ("cognition_contracts.execution_code_patch", "execution.code_patch"),
    ("cognition_contracts.judge_verification", "judge.verification"),
    ("cognition_contracts.observation_fact_gathering", "observation.fact_gathering"),
]


# ---------------------------------------------------------------------------
# Bundle loader
# ---------------------------------------------------------------------------

def _load_bundle() -> dict | None:
    """Load bundle.json from repo root. Returns None if missing or invalid."""
    bundle_path = os.path.join(_REPO_ROOT, "bundle.json")
    if not os.path.isfile(bundle_path):
        print(f"  WARNING: bundle.json not found at {bundle_path} — skipping bundle checks")
        return None
    try:
        with open(bundle_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"  WARNING: failed to load bundle.json: {exc} — skipping bundle checks")
        return None


# ---------------------------------------------------------------------------
# Import helper
# ---------------------------------------------------------------------------

def _import_contract(module_name: str):
    """Import a contract module by dotted name. Returns module or None."""
    try:
        import importlib
        return importlib.import_module(module_name)
    except Exception as exc:
        print(f"  WARNING: cannot import {module_name}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_shadow_contracts() -> list[str]:
    """
    Run all shadow contract checks. Returns a list of violation strings.
    Empty list = all clear.
    """
    violations: list[str] = []
    warnings: list[str] = []
    bundle = _load_bundle()

    for module_name, subtype in _CONTRACT_MODULES:
        mod = _import_contract(module_name)
        if mod is None:
            warnings.append(f"SKIP: {module_name} could not be imported")
            continue

        # Gather contract attributes with safe getattr
        gate_rules = getattr(mod, "GATE_RULES", [])
        field_specs = getattr(mod, "FIELD_SPECS", [])
        schema_props = getattr(mod, "SCHEMA_PROPERTIES", {})

        schema_keys = set(schema_props.keys())
        field_spec_names = {fs.name for fs in field_specs}

        # --- Check 1: GateRule.field must be in SCHEMA_PROPERTIES ---
        for rule in gate_rules:
            if rule.field not in schema_keys:
                violations.append(
                    f"SHADOW [gate_rule_vs_schema]: "
                    f"{module_name} GateRule '{rule.name}' checks field "
                    f"'{rule.field}' which is NOT in SCHEMA_PROPERTIES "
                    f"(available: {sorted(schema_keys)})"
                )

        # --- Check 2: Required FieldSpec.name must be in SCHEMA_PROPERTIES ---
        for fs in field_specs:
            if fs.required and fs.name not in schema_keys:
                violations.append(
                    f"SHADOW [field_spec_vs_schema]: "
                    f"{module_name} required FieldSpec '{fs.name}' has NO key "
                    f"in SCHEMA_PROPERTIES (available: {sorted(schema_keys)})"
                )

        # --- Check 3: FieldSpec.name should be in SCHEMA_PROPERTIES (warning for optional) ---
        for fs in field_specs:
            if not fs.required and fs.name not in schema_keys:
                warnings.append(
                    f"NOTICE: {module_name} optional FieldSpec '{fs.name}' "
                    f"has no key in SCHEMA_PROPERTIES — may be intentional"
                )

        # --- Check 4: GateRule.field must reference a FieldSpec ---
        # (Already done by validate_contract_definition check 5, but we
        # include it here for completeness in the shadow detector.)
        for rule in gate_rules:
            if rule.field not in field_spec_names:
                violations.append(
                    f"SHADOW [gate_rule_vs_field_spec]: "
                    f"{module_name} GateRule '{rule.name}' references field "
                    f"'{rule.field}' not in FIELD_SPECS "
                    f"(available: {sorted(field_spec_names)})"
                )

        # --- Check 5: Bundle schema cross-check (if bundle available) ---
        if bundle is not None:
            contracts = bundle.get("contracts", {})
            bundle_contract = contracts.get(subtype)
            if bundle_contract is None:
                warnings.append(
                    f"NOTICE: subtype '{subtype}' not found in bundle.json contracts"
                )
            else:
                bundle_schema = bundle_contract.get("schema", {})
                bundle_props = set(bundle_schema.get("properties", {}).keys())

                for rule in gate_rules:
                    if rule.field not in bundle_props:
                        violations.append(
                            f"SHADOW [gate_rule_vs_bundle]: "
                            f"{module_name} GateRule '{rule.name}' checks field "
                            f"'{rule.field}' which is NOT in bundle schema for "
                            f"'{subtype}' (bundle has: {sorted(bundle_props)})"
                        )

                # Also check: contract SCHEMA_PROPERTIES keys vs bundle schema keys
                contract_only = schema_keys - bundle_props
                bundle_only = bundle_props - schema_keys
                if contract_only:
                    warnings.append(
                        f"DRIFT: {subtype} — contract has keys not in bundle: "
                        f"{sorted(contract_only)}"
                    )
                if bundle_only:
                    warnings.append(
                        f"DRIFT: {subtype} — bundle has keys not in contract: "
                        f"{sorted(bundle_only)}"
                    )

    # Print warnings
    for w in warnings:
        print(f"  {w}")

    return violations


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("check_shadow_contracts: detecting shadow contracts...")
    print()

    violations = check_shadow_contracts()

    print()
    if violations:
        print(f"FAIL: {len(violations)} shadow contract violation(s)")
        for v in violations:
            print(f"  {v}")
        sys.exit(1)
    else:
        print("PASS: no shadow contracts detected")
        sys.exit(0)


if __name__ == "__main__":
    main()
