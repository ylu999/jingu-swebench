"""
phase_validator.py — Validate PhaseRecord against CognitionLoader contracts.

Validates:
  1. Phase validity (known phase in cognition bundle)
  2. Subtype validity (known subtype in cognition bundle)
  3. Subtype-phase consistency (subtype belongs to declared phase)
  4. Required principals (missing principals from contract)
  5. Forbidden principals (principals that must NOT be declared)
  6. Evidence discipline (analysis phase must have evidence)
  7. Required fields from phase definition

All validation errors are self-describing (GateRejection pattern from p217).

Feature flag: COGNITION_EXECUTION_ENABLED from cognition_loader.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Phase 3: cognition_loader deleted. CognitionLoader stub for type annotations only.
from cognition_prompts import CognitionLoader
from phase_record import PhaseRecord
from gate_rejection import (
    GateRejection,
    ContractView,
    FieldFailure,
    FieldSpec,
    build_gate_rejection,
    build_repair_from_rejection,
)


# ── Validation Error ─────────────────────────────────────────────────────────

@dataclass
class ValidationError:
    """One specific validation failure with error code and details.

    Error codes:
      unknown_phase             — phase not in cognition bundle
      unknown_subtype           — subtype not in cognition bundle
      subtype_phase_mismatch    — subtype belongs to a different phase
      missing_principals        — required principals not declared
      forbidden_principals      — forbidden principals declared
      missing_evidence          — analysis phase missing evidence_refs
      missing_required_field    — required field is empty/missing
    """
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


# ── Validator ────────────────────────────────────────────────────────────────

def validate_phase_record(
    record: PhaseRecord,
    cognition_loader: CognitionLoader,
) -> list[ValidationError]:
    """Validate a PhaseRecord against cognition bundle contracts.

    Returns a list of ValidationError. Empty list = valid.

    Args:
        record: PhaseRecord to validate.
        cognition_loader: CognitionLoader with loaded bundle.

    Returns:
        List of ValidationError instances. Empty = all valid.
    """
    errors: list[ValidationError] = []

    # 1. Phase validity
    phase_def = cognition_loader.get_phase_definition(record.phase)
    if not phase_def:
        errors.append(ValidationError(
            code="unknown_phase",
            message=f"Phase '{record.phase}' not found in cognition bundle",
            details={"phase": record.phase, "known_phases": cognition_loader.get_all_phases()},
        ))
        return errors  # cannot validate further without valid phase

    # 2. Subtype validity
    subtype_def = cognition_loader.get_subtype_definition(record.subtype)
    if not subtype_def:
        errors.append(ValidationError(
            code="unknown_subtype",
            message=f"Subtype '{record.subtype}' not found in cognition bundle",
            details={
                "subtype": record.subtype,
                "known_subtypes": list(cognition_loader.subtypes.keys()),
            },
        ))
        return errors  # cannot validate further without valid subtype

    # 3. Subtype belongs to declared phase
    if subtype_def["phase"] != record.phase:
        errors.append(ValidationError(
            code="subtype_phase_mismatch",
            message=(
                f"Subtype '{record.subtype}' belongs to phase "
                f"'{subtype_def['phase']}', not '{record.phase}'"
            ),
            details={
                "subtype": record.subtype,
                "expected_phase": subtype_def["phase"],
                "actual_phase": record.phase,
            },
        ))

    # 4. Required principals
    required = cognition_loader.get_required_principals(record.subtype)
    declared = set(record.principals)
    missing = [p for p in required if p not in declared]
    if missing:
        errors.append(ValidationError(
            code="missing_principals",
            message=f"Missing required principals: {missing}",
            details={
                "missing": missing,
                "required": required,
                "declared": record.principals,
            },
        ))

    # 5. Forbidden principals
    forbidden = cognition_loader.get_forbidden_principals(record.subtype)
    present_forbidden = [p for p in forbidden if p in declared]
    if present_forbidden:
        errors.append(ValidationError(
            code="forbidden_principals",
            message=f"Forbidden principals present: {present_forbidden}",
            details={
                "forbidden_present": present_forbidden,
                "forbidden_list": forbidden,
            },
        ))

    # 6. Evidence discipline (analysis phase must have evidence)
    if record.phase == "ANALYZE" and not record.evidence_refs:
        errors.append(ValidationError(
            code="missing_evidence",
            message="ANALYZE phase requires evidence_refs",
            details={"phase": record.phase},
        ))

    # 7. Required fields from phase definition
    for field_name in phase_def.get("required_fields", []):
        value = getattr(record, field_name, None)
        if not value:
            errors.append(ValidationError(
                code="missing_required_field",
                message=f"Phase '{record.phase}' requires field '{field_name}'",
                details={
                    "field": field_name,
                    "phase": record.phase,
                },
            ))

    return errors


# ── GateRejection Integration ───────────────────────────────────────────────

def build_cognition_gate_rejection(
    errors: list[ValidationError],
    record: PhaseRecord,
    cognition_loader: CognitionLoader,
) -> GateRejection:
    """Convert validation errors to a GateRejection for structured feedback.

    Args:
        errors: Validation errors from validate_phase_record().
        record: The PhaseRecord that was validated.
        cognition_loader: CognitionLoader for contract lookup.

    Returns:
        GateRejection with self-describing failures.
    """
    # Build contract view
    required_principals = cognition_loader.get_required_principals(record.subtype)
    phase_def = cognition_loader.get_phase_definition(record.phase)
    required_fields = phase_def.get("required_fields", []) if phase_def else []

    contract = ContractView(
        required_fields=required_fields + [f"principal:{p}" for p in required_principals],
        field_specs={
            f"principal:{p}": FieldSpec(
                description=f"Required principal declaration: {p}",
                required=True,
            )
            for p in required_principals
        },
    )

    # Convert validation errors to field failures
    failures: list[FieldFailure] = []
    for err in errors:
        if err.code == "missing_principals":
            for p in err.details.get("missing", []):
                failures.append(FieldFailure(
                    field=f"principal:{p}",
                    reason="missing",
                    hint=f"Declare principal '{p}' in your PRINCIPALS list.",
                    expected=p,
                    actual=None,
                ))
        elif err.code == "forbidden_principals":
            for p in err.details.get("forbidden_present", []):
                failures.append(FieldFailure(
                    field=f"principal:{p}",
                    reason="principal_violation",
                    hint=f"Remove principal '{p}' — forbidden for subtype '{record.subtype}'.",
                    expected="not declared",
                    actual=p,
                ))
        elif err.code == "missing_evidence":
            failures.append(FieldFailure(
                field="evidence_refs",
                reason="missing",
                hint="Include evidence_refs (file:line references) for your analysis.",
                expected="non-empty evidence_refs",
                actual="[]",
            ))
        elif err.code == "missing_required_field":
            field_name = err.details.get("field", "unknown")
            failures.append(FieldFailure(
                field=field_name,
                reason="missing",
                hint=f"Provide '{field_name}' — required for phase '{record.phase}'.",
                expected=f"non-empty {field_name}",
                actual=None,
            ))
        else:
            # Generic failure for unknown_phase, unknown_subtype, subtype_phase_mismatch
            failures.append(FieldFailure(
                field=err.code,
                reason=err.code,
                hint=err.message,
                expected="valid",
                actual=str(err.details),
            ))

    return build_gate_rejection(
        gate_name="cognition_validator",
        contract=contract,
        extracted={
            "phase": record.phase,
            "subtype": record.subtype,
            "principals": record.principals,
            "evidence_refs_count": len(record.evidence_refs),
        },
        failures=failures,
    )


def build_validation_feedback(
    errors: list[ValidationError],
    record: PhaseRecord,
    cognition_loader: CognitionLoader,
) -> str:
    """Build agent-readable repair prompt from validation errors.

    Args:
        errors: Validation errors.
        record: PhaseRecord that was validated.
        cognition_loader: CognitionLoader for contract lookup.

    Returns:
        String repair prompt for the agent.
    """
    if not errors:
        return ""
    rejection = build_cognition_gate_rejection(errors, record, cognition_loader)
    return build_repair_from_rejection(rejection)
