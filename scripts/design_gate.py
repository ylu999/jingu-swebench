"""
design_gate.py — Design quality enforcement for ANALYZE->EXECUTE transition.

Evaluates whether the agent's design meets minimum quality thresholds
before allowing advance to EXECUTE. Targets constraint_encoding_failure.

Contract source of truth: cognition_contracts/design_solution_shape.py
Gate rules and field specs are defined there; this file implements scoring.

Events are system-generated facts, never LLM self-descriptions.
Every field must be derived from system state, not from LLM output.
"""

from dataclasses import dataclass, field
from phase_record import PhaseRecord
from gate_rejection import GateRejection

# ── Contract-derived references ──────────────────────────────────────────────
# Gate rules and repair hints originate from the canonical contract.
# design_gate scoring functions implement the checks; the contract defines
# which fields they target and what repair hint to emit on failure.
try:
    from cognition_contracts.design_solution_shape import (
        GATE_RULES as _CONTRACT_GATE_RULES,
        GATE_RULE_MAP as _CONTRACT_RULE_MAP,
        GATE_THRESHOLD as _CONTRACT_THRESHOLD,
    )
except ImportError:
    # Fallback: contract module not yet on PYTHONPATH — degrade gracefully.
    _CONTRACT_GATE_RULES = []
    _CONTRACT_RULE_MAP = {}
    _CONTRACT_THRESHOLD = 0.5


@dataclass
class DesignVerdict:
    """Result of design gate evaluation."""
    passed: bool
    failed_rules: list
    reasons: list
    scores: dict
    rejection: GateRejection | None = None


# ── Rule 1: Invariant Preservation (structural) ─────────────────────────────


def _check_invariant_preservation(pr: PhaseRecord, analysis_records: list[PhaseRecord] | None = None) -> float:
    """
    Check that design references invariants to preserve.

    Structural check: looks for a non-empty `invariants` list with
    substantive entries (len > 5 after strip).

    Score:
      0.0 = no invariants field or empty
      1.0 = at least one substantive invariant present
    """
    invariants = getattr(pr, 'invariants', None) or []
    substantive = [inv for inv in invariants if isinstance(inv, str) and len(inv.strip()) > 5]
    if len(substantive) >= 1:
        return 1.0
    return 0.0


# ── Rule 2: Design Comparison (structural) ───────────────────────────────────


def _check_design_comparison(pr: PhaseRecord) -> float:
    """
    Check that design compares at least 2 approaches.

    Structural check: looks for a `design_comparison` dict with an
    `options` list containing substantive entries (name + pros or cons).

    Score:
      0.0 = no design_comparison or no substantive options
      0.5 = 1 substantive option
      1.0 = 2+ substantive options
    """
    comparison = getattr(pr, 'design_comparison', None) or {}
    if not isinstance(comparison, dict):
        return 0.0
    options = comparison.get('options') or []
    substantive = [
        a for a in options
        if isinstance(a, dict) and a.get('name') and (a.get('pros') or a.get('cons'))
    ]
    if len(substantive) >= 2:
        return 1.0
    elif len(substantive) >= 1:
        return 0.5
    return 0.0


# ── Rule 3: Constraint Encoding (structural) ─────────────────────────────────


def _check_constraint_encoding(pr: PhaseRecord) -> float:
    """
    Check that if design uses allowlist approach, completeness is justified.

    Structural check: looks at `scope_boundary` for allowlist indicators,
    then checks that `invariants` has at least one substantive entry.

    Score:
      1.0 = no scope_boundary (rule N/A)
      1.0 = scope_boundary without allowlist indicator (rule N/A)
      1.0 = allowlist with substantive invariant justification
      0.0 = allowlist without substantive invariant justification
    """
    scope = getattr(pr, 'scope_boundary', '') or ''
    if not scope.strip():
        return 1.0  # no scope = rule N/A
    allowlist_terms = ('allowlist', 'whitelist', 'permitted characters', 'valid characters')
    is_allowlist = any(term in scope.lower() for term in allowlist_terms)
    if not is_allowlist:
        return 1.0  # no allowlist = rule N/A
    invariants = getattr(pr, 'invariants', None) or []
    if len(invariants) >= 1 and any(len(inv.strip()) > 10 for inv in invariants if isinstance(inv, str)):
        return 1.0
    return 0.0


# ── Main evaluation function (soft quality signal — no rejection) ─────────


def evaluate_design(
    pr: PhaseRecord,
    analysis_records: list[PhaseRecord] | None = None,
) -> DesignVerdict:
    """
    Evaluate design quality as soft telemetry signal.

    Scores are computed for observability but never cause rejection.
    Gate mode is soft_quality_signal — all rules emit scores only.

    Args:
        pr: PhaseRecord containing the design/plan.
        analysis_records: Previous analysis PhaseRecords (for invariant cross-check).
    """
    scores: dict = {}

    # Rule 1: Invariant preservation (score only)
    scores["invariant_preservation"] = _check_invariant_preservation(pr, analysis_records)

    # Rule 2: Design comparison (score only)
    scores["design_comparison"] = _check_design_comparison(pr)

    # Rule 3: Constraint encoding (score only)
    scores["constraint_encoding"] = _check_constraint_encoding(pr)

    # Mark as soft quality signal — no hard rejection
    scores["gate_mode"] = "soft_quality_signal"

    return DesignVerdict(
        passed=True,
        failed_rules=[],
        reasons=[],
        scores=scores,
        rejection=None,
    )
