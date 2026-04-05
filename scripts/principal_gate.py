"""
principal_gate.py — Phase-specific principal enforcement.

Each phase has a minimum set of required principals.
If the agent's PhaseRecord doesn't declare them, emit a violation.

Violation is a redirect hint injection, not a fatal error.
The main flow is always wrapped in try/except to ensure robustness.
"""

from __future__ import annotations

# Load required principals from canonical source (subtype_contracts, p193).
# Exception-safe: if import fails, fallback to static dict (no crash).
try:
    from subtype_contracts import (
        get_required_principals as _get_rp,
        get_repair_target as _get_rt,
        SUBTYPE_CONTRACTS as _SC,
    )
    # Build PHASE_REQUIRED_PRINCIPALS from contracts for backward compatibility
    # (test_principal_gate.py imports this dict directly).
    PHASE_REQUIRED_PRINCIPALS: dict[str, list[str]] = {
        "OBSERVE":  _get_rp("OBSERVE"),
        "ANALYZE":  _get_rp("ANALYZE"),
        "EXECUTE":  _get_rp("EXECUTE"),
        "JUDGE":    _get_rp("JUDGE"),
    }
    # Build PHASE_VIOLATION_REDIRECT from contracts
    PHASE_VIOLATION_REDIRECT: dict[str, str] = {
        phase: _get_rt(phase)
        for phase in ["ANALYZE", "EXECUTE", "JUDGE"]
        if _get_rt(phase)
    }
    # Export get_required_principals for callers who prefer the function API
    def get_required_principals(phase: str) -> list[str]:
        """Return required principals for phase from SUBTYPE_CONTRACTS."""
        return _get_rp(phase)

except Exception:
    # Fallback: static dicts (ensures no crash if subtype_contracts unavailable)
    PHASE_REQUIRED_PRINCIPALS = {
        "OBSERVE":  [],
        "ANALYZE":  ["causal_grounding"],
        "EXECUTE":  ["minimal_change"],
        "JUDGE":    ["invariant_preservation"],
    }
    PHASE_VIOLATION_REDIRECT = {
        "ANALYZE":  "OBSERVE",
        "EXECUTE":  "ANALYZE",
        "JUDGE":    "EXECUTE",
    }

    def get_required_principals(phase: str) -> list[str]:
        """Return required principals for phase (fallback static version)."""
        return PHASE_REQUIRED_PRINCIPALS.get(phase.upper(), [])

# Human-readable feedback for each violation
_FEEDBACK: dict[str, str] = {
    "missing_causal_grounding": (
        "Your analysis must identify a root cause with causal evidence. "
        "Declare PRINCIPALS: causal_grounding"
    ),
    "missing_minimal_change": (
        "Your patch must be scoped to the minimum change. "
        "Declare PRINCIPALS: minimal_change"
    ),
    "missing_invariant_preservation": (
        "Your judge output must verify an invariant was preserved. "
        "Declare PRINCIPALS: invariant_preservation"
    ),
}


def check_principal_gate(phase_record, phase: str) -> str | None:
    """
    Check if the PhaseRecord satisfies required principals for the given phase.

    Returns violation string (e.g. 'missing_causal_grounding') if violated,
    None if OK or no enforcement for this phase.

    Args:
        phase_record: PhaseRecord or any object with a .principals list attribute
        phase: Phase name string (e.g. 'ANALYZE', 'EXECUTE', 'JUDGE')
    """
    required = PHASE_REQUIRED_PRINCIPALS.get(phase.upper(), [])
    if not required:
        return None

    declared = [p.lower() for p in (getattr(phase_record, "principals", None) or [])]
    for req in required:
        if req not in declared:
            return f"missing_{req}"
    return None


def check_principal_inference(phase_record, phase: str) -> str | None:
    """
    p194: System-inferred principal check (three-way diff).

    Infers principals from PhaseRecord behavior and diffs against declared principals.
    Returns a violation string or None.

    Violation types:
      fake_principal:<name,...>     — declared but not inferred (hard reject)
      missing_required:<name,...>   — required by contract but not declared (hard reject)
      None                          — clean or only missing_expected (soft warn only)

    Exception-safe: any import or inference failure returns None (no crash).

    Args:
        phase_record: PhaseRecord or any object with behavioral attributes
        phase: Phase name string (e.g. 'ANALYZE', 'EXECUTE', 'JUDGE')
    """
    try:
        from principal_inference import infer_principals, diff_principals
        _inferred = infer_principals(phase_record)
        _diff = diff_principals(
            getattr(phase_record, "principals", []) or [],
            _inferred,
            phase=phase,
        )
        if _diff["fake"]:
            print(
                f"    [principal_inference] fake={_diff['fake']} inferred={_inferred}",
                flush=True,
            )
            return f"fake_principal:{','.join(_diff['fake'])}"
        elif _diff["missing_required"]:
            print(
                f"    [principal_inference] missing_required={_diff['missing_required']}"
                f" inferred={_inferred}",
                flush=True,
            )
            return f"missing_required:{','.join(_diff['missing_required'])}"
        else:
            if _diff["missing_expected"]:
                print(
                    f"    [principal_inference] missing_expected={_diff['missing_expected']}"
                    f" inferred={_inferred}",
                    flush=True,
                )
            else:
                print(
                    f"    [principal_inference] match inferred={_inferred}",
                    flush=True,
                )
            return None
    except Exception as _inf_e:
        print(f"    [principal_inference] error={_inf_e}", flush=True)
        return None


def get_principal_feedback(violation: str) -> str:
    """Return human-readable feedback for a principal violation."""
    return _FEEDBACK.get(
        violation,
        f"Principal violation: {violation}. Declare required principals for this phase.",
    )
