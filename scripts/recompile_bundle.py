#!/usr/bin/env python3
"""
recompile_bundle.py — Recompile bundle.json from cognition_contracts source files.

Reads all contract modules in cognition_contracts/, compiles each via
_compiler.compile_contract(), and patches the contracts section of bundle.json.
Non-contract sections (version, phases, cognition, capabilities) are preserved.

Usage:
    python scripts/recompile_bundle.py [--bundle path/to/bundle.json] [--check]

    --check: dry-run mode — report drift without writing. Exit 1 if drift found.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from datetime import datetime, timezone

# Ensure scripts/ is on path
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from cognition_contracts._compiler import compile_contract, BundleContractOutput

# ── Contract module registry ────────────────────────────────────────────────
# Maps bundle.json contract key -> Python module name under cognition_contracts/
_CONTRACT_MODULES: dict[str, str] = {
    "observation.fact_gathering": "cognition_contracts.observation_fact_gathering",
    "analysis.root_cause": "cognition_contracts.analysis_root_cause",
    "decision.fix_direction": "cognition_contracts.decision_fix_direction",
    "design.solution_shape": "cognition_contracts.design_solution_shape",
    "execution.code_patch": "cognition_contracts.execution_code_patch",
    "judge.verification": "cognition_contracts.judge_verification",
}


def _output_to_dict(output: BundleContractOutput, phase: str, subtype: str) -> dict:
    """Convert BundleContractOutput to the bundle.json contract entry format."""
    return {
        "phase": phase,
        "subtype": subtype,
        "compiled_at": datetime.now(timezone.utc).isoformat(),
        "phase_spec": output.phase_spec,
        "cognition_spec": output.cognition_spec,
        "principals": output.principals,
        "policy": output.policy,
        "schema": output.schema,
        "prompt": output.prompt,
        "repair_templates": output.repair_templates,
        "routing": output.routing,
    }


def recompile_contracts(bundle_path: str, *, check_only: bool = False) -> list[str]:
    """Recompile all contracts and update bundle.json.

    Returns list of changed contract keys. Empty = no changes.
    """
    # Load existing bundle
    with open(bundle_path) as f:
        bundle = json.load(f)

    changes: list[str] = []

    for contract_key, module_name in _CONTRACT_MODULES.items():
        # Import (or reload) the contract module
        mod = importlib.import_module(module_name)
        importlib.reload(mod)  # Always reload to pick up source changes

        # Compile
        output = compile_contract(mod)
        new_entry = _output_to_dict(output, mod.PHASE, mod.SUBTYPE)

        # Compare with existing
        old_entry = bundle.get("contracts", {}).get(contract_key, {})

        # Compare schema (the most important part for drift detection)
        old_schema = old_entry.get("schema", {})
        new_schema = new_entry["schema"]
        old_policy = old_entry.get("policy", {})
        new_policy = new_entry["policy"]

        schema_changed = (
            sorted(old_schema.get("properties", {}).keys())
            != sorted(new_schema.get("properties", {}).keys())
            or sorted(old_schema.get("required", []))
            != sorted(new_schema.get("required", []))
        )
        policy_changed = old_policy != new_policy

        if schema_changed or policy_changed:
            changes.append(contract_key)
            if not check_only:
                bundle.setdefault("contracts", {})[contract_key] = new_entry
            # Report what changed
            if schema_changed:
                old_props = set(old_schema.get("properties", {}).keys())
                new_props = set(new_schema.get("properties", {}).keys())
                added = new_props - old_props
                removed = old_props - new_props
                old_req = set(old_schema.get("required", []))
                new_req = set(new_schema.get("required", []))
                req_added = new_req - old_req
                req_removed = old_req - new_req
                parts = []
                if added:
                    parts.append(f"props+={sorted(added)}")
                if removed:
                    parts.append(f"props-={sorted(removed)}")
                if req_added:
                    parts.append(f"required+={sorted(req_added)}")
                if req_removed:
                    parts.append(f"required-={sorted(req_removed)}")
                print(f"  [{contract_key}] schema: {', '.join(parts)}")
        else:
            # Still update compiled_at and other sections that may have changed
            if not check_only:
                bundle.setdefault("contracts", {})[contract_key] = new_entry

    if not check_only:
        # Update metadata
        bundle["generated_at"] = datetime.now(timezone.utc).isoformat()

        # Write back
        with open(bundle_path, "w") as f:
            json.dump(bundle, f, indent=2, ensure_ascii=False)
            f.write("\n")

    return changes


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Recompile bundle.json from contract sources")
    parser.add_argument(
        "--bundle",
        default=os.path.join(os.path.dirname(_SCRIPTS_DIR), "bundle.json"),
        help="Path to bundle.json (default: repo root bundle.json)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check for drift without writing (exit 1 if drift found)",
    )
    args = parser.parse_args()

    if not os.path.exists(args.bundle):
        print(f"ERROR: bundle.json not found at {args.bundle}", file=sys.stderr)
        sys.exit(1)

    mode = "check" if args.check else "recompile"
    print(f"[recompile_bundle] {mode}: {args.bundle}")

    changes = recompile_contracts(args.bundle, check_only=args.check)

    if changes:
        print(f"[recompile_bundle] {'drift detected in' if args.check else 'updated'}: {changes}")
        if args.check:
            sys.exit(1)
    else:
        print("[recompile_bundle] no schema changes detected")


if __name__ == "__main__":
    main()
