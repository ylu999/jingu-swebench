"""
jingu_onboard.py — Thin shim for backward compatibility.

All governance types and logic have been migrated to jingu-bundle-loader
(jingu_loader package). This file re-exports public API and keeps the
onboard() entry point which depends on bundle_compiler (swebench-specific).

Migration: p227-04
"""

from __future__ import annotations

import sys
import os
from typing import Any

# Add jingu_loader to sys.path.
# Priority 1: Docker/ECS — /app/python/ (COPY'd in Dockerfile)
# Priority 2: Local dev — sibling repo jingu-bundle-loader/python/
_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "..", "python"),             # Docker: /app/python/
    os.path.join(os.path.dirname(__file__), "..", "..", "jingu-bundle-loader", "python"),  # local dev
]
for _candidate in _CANDIDATES:
    if os.path.isdir(_candidate) and _candidate not in sys.path:
        sys.path.insert(0, _candidate)
        break

# Re-export all public types from jingu_loader
from jingu_loader import (  # noqa: E402
    JinguGovernance,
    PhaseConfig,
    PhaseGate,
    PrincipalSpec,
    Route,
    CognitionSpec,
    build_governance_from_compiled,
    parse_contract,
    adapt_schema_for_constrained_decoding,
    validate_adapted_schema,
)

# Backward-compatible aliases for private names used by bundle_compiler
_build_governance_from_compiled = build_governance_from_compiled
_parse_contract = parse_contract
_adapt_schema_for_constrained_decoding = adapt_schema_for_constrained_decoding
_validate_adapted_schema = validate_adapted_schema


# ── The single entry point (swebench-specific, depends on bundle_compiler) ──

def onboard(bundle_path: "str | None" = None, *, force_reload: bool = False) -> JinguGovernance:
    """Backward-compatible entry point. Delegates to bundle_compiler.compile_bundle().

    Returns:
        JinguGovernance instance (cached by compile_bundle on first call).

    Raises:
        FileNotFoundError: If bundle.json does not exist.
        CompilationError: If bundle fails validation (replaces old ValueError).
    """
    from bundle_compiler import compile_bundle
    bundle = compile_bundle(bundle_path, force_reload=force_reload)
    return bundle.governance
