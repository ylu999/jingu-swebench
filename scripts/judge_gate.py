"""
judge_gate.py — Phase boundary enforcement for JUDGE phase.

Evaluates whether the agent's verification meets minimum quality thresholds
before accepting or routing back for retry.

Four rules:
1. Test results present: test_results must have a passed boolean
2. Criteria verified: success_criteria_met must have at least 1 substantive entry
3. Risks acknowledged: residual_risks field must be present (even if empty list)
4. Verdict consistent: verdict must match test results (no pass with failed tests)

Events are system-generated facts, never LLM self-descriptions.
Every field must be derived from system state, not from LLM output.
"""

from dataclasses import dataclass, field
from phase_record import PhaseRecord
from cognition_contracts import judge_verification as _jv


@dataclass
class JudgeVerdict:
    """Result of judge gate evaluation."""
    passed: bool = False
    failed_rules: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    rejection: dict | None = None


# -- Rule 1: Test Results Present ----------------------------------------------

def _check_test_results_present(pr: PhaseRecord) -> float:
    """Check that test_results contains a passed boolean field.

    Score:
      0.0 = test_results missing, empty, or no passed field
      1.0 = test_results is a dict with passed boolean
    """
    results = getattr(pr, 'test_results', None) or {}
    if isinstance(results, dict) and 'passed' in results:
        return 1.0
    return 0.0


# -- Rule 2: Criteria Verified ------------------------------------------------

def _check_criteria_verified(pr: PhaseRecord) -> float:
    """Check that success_criteria_met has at least 1 substantive entry.

    Substantive = dict with non-empty 'criterion' and 'met' key present.

    Score:
      0.0 = no criteria or all empty
      0.5 = criteria present but not substantive (missing fields)
      1.0 = at least 1 substantive criterion
    """
    criteria = getattr(pr, 'success_criteria_met', None) or []
    if not criteria:
        return 0.0
    substantive = [
        c for c in criteria
        if isinstance(c, dict)
        and c.get('criterion')
        and 'met' in c
    ]
    if len(substantive) >= 1:
        return 1.0
    # Has entries but none substantive
    if len(criteria) > 0:
        return 0.5
    return 0.0


# -- Rule 3: Risks Acknowledged -----------------------------------------------

def _check_risks_acknowledged(pr: PhaseRecord) -> float:
    """Check that residual_risks field is present (even if empty list).

    Score:
      0.0 = residual_risks is None (not set at all)
      1.0 = residual_risks is present (list, possibly empty = no risks)
    """
    risks = getattr(pr, 'residual_risks', None)
    if risks is not None:
        return 1.0
    return 0.0


# -- Rule 4: Verdict Consistent -----------------------------------------------

def _check_verdict_consistent(pr: PhaseRecord, verdict: str | None = None) -> float:
    """Check that verdict is consistent with test results.

    A verdict of 'pass' when tests show passed=False is inconsistent.

    Score:
      0.0 = verdict says pass but tests failed
      0.5 = test_results missing or no passed field (cannot verify)
      1.0 = verdict consistent with test results (or no verdict given)
    """
    results = getattr(pr, 'test_results', None) or {}
    if not isinstance(results, dict) or 'passed' not in results:
        return 0.5
    test_passed = results.get('passed', None)
    if verdict and verdict.lower() == 'pass' and test_passed is False:
        return 0.0
    return 1.0


# -- Rule dispatch map --------------------------------------------------------

_RULE_CHECKS = {
    "test_results_present": _check_test_results_present,
    "criteria_verified": _check_criteria_verified,
    "risks_acknowledged": _check_risks_acknowledged,
    "verdict_consistent": _check_verdict_consistent,
}

_THRESHOLD = _jv.GATE_THRESHOLD  # From contract (single source of truth)


# -- Main evaluation function -------------------------------------------------

def evaluate_judge(
    pr: PhaseRecord,
    verdict: str | None = None,
    subtype: str | None = None,
) -> JudgeVerdict:
    """
    Evaluate judge phase quality. Returns verdict with pass/fail + reasons.

    Args:
        pr: PhaseRecord to evaluate.
        verdict: Optional verdict string (e.g. 'pass', 'fail') for consistency check.
        subtype: Optional subtype override (reserved for future use).
    """
    result = JudgeVerdict()

    for rule in _jv.GATE_RULES:
        check_fn = _RULE_CHECKS.get(rule.name)
        if not check_fn:
            continue

        # verdict_consistent needs the verdict parameter
        if rule.name == "verdict_consistent":
            score = check_fn(pr, verdict)
        else:
            score = check_fn(pr)

        result.scores[rule.name] = score
        if score < _THRESHOLD:
            result.failed_rules.append(rule.name)
            result.reasons.append(rule.repair_hint)

    result.passed = len(result.failed_rules) == 0
    return result
