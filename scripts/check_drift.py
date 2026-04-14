#!/usr/bin/env python3
"""
check_drift.py — CI entry point: drift audit + shadow contract detection.

Loads bundle.json, runs drift_audit.audit_all_contracts() and
shadow_detector.scan_all(), reports violations, and exits with
appropriate code.

Exit 0: no violations (or --warn-only).
Exit 1: violations found.

Flags:
    --audit-only    Run only drift audit (skip shadow detector)
    --shadow-only   Run only shadow detector (skip drift audit)
    --warn-only     Report violations but always exit 0
    --bundle PATH   Path to bundle.json (default: ./bundle.json)
"""

from __future__ import annotations

import json
import os
import sys

# Ensure scripts/ is importable
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from drift_audit import audit_all_contracts, DriftViolation
from shadow_detector import scan_all, ShadowContractViolation


def _parse_args(argv: list[str]) -> dict:
    """Simple argument parser (no argparse dependency for CI speed)."""
    opts = {
        "audit_only": False,
        "shadow_only": False,
        "warn_only": False,
        "bundle_path": os.path.join(os.path.dirname(_SCRIPTS_DIR), "bundle.json"),
    }
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--audit-only":
            opts["audit_only"] = True
        elif arg == "--shadow-only":
            opts["shadow_only"] = True
        elif arg == "--warn-only":
            opts["warn_only"] = True
        elif arg == "--bundle" and i + 1 < len(argv):
            i += 1
            opts["bundle_path"] = argv[i]
        i += 1
    return opts


def _print_drift_violations(violations: list[DriftViolation]) -> None:
    """Print drift audit violations."""
    if not violations:
        print("  [drift_audit] PASS — no violations")
        return

    for v in violations:
        severity = "WARNING" if v.violation_type == "extra_in_b" else "ERROR"
        print(f"  [{severity}] {v.check_name}: {v.item}")
        print(f"    {v.detail}")


def _print_shadow_violations(violations: list[ShadowContractViolation]) -> None:
    """Print shadow detector violations."""
    if not violations:
        print("  [shadow_detector] PASS — no violations")
        return

    # Group by violation type
    by_type: dict[str, list[ShadowContractViolation]] = {}
    for v in violations:
        by_type.setdefault(v.violation_type, []).append(v)

    for vtype in sorted(by_type):
        items = by_type[vtype]
        print(f"  --- {vtype} ({len(items)}) ---")
        for v in items:
            relpath = os.path.relpath(v.file, _SCRIPTS_DIR)
            print(f"    {relpath}:{v.line}  {v.item}")


def main(argv: list[str] | None = None) -> int:
    """Main entry point. Returns exit code."""
    opts = _parse_args(argv or sys.argv[1:])

    print("=" * 60)
    print("check_drift — Contract Drift + Shadow Contract CI Check")
    print("=" * 60)

    total_violations = 0
    drift_violations: list[DriftViolation] = []
    shadow_violations: list[ShadowContractViolation] = []

    # ── Drift Audit ──────────────────────────────────────────────
    if not opts["shadow_only"]:
        print(f"\n[1/2] Drift Audit (bundle: {opts['bundle_path']})")
        try:
            with open(opts["bundle_path"]) as f:
                bundle = json.load(f)
            drift_violations = audit_all_contracts(bundle)
            _print_drift_violations(drift_violations)
            total_violations += len(drift_violations)
        except FileNotFoundError:
            print(f"  WARNING: {opts['bundle_path']} not found — skipping drift audit")
        except json.JSONDecodeError as e:
            print(f"  ERROR: Failed to parse {opts['bundle_path']}: {e}")
            return 1

    # ── Shadow Detector ──────────────────────────────────────────
    if not opts["audit_only"]:
        print(f"\n[2/2] Shadow Contract Detector (dir: {_SCRIPTS_DIR})")
        shadow_violations = scan_all(_SCRIPTS_DIR)
        _print_shadow_violations(shadow_violations)
        total_violations += len(shadow_violations)

    # ── Summary ──────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Total: {len(drift_violations)} drift + {len(shadow_violations)} shadow = {total_violations} violations")

    if opts["warn_only"]:
        print("(--warn-only: exiting 0 regardless)")
        return 0

    if total_violations > 0:
        print("EXIT 1 — violations found")
        return 1

    print("EXIT 0 — all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
