"""Tests for check_onboarding.py — Phase 3 thin wrapper around compile_bundle().

The old per-function checks (check_bundle_valid, check_all_phases_have_contracts, etc.)
have been removed. Equivalent coverage is now in test_bundle_compiler.py via
TestRegressionCheckOnboarding. This file only verifies the CLI wrapper behaviour.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from bundle_compiler import compile_bundle, CompilationError


# ── Integration test: real bundle.json ────────────────────────────────────────

class TestCheckOnboardingIntegration:
    def test_real_bundle_passes_if_available(self):
        """If bundle.json exists, compile_bundle() should succeed."""
        bundle_path = Path(__file__).parent.parent / "bundle.json"
        if not bundle_path.exists():
            pytest.skip("Real bundle.json not found")
        bundle = compile_bundle(str(bundle_path), force_reload=True)
        report = bundle.activation_report
        assert report.activation_ok, (
            f"compile_bundle failed: completeness_errors={report.completeness_errors}, "
            f"consistency_errors={report.consistency_errors}"
        )
        assert report.contracts_compiled > 0

    def test_missing_bundle_raises(self):
        """compile_bundle raises on missing bundle."""
        with pytest.raises((FileNotFoundError, CompilationError)):
            compile_bundle("/nonexistent/bundle.json", force_reload=True)
