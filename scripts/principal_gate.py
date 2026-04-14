"""
principal_gate.py — Phase-specific principal enforcement.

Each phase has a minimum set of required principals.
If the agent's PhaseRecord doesn't declare them, emit a violation.

Violation is a redirect hint injection, not a fatal error.
The main flow is always wrapped in try/except to ensure robustness.
"""

from __future__ import annotations

from gate_rejection import (
    GateRejection, ContractView, FieldSpec, FieldFailure,
    build_gate_rejection, SDG_ENABLED,
)
from gate_failure_code import (
    GateFailureCode, GateFailureCategory, GateFailureSeverity,
    missing_principal, forbidden_principal, missing_field,
    semantic_fail, forbidden_transition, get_repair_hint,
)
from routing_decision import AdmissionStatus, RoutingDecision

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
    # EF-5: PHASE_VIOLATION_REDIRECT deleted — routing now via RoutingDecision.
    # Consumers use AdmissionResult.routing or subtype_contracts.get_repair_target().
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
    def get_required_principals(phase: str) -> list[str]:
        """Return required principals for phase (fallback static version)."""
        return PHASE_REQUIRED_PRINCIPALS.get(phase.upper(), [])

# Repair hints now unified via get_repair_hint() in gate_failure_code.py (EF-4).
# _FEEDBACK dict deleted — bundle repair_templates is the single source of truth.


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


def get_principal_feedback(violation: str, bundle: dict | None = None) -> str:
    """Return human-readable feedback for a principal violation.

    Uses get_repair_hint() from gate_failure_code if a GateFailureCode is available
    via bundle lookup. Falls back to a generic message.
    """
    # Try bundle-based hint via gate_failure_code
    if bundle and violation.startswith("missing_"):
        principal = violation.replace("missing_", "", 1)
        try:
            from gate_failure_code import get_repair_hint, missing_principal
            fc = missing_principal(principal, "", "")
            hint = get_repair_hint(fc, bundle)
            if hint:
                return hint
        except Exception:
            pass
    return f"Principal violation: {violation}. Declare required principals for this phase."


# ── AdmissionResult (v2 — EF-5) ──────────────────────────────────────────────

class AdmissionResult:
    """
    Verdict from evaluate_admission().

    status:
      ADMITTED   — all contracts satisfied; phase may proceed / transition
      RETRYABLE  — missing material (principal or field); redirect to repair phase
      REJECTED   — phase boundary error (forbidden transition / structural mismatch);
                   do not redirect, stop attempt
      ESCALATED  — system-level issue requiring external intervention

    Taxonomy rule:
      RETRYABLE: right phase, incomplete output — agent can fix in-loop
      REJECTED:  wrong phase position or boundary violation — no in-loop fix possible

    v2 changes (EF-5):
      - status: AdmissionStatus enum (backward-compatible str enum)
      - reasons: list[GateFailureCode] (typed failure codes)
      - reasons_legacy: property returning list[str] for backward compat
      - routing: RoutingDecision | None — where to redirect on failure
    """
    __slots__ = ("status", "reasons", "rejection", "routing")

    def __init__(
        self,
        status: str | AdmissionStatus,
        reasons: list[str] | list[GateFailureCode],
        rejection: GateRejection | None = None,
        routing: RoutingDecision | None = None,
    ) -> None:
        # Accept both str and AdmissionStatus for backward compat
        if isinstance(status, str) and not isinstance(status, AdmissionStatus):
            try:
                self.status: str | AdmissionStatus = AdmissionStatus(status)
            except ValueError:
                self.status = status  # fallback for unknown status strings
        else:
            self.status = status
        self.reasons: list = reasons  # GateFailureCode list (or legacy str list)
        self.rejection = rejection
        self.routing = routing

    @property
    def reasons_legacy(self) -> list[str]:
        """Backward-compatible string reason codes.

        Returns list[str] regardless of whether reasons contains
        GateFailureCode objects or plain strings.
        """
        result = []
        for r in self.reasons:
            if isinstance(r, GateFailureCode):
                result.append(r.code)
            else:
                result.append(str(r))
        return result

    def __repr__(self) -> str:
        return f"AdmissionResult({self.status}, {self.reasons_legacy})"


def _build_principal_contract(phase: str) -> ContractView:
    """Build a ContractView for the principal/admission gate of a given phase."""
    required = PHASE_REQUIRED_PRINCIPALS.get(phase.upper(), [])
    field_specs = {}
    for p in required:
        field_specs[p] = FieldSpec(
            description=f"Declare {p}",
            required=True,
            semantic_check="principal_declared",
        )
    # Add required fields from subtype_contracts if available
    try:
        from subtype_contracts import get_required_fields as _get_rf
        for f_name in _get_rf(phase):
            field_specs[f_name] = FieldSpec(
                description=f"Required field: {f_name}",
                required=True,
            )
    except Exception:
        pass
    all_required = list(required)
    try:
        from subtype_contracts import get_required_fields as _get_rf2
        all_required.extend(_get_rf2(phase))
    except Exception:
        pass
    return ContractView(required_fields=all_required, field_specs=field_specs)


def _build_admission_rejection(
    phase: str, reasons: list[str], phase_record, status: str,
) -> GateRejection | None:
    """Build GateRejection from admission check reasons (p217 SDG)."""
    if not SDG_ENABLED or not reasons:
        return None

    contract = _build_principal_contract(phase)
    failures = []
    extracted = {}

    # Extract declared principals for the extracted dict
    declared = [p.lower() for p in (getattr(phase_record, "principals", None) or [])]
    extracted["declared_principals"] = declared

    for reason_code in reasons:
        if reason_code.startswith("missing_required_principal:"):
            principal = reason_code.split(":", 1)[1]
            failures.append(FieldFailure(
                field=principal,
                reason="principal_violation",
                hint=f"Declare {principal}",
                expected=f"Principal '{principal}' must be declared",
                actual=None,
            ))
        elif reason_code.startswith("forbidden_principal:"):
            principal = reason_code.split(":", 1)[1]
            failures.append(FieldFailure(
                field=principal,
                reason="principal_violation",
                hint=f"Principal '{principal}' is forbidden in this phase",
                expected=f"Principal '{principal}' must NOT be declared",
                actual=principal,
            ))
        elif reason_code.startswith("missing_required_field:"):
            field_name = reason_code.split(":", 1)[1]
            failures.append(FieldFailure(
                field=field_name,
                reason="missing",
                hint=f"Required field '{field_name}' must be non-empty",
                expected=f"Non-empty {field_name}",
                actual=None,
            ))
        elif reason_code.startswith("missing_"):
            # Generic missing field (e.g. missing_root_cause, missing_plan)
            field_name = reason_code.replace("missing_", "")
            failures.append(FieldFailure(
                field=field_name,
                reason="missing",
                hint=f"Provide {field_name}",
                expected=f"Non-empty {field_name}",
                actual=None,
            ))
        elif reason_code.startswith("plan_not_grounded"):
            failures.append(FieldFailure(
                field="plan",
                reason="semantic_fail",
                hint="Ground plan in root cause",
                expected="Plan must reference the root cause from ANALYZE",
                actual=getattr(phase_record, "plan", "")[:80] if hasattr(phase_record, "plan") else None,
            ))
        elif reason_code.startswith("forbidden_transition:"):
            transition = reason_code.split(":", 1)[1]
            failures.append(FieldFailure(
                field="phase_transition",
                reason="format_invalid",
                hint=f"Phase transition {transition} is not allowed",
                expected="Allowed phase transition",
                actual=transition,
            ))
        elif reason_code.startswith("missing_evidence_basis"):
            failures.append(FieldFailure(
                field="evidence_basis",
                reason="missing",
                hint="Provide evidence_refs, from_steps, or use observation tools",
                expected="Evidence basis (evidence_refs or from_steps or tool usage)",
                actual=None,
            ))
        else:
            # Catch-all for unknown reason codes
            failures.append(FieldFailure(
                field=reason_code,
                reason="format_invalid",
                hint=f"Violation: {reason_code}",
                expected="No violation",
                actual=reason_code,
            ))

    return build_gate_rejection(
        gate_name="principal_gate",
        contract=contract,
        extracted=extracted,
        failures=failures,
    )


def evaluate_admission(phase_record, phase: str, next_phase: str = "", observe_tool_signal: bool = False, last_analyze_root_cause: str = "", structured_output: bool = False) -> AdmissionResult:
    """
    Full admission check for a PhaseRecord at phase boundary.

    Checks (in order):
      1. required principals  — missing → RETRYABLE
      2. forbidden principals — declared → REJECTED (fake principal / phase boundary violation)
      3. required fields      — missing → RETRYABLE (skipped when structured_output=True)
      4. allowed_next         — forbidden transition → REJECTED (only if next_phase provided)

    Returns AdmissionResult(ADMITTED / RETRYABLE / REJECTED, reasons).
    Exception-safe: any error returns ADMITTED (no crash, no false stop).

    Args:
        phase_record: PhaseRecord or object with .principals / field attributes
        phase:        current phase name (e.g. "ANALYZE")
        next_phase:   proposed next phase (e.g. "EXECUTE"); "" = skip transition check
        structured_output: When True (p221), skip structural field presence checks
            (steps 3, 3a, 3b) — JSON schema already enforces them.
            Principal checks (steps 1, 2) and transition checks (step 4) still apply.
    """
    try:
        # Resolve subtype for typed failure codes
        _subtype = ""
        try:
            from subtype_contracts import _PHASE_TO_SUBTYPE as _PTS
            _subtype = _PTS.get(phase.upper(), "")
        except Exception:
            pass

        retryable_codes: list[GateFailureCode] = []
        rejected_codes: list[GateFailureCode] = []
        # Legacy string lists maintained for _build_admission_rejection compatibility
        retryable: list[str] = []
        rejected: list[str] = []

        # 1. required principals (RETRYABLE)
        required = PHASE_REQUIRED_PRINCIPALS.get(phase.upper(), [])
        declared = [p.lower() for p in (getattr(phase_record, "principals", None) or [])]
        for req in required:
            if req not in declared:
                retryable_codes.append(missing_principal(req, phase.upper(), _subtype))
                retryable.append(f"missing_required_principal:{req}")

        # 2. forbidden principals (REJECTED — fake principal / phase boundary violation)
        try:
            from subtype_contracts import get_forbidden_principals as _get_fp
            forbidden_list = _get_fp(phase)
        except Exception:
            forbidden_list = []
        for fp in forbidden_list:
            if fp in declared:
                rejected_codes.append(forbidden_principal(fp, phase.upper(), _subtype))
                rejected.append(f"forbidden_principal:{fp}")

        # Steps 3, 3a, 3b: structural field presence checks.
        # When structured_output=True (p221), JSON schema already enforces field presence
        # and min_length — skip these structural checks, keep only semantic checks.
        if not structured_output:
            # 3. required fields (RETRYABLE)
            try:
                from subtype_contracts import get_required_fields as _get_rf
                req_fields = _get_rf(phase)
            except Exception:
                req_fields = []
            for field_name in req_fields:
                val = getattr(phase_record, field_name, None)
                if not val:  # None, [], "", 0 all count as missing
                    retryable_codes.append(missing_field(field_name, phase.upper(), _subtype))
                    retryable.append(f"missing_required_field:{field_name}")

            # 3a. structured fields check (RETRYABLE) — ANALYZE requires root_cause (p23)
            if phase.upper() == "ANALYZE":
                _rc = getattr(phase_record, "root_cause", None) or ""
                if not _rc:
                    retryable_codes.append(missing_field("root_cause", phase.upper(), _subtype, gate_rule="check_root_cause"))
                    retryable.append("missing_root_cause")

            # 3b. has_evidence_basis check (RETRYABLE)
            try:
                from subtype_contracts import SUBTYPE_CONTRACTS as _SC, _PHASE_TO_SUBTYPE as _PTS2
                _subtype2 = _PTS2.get(phase.upper(), "")
                _contract = _SC.get(_subtype2, {})
                if _contract.get("has_evidence_basis_required"):
                    _evidence_refs = getattr(phase_record, "evidence_refs", None) or []
                    _from_steps = getattr(phase_record, "from_steps", None) or []
                    if not _evidence_refs and not _from_steps and not observe_tool_signal:
                        from gate_failure_code import GateFailureCode as _GFC, GateFailureCategory as _GFCat, GateFailureSeverity as _GFSev
                        retryable_codes.append(_GFC(
                            category=_GFCat.MISSING_EVIDENCE_BASIS,
                            subcode="evidence_refs",
                            severity=_GFSev.RETRYABLE,
                            gate_rule="check_evidence_basis",
                            phase=phase.upper(),
                            subtype=_subtype2,
                        ))
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
                rejected_codes.append(forbidden_transition(phase.upper(), next_phase.upper(), _subtype))
                rejected.append(f"forbidden_transition:{phase}->{next_phase}")

        # Build RoutingDecision for non-ADMITTED results
        def _build_routing(codes: list[GateFailureCode], status: AdmissionStatus) -> RoutingDecision | None:
            if status == AdmissionStatus.ADMITTED:
                return None
            # Determine next_phase from first failure code's repair_target
            _next = "ANALYZE"  # fallback
            _source = "default_route"
            _hints: list[str] = []
            # Try bundle-based routing
            try:
                from jingu_onboard import onboard as _onb
                _bundle = _onb()
                if hasattr(_bundle, "bundle"):
                    _bdata = _bundle.bundle
                elif isinstance(_bundle, dict):
                    _bdata = _bundle
                else:
                    _bdata = None
            except Exception:
                _bdata = None

            for code in codes:
                _target = code.repair_target(_bdata)
                if _target and _target != code.phase:
                    _next = _target
                    _source = "principal_route"
                    break
                elif _target:
                    _next = _target

            # Collect repair hints from all codes
            for code in codes:
                _h = code.repair_hint(_bdata) if _bdata else ""
                if _h:
                    _hints.append(_h)

            return RoutingDecision(
                next_phase=_next,
                strategy=status.value.lower(),
                repair_hints=_hints,
                source=_source,
            )

        if rejected_codes:
            all_codes = rejected_codes + retryable_codes
            all_reasons = rejected + retryable
            rejection_obj = _build_admission_rejection(phase, all_reasons, phase_record, "REJECTED")
            _routing = _build_routing(all_codes, AdmissionStatus.REJECTED)
            return AdmissionResult(
                AdmissionStatus.REJECTED, all_codes,
                rejection=rejection_obj, routing=_routing,
            )
        if retryable_codes:
            rejection_obj = _build_admission_rejection(phase, retryable, phase_record, "RETRYABLE")
            _routing = _build_routing(retryable_codes, AdmissionStatus.RETRYABLE)
            return AdmissionResult(
                AdmissionStatus.RETRYABLE, retryable_codes,
                rejection=rejection_obj, routing=_routing,
            )
        return AdmissionResult(AdmissionStatus.ADMITTED, [])

    except Exception as _e:
        # Safety: never crash the caller; treat as admitted on unexpected error
        return AdmissionResult(AdmissionStatus.ADMITTED, [f"admission_check_error:{_e}"])
