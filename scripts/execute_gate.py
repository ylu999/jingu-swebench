"""
execute_gate.py — Phase boundary enforcement for EXECUTE phase.

Evaluates whether the agent's execution step meets minimum quality thresholds.
Three structural rules derived from cognition_contracts/execution_code_patch.py:

1. patch_described: patch_description must be non-trivial (>10 chars)
2. causal_grounding: patch description should reference analysis evidence
3. scope_bounded: files_modified should list the changed files

Events are system-generated facts, never LLM self-descriptions.
Every field must be derived from system state, not from LLM output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from phase_record import PhaseRecord
from gate_rejection import (
    GateRejection, ContractView, FieldSpec, FieldFailure,
    build_gate_rejection, SDG_ENABLED,
)
from cognition_contracts import execution_code_patch as _ecp


@dataclass
class ExecuteVerdict:
    """Result of execute gate evaluation."""
    passed: bool = False
    failed_rules: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    rejection: GateRejection | None = None


# -- Rule 1: Patch Described --------------------------------------------------

def _check_patch_described(pr: PhaseRecord) -> float:
    """Check that patch_description is present and substantive (>10 chars).

    Score:
      0.0 = missing or empty
      1.0 = present and >10 chars
    """
    desc = getattr(pr, 'patch_description', '') or ''
    if len(desc.strip()) > 10:
        return 1.0
    return 0.0


# -- Rule 2: Causal Grounding ------------------------------------------------

def _check_causal_grounding(pr: PhaseRecord, analysis_evidence_refs: list[str] | None = None) -> float:
    """Check that the patch references the root cause from ANALYZE.

    Uses analysis_evidence_refs (from prior ANALYZE phase) to verify the patch
    touches files identified during analysis.

    Score:
      0.0 = no patch description
      0.5 = patch description present but no evidence refs to cross-check,
             or evidence refs provided but no file overlap found
      1.0 = evidence refs provided and at least one file overlaps
    """
    desc = getattr(pr, 'patch_description', '') or ''
    if not desc.strip():
        return 0.0

    if not analysis_evidence_refs:
        return 0.5  # cannot cross-check without evidence refs

    files_modified = getattr(pr, 'files_modified', None) or []
    for ref in analysis_evidence_refs:
        file_part = ref.split(':')[0] if ':' in ref else ref
        if any(file_part in fm for fm in files_modified):
            return 1.0
        if file_part in desc:
            return 1.0

    return 0.5


# -- Rule 3: Scope Bounded ---------------------------------------------------

def _check_scope_bounded(pr: PhaseRecord) -> float:
    """Check that files_modified lists at least one file.

    Score:
      0.0 = no files listed
      1.0 = at least one file listed
    """
    files = getattr(pr, 'files_modified', None) or []
    if len(files) >= 1:
        return 1.0
    return 0.0


# -- Rule dispatch ------------------------------------------------------------

_RULE_CHECKS = {
    "patch_described": _check_patch_described,
    "causal_grounding": _check_causal_grounding,
    "scope_bounded": _check_scope_bounded,
}


# -- EXECUTE contract (SDG) ---------------------------------------------------

_EXECUTE_CONTRACT = ContractView(
    required_fields=list(_ecp.GATE_REQUIRED_FIELDS),
    field_specs={
        fs.name: FieldSpec(
            description=fs.description,
            required=fs.required,
            min_length=fs.min_length,
        )
        for fs in _ecp.FIELD_SPECS
    },
)

# Rule name -> (field, hint) mapping for SDG FieldFailure construction.
_RULE_TO_FIELD: dict[str, tuple[str, str]] = {
    rule.name: (rule.field, rule.repair_hint) for rule in _ecp.GATE_RULES
}


# -- Main evaluation function ------------------------------------------------

_THRESHOLD = _ecp.GATE_THRESHOLD  # From contract (single source of truth)


def evaluate_execute(
    pr: PhaseRecord,
    analysis_evidence_refs: list[str] | None = None,
    subtype: str | None = None,
) -> ExecuteVerdict:
    """
    Evaluate execute phase quality. Returns verdict with pass/fail + reasons.

    Threshold is 0.5 (soft gate). We reject clearly empty executions,
    not borderline ones.

    Args:
        pr: PhaseRecord to evaluate.
        analysis_evidence_refs: evidence_refs from prior ANALYZE phase (for causal grounding).
        subtype: subtype string (reserved for future use).
    """
    verdict = ExecuteVerdict()

    for rule in _ecp.GATE_RULES:
        check_fn = _RULE_CHECKS.get(rule.name)
        if not check_fn:
            continue

        if rule.name == "causal_grounding":
            score = check_fn(pr, analysis_evidence_refs)
        else:
            score = check_fn(pr)

        verdict.scores[rule.name] = score
        if score < _THRESHOLD:
            verdict.failed_rules.append(rule.name)
            verdict.reasons.append(rule.repair_hint)

    verdict.passed = len(verdict.failed_rules) == 0

    # Build structured SDG rejection on failure
    if not verdict.passed and SDG_ENABLED:
        field_failures = []
        for rule_name in verdict.failed_rules:
            field_name, hint = _RULE_TO_FIELD.get(
                rule_name, (rule_name, f"Fix {rule_name}")
            )
            score = verdict.scores.get(rule_name, 0.0)
            reason = "missing" if score == 0.0 else "semantic_fail"

            field_spec = _EXECUTE_CONTRACT.field_specs.get(field_name)
            expected = field_spec.description if field_spec else f"{field_name} required"

            field_failures.append(FieldFailure(
                field=field_name,
                reason=reason,
                hint=hint,
                expected=expected,
                actual=None,
            ))

        verdict.rejection = build_gate_rejection(
            gate_name="execute_gate",
            contract=_EXECUTE_CONTRACT,
            extracted={
                "patch_description": (getattr(pr, 'patch_description', '') or '')[:100],
                "files_modified": getattr(pr, 'files_modified', None) or [],
            },
            failures=field_failures,
        )

    return verdict
