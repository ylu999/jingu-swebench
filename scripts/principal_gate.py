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
        "JUDGE":    ["result_verification", "uncertainty_honesty"],
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
    "missing_result_verification": (
        "Your judge output must verify the actual result. "
        "Declare PRINCIPALS: result_verification"
    ),
    "missing_uncertainty_honesty": (
        "Your judge output must express honest uncertainty. "
        "Declare PRINCIPALS: uncertainty_honesty"
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
    p195: System-inferred principal check (three-way diff) using rich inference result.

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
        from principal_inference import run_inference, diff_principals, InferredPrincipalResult
        from subtype_contracts import _PHASE_TO_SUBTYPE
        _subtype = _PHASE_TO_SUBTYPE.get(phase.upper(), "")
        _rich_result = run_inference(phase_record, _subtype)
        _diff = diff_principals(
            getattr(phase_record, "principals", []) or [],
            _rich_result,
            phase=phase,
        )
        if _diff["fake"]:
            # 改动4: only treat fake as hard violation if the principal is required.
            # Expected-only fake (declared but not behaviorally supported, not required)
            # → warn only, do NOT trigger RETRYABLE redirect.
            # This prevents soft principals (e.g. alternative_hypothesis_check) from
            # blocking the main path after required principals already passed.
            try:
                _required_set = {p.lower() for p in (get_required_principals(phase) or [])}
            except Exception:
                _required_set = set()
            _hard_fake = [p for p in _diff["fake"] if p in _required_set]
            _soft_fake = [p for p in _diff["fake"] if p not in _required_set]
            if _soft_fake:
                print(
                    f"    [principal_inference] fake_soft={_soft_fake}"
                    f" (expected-only, warn-only) inferred={_rich_result.present}",
                    flush=True,
                )
            if _hard_fake:
                print(
                    f"    [principal_inference] fake={_hard_fake} inferred={_rich_result.present}",
                    flush=True,
                )
                return f"fake_principal:{','.join(_hard_fake)}"
        elif _diff["missing_required"]:
            print(
                f"    [principal_inference] missing_required={_diff['missing_required']}"
                f" inferred={_rich_result.present}",
                flush=True,
            )
            return f"missing_required:{','.join(_diff['missing_required'])}"
        else:
            if _diff["missing_expected"]:
                _details = {
                    p: _rich_result.details.get(p)
                    for p in _diff["missing_expected"]
                    if p in _rich_result.details
                }
                print(
                    f"    [principal_inference] missing_expected={_diff['missing_expected']}"
                    f" signals={_details}",
                    flush=True,
                )
            else:
                print(
                    f"    [principal_inference] match inferred={_rich_result.present}",
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


# ── AdmissionResult (v0.4) ────────────────────────────────────────────────────

class AdmissionResult:
    """
    Verdict from evaluate_admission().

    status:
      ADMITTED   — all contracts satisfied; phase may proceed / transition
      RETRYABLE  — missing material (principal or field); redirect to repair phase
      REJECTED   — phase boundary error (forbidden transition / structural mismatch);
                   do not redirect, stop attempt

    Taxonomy rule:
      RETRYABLE: right phase, incomplete output — agent can fix in-loop
      REJECTED:  wrong phase position or boundary violation — no in-loop fix possible
    """
    __slots__ = ("status", "reasons")

    def __init__(self, status: str, reasons: list[str]) -> None:
        self.status = status    # "ADMITTED" | "RETRYABLE" | "REJECTED"
        self.reasons = reasons  # violation codes

    def __repr__(self) -> str:
        return f"AdmissionResult({self.status}, {self.reasons})"


def evaluate_admission(phase_record, phase: str, next_phase: str = "") -> AdmissionResult:
    """
    Full admission check for a PhaseRecord at phase boundary.

    Checks (in order):
      1. required principals  — missing → RETRYABLE
      2. forbidden principals — declared → REJECTED (fake principal / phase boundary violation)
      3. required fields      — missing → RETRYABLE
      4. allowed_next         — forbidden transition → REJECTED (only if next_phase provided)

    Returns AdmissionResult(ADMITTED / RETRYABLE / REJECTED, reasons).
    Exception-safe: any error returns ADMITTED (no crash, no false stop).

    Args:
        phase_record: PhaseRecord or object with .principals / field attributes
        phase:        current phase name (e.g. "ANALYZE")
        next_phase:   proposed next phase (e.g. "EXECUTE"); "" = skip transition check
    """
    try:
        retryable: list[str] = []
        rejected: list[str] = []

        # 1. required principals (RETRYABLE)
        required = PHASE_REQUIRED_PRINCIPALS.get(phase.upper(), [])
        declared = [p.lower() for p in (getattr(phase_record, "principals", None) or [])]
        for req in required:
            if req not in declared:
                retryable.append(f"missing_required_principal:{req}")

        # 2. forbidden principals (REJECTED — fake principal / phase boundary violation)
        try:
            from subtype_contracts import get_forbidden_principals as _get_fp
            forbidden = _get_fp(phase)
        except Exception:
            forbidden = []
        for fp in forbidden:
            if fp in declared:
                rejected.append(f"forbidden_principal:{fp}")

        # 3. required fields (RETRYABLE)
        try:
            from subtype_contracts import get_required_fields as _get_rf
            req_fields = _get_rf(phase)
        except Exception:
            req_fields = []
        for field_name in req_fields:
            val = getattr(phase_record, field_name, None)
            if not val:  # None, [], "", 0 all count as missing
                retryable.append(f"missing_required_field:{field_name}")

        # 3b. has_evidence_basis check (RETRYABLE) — for phases that require evidence basis
        # but NOT specifically file.py:line regex matches (e.g. ANALYZE).
        # P16 fix: ANALYZE requires evidence *basis* (evidence_refs OR from_steps non-empty),
        # not a hard evidence_refs field check. This separates representational artifact
        # (regex-extracted file refs) from cognition requirement (evidence grounding).
        try:
            from subtype_contracts import SUBTYPE_CONTRACTS as _SC, _PHASE_TO_SUBTYPE as _PTS
            _subtype = _PTS.get(phase.upper(), "")
            _contract = _SC.get(_subtype, {})
            if _contract.get("has_evidence_basis_required"):
                _evidence_refs = getattr(phase_record, "evidence_refs", None) or []
                _from_steps = getattr(phase_record, "from_steps", None) or []
                if not _evidence_refs and not _from_steps:
                    retryable.append("missing_evidence_basis")
        except Exception:
            pass

        # 4. allowed_next transition check (REJECTED — boundary error)
        if next_phase:
            try:
                from subtype_contracts import get_allowed_next as _get_an
                allowed = _get_an(phase)
            except Exception:
                allowed = []
            if allowed and next_phase.upper() not in [p.upper() for p in allowed]:
                rejected.append(f"forbidden_transition:{phase}->{next_phase}")

        if rejected:
            return AdmissionResult("REJECTED", rejected + retryable)
        if retryable:
            return AdmissionResult("RETRYABLE", retryable)
        return AdmissionResult("ADMITTED", [])

    except Exception as _e:
        # Safety: never crash the caller; treat as admitted on unexpected error
        return AdmissionResult("ADMITTED", [f"admission_check_error:{_e}"])
