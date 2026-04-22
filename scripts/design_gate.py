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
from typing import TYPE_CHECKING
from phase_record import PhaseRecord
from gate_rejection import GateRejection, ContractView

if TYPE_CHECKING:
    from bundle_compiler import CompiledBundle

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


# ── Rule 4: Target Files Bounded (hard) ──────────────────────────────────


def _check_target_files_bounded(pr: PhaseRecord) -> float:
    """
    Check that files_to_modify has 1-3 specific files.

    Score:
      0.0 = no files or more than 3
      1.0 = 1-3 files
    """
    files = getattr(pr, 'files_to_modify', None) or []
    if not isinstance(files, list):
        return 0.0
    if 1 <= len(files) <= 3:
        return 1.0
    return 0.0


# ── Rule 5: Test-to-Code Link (hard) ────────────────────────────────────


def _check_test_code_linked(pr: PhaseRecord) -> float:
    """
    Check that test_to_code_link is substantive (>= 10 chars).

    Score:
      0.0 = missing or too short
      1.0 = substantive link present
    """
    link = getattr(pr, 'test_to_code_link', '') or ''
    if isinstance(link, str) and len(link.strip()) >= 10:
        return 1.0
    return 0.0


# ── Rule 6: Rejected Alternative (hard) ──────────────────────────────────


def _check_alternative_considered(pr: PhaseRecord) -> float:
    """
    Check that rejected_alternative is substantive (>= 10 chars).

    Score:
      0.0 = missing or too short
      1.0 = substantive alternative present
    """
    alt = getattr(pr, 'rejected_alternative', '') or ''
    if isinstance(alt, str) and len(alt.strip()) >= 10:
        return 1.0
    return 0.0


# ── Rule 7: Change Mechanism (hard) ────────────────────────────────────


def _check_change_mechanism(pr: PhaseRecord) -> float:
    """
    Check that change_mechanism is substantive (>= 10 chars).

    Score:
      0.0 = missing or too short
      1.0 = substantive mechanism present
    """
    mech = getattr(pr, 'change_mechanism', '') or ''
    if isinstance(mech, str) and len(mech.strip()) >= 10:
        return 1.0
    return 0.0


# ── Main evaluation function ─────────────────────────────────────────────


def evaluate_design(
    phase_record: PhaseRecord,
    analysis_records: list[PhaseRecord] | None = None,
    *,
    compiled_bundle: "CompiledBundle | None" = None,
) -> DesignVerdict:
    """
    Evaluate design quality with hard gate checks (v0.2).

    Three hard checks that can reject:
    - target_files_bounded: files_to_modify must have 1-3 files
    - test_code_linked: test_to_code_link must be substantive
    - alternative_considered: rejected_alternative must be substantive

    Three soft checks (score-only, no rejection):
    - invariant_preservation, design_comparison, constraint_encoding

    Args:
        phase_record: PhaseRecord containing the design/plan.
        analysis_records: Previous analysis PhaseRecords (for invariant cross-check).
    """
    pr = phase_record

    scores: dict = {}
    failed_rules: list = []
    reasons: list = []

    # C-04: Resolve contract from CompiledBundle when available.
    _contract_source = "cognition_contracts"
    if compiled_bundle is not None:
        try:
            _cv = compiled_bundle.validators.get("DESIGN")
            if _cv is not None:
                _contract = ContractView.from_compiled_validator(_cv)
                _contract_source = "compiled_bundle"
            else:
                _contract = None
        except Exception:
            _contract = None
    else:
        _contract = None
    # ── Hard checks (can cause rejection) ──

    # Rule 4: Target files bounded (1-3 files)
    scores["target_files_bounded"] = _check_target_files_bounded(pr)
    if scores["target_files_bounded"] < _CONTRACT_THRESHOLD:
        _rule = _CONTRACT_RULE_MAP.get("target_files_bounded")
        _hint = _rule.repair_hint if _rule else "Limit files_to_modify to 1-3 specific files."
        failed_rules.append("target_files_bounded")
        reasons.append(_hint)

    # Rule 5: Test-to-code link
    scores["test_code_linked"] = _check_test_code_linked(pr)
    if scores["test_code_linked"] < _CONTRACT_THRESHOLD:
        _rule = _CONTRACT_RULE_MAP.get("test_code_linked")
        _hint = _rule.repair_hint if _rule else "Provide test_to_code_link mapping failing test to code."
        failed_rules.append("test_code_linked")
        reasons.append(_hint)

    # Rule 6: Rejected alternative
    scores["alternative_considered"] = _check_alternative_considered(pr)
    if scores["alternative_considered"] < _CONTRACT_THRESHOLD:
        _rule = _CONTRACT_RULE_MAP.get("alternative_considered")
        _hint = _rule.repair_hint if _rule else "Provide rejected_alternative with at least one considered approach."
        failed_rules.append("alternative_considered")
        reasons.append(_hint)

    # Rule 7: Change mechanism
    scores["change_mechanism_present"] = _check_change_mechanism(pr)
    if scores["change_mechanism_present"] < _CONTRACT_THRESHOLD:
        _rule = _CONTRACT_RULE_MAP.get("change_mechanism_present")
        _hint = _rule.repair_hint if _rule else "Provide change_mechanism: explain what behavior changes and why failing tests should pass."
        failed_rules.append("change_mechanism_present")
        reasons.append(_hint)

    # ── Soft checks (score-only, no rejection) ──

    # Rule 1: Invariant preservation (score only)
    scores["invariant_preservation"] = _check_invariant_preservation(pr, analysis_records)

    # Rule 2: Design comparison (score only)
    scores["design_comparison"] = _check_design_comparison(pr)

    # Rule 3: Constraint encoding (score only)
    scores["constraint_encoding"] = _check_constraint_encoding(pr)

    # Determine pass/fail
    passed = len(failed_rules) == 0

    return DesignVerdict(
        passed=passed,
        failed_rules=failed_rules,
        reasons=reasons,
        scores=scores,
        rejection=None,
    )
