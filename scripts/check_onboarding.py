#!/usr/bin/env python3
"""
check_onboarding.py — CI script to validate onboarding completeness (Phase 3 thin wrapper).

Delegates all validation to bundle_compiler.compile_bundle() which runs the full
8-stage pipeline and emits an ActivationReport (RT4 activation proof).

Usage:
    python scripts/check_onboarding.py
    python scripts/check_onboarding.py --bundle path/to/bundle.json

Exit codes:
    0 — all checks pass (compile_bundle succeeded)
    1 — one or more checks failed (CompilationError raised)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Ensure scripts/ is importable
sys.path.insert(0, str(Path(__file__).parent))

from bundle_compiler import compile_bundle, CompilationError


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CI check for onboarding completeness (delegates to compile_bundle)",
    )
    parser.add_argument(
        "--bundle",
        default=None,
        help="Path to bundle.json (default: JINGU_BUNDLE_PATH env or bundle.json in project root)",
    )
    args = parser.parse_args()

    bundle_path = args.bundle
    if bundle_path:
        os.environ["JINGU_BUNDLE_PATH"] = bundle_path

    try:
        bundle = compile_bundle(bundle_path, force_reload=True)
        report = bundle.activation_report
        print(
            f"[check_onboarding] OK: {report.contracts_compiled} contracts compiled "
            f"(bundle v{report.bundle_version}, "
            f"phases={report.phases_compiled})"
        )
        sys.exit(0)
    except CompilationError as e:
        print(f"[check_onboarding] FAILED: {e}")
        sys.exit(1)
    except FileNotFoundError as e:
        print(f"[check_onboarding] FAILED: bundle not found — {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
