"""
decide_gate.py — Phase boundary enforcement for DECIDE phase.

Evaluates whether the agent's decision meets minimum quality thresholds
before allowing advance to DESIGN or EXECUTE.

Three rules:
1. Option comparison: at least 2 substantive options with pros/cons
2. Selection justified: chosen option named + rationale provided
3. Chosen matches option: chosen value must match one of the listed option names

Events are system-generated facts, never LLM self-descriptions.
Every field must be derived from system state, not from LLM output.
"""

from dataclasses import dataclass, field
from phase_record import PhaseRecord
from cognition_contracts import decision_fix_direction as _dfd


@dataclass
class DecideVerdict:
    """Result of decide gate evaluation."""
    passed: bool
    failed_rules: list          # e.g. ["option_comparison", "chosen_matches_option"]
    reasons: list               # human-readable rejection reasons
    scores: dict                # per-rule scores for telemetry
    rejection: object = None    # placeholder for future SDG rejection


# -- Rule 1: Option Comparison ------------------------------------------------

def _check_option_comparison(pr: PhaseRecord) -> float:
    """Check that at least 2 substantive options with pros/cons are listed.

    Structural-only: reads pr.options (array of dicts from schema).

    Score:
      0.0 = no options or no substantive options
      0.5 = exactly 1 substantive option
      1.0 = 2+ substantive options with pros/cons
    """
    options = getattr(pr, 'options', None) or []
    substantive = [
        o for o in options
        if isinstance(o, dict)
        and (o.get('name') or '').strip()
        and ((o.get('pros') or '').strip() or (o.get('cons') or '').strip())
    ]
    if len(substantive) >= 2:
        return 1.0
    elif len(substantive) >= 1:
        return 0.5
    return 0.0


# -- Rule 2: Selection Justified ----------------------------------------------

def _check_selection_justified(pr: PhaseRecord) -> float:
    """Check that a chosen option is named and a rationale is provided.

    Structural-only: reads pr.chosen and pr.rationale.

    Score:
      0.0 = no chosen value
      0.5 = chosen present but rationale missing or too short
      1.0 = chosen present + rationale > 10 chars
    """
    chosen = (getattr(pr, 'chosen', '') or '').strip()
    rationale = (getattr(pr, 'rationale', '') or '').strip()
    if not chosen:
        return 0.0
    if len(rationale) > 10:
        return 1.0
    return 0.5


# -- Rule 3: Chosen Matches Option -------------------------------------------

def _check_chosen_matches_option(pr: PhaseRecord) -> float:
    """Check that the chosen value matches one of the listed option names.

    Structural-only: compares pr.chosen against pr.options[].name.

    Score:
      0.0 = chosen is empty or does not match any option name
      1.0 = chosen matches an option name (case-insensitive)
    """
    chosen = (getattr(pr, 'chosen', '') or '').strip().lower()
    if not chosen:
        return 0.0
    options = getattr(pr, 'options', None) or []
    names = [
        (o.get('name') or '').strip().lower()
        for o in options
        if isinstance(o, dict)
    ]
    if chosen in names:
        return 1.0
    return 0.0


# -- Main evaluation function ------------------------------------------------

# Threshold and rule-to-field mapping derived from contract (single source of truth)
_THRESHOLD = _dfd.GATE_THRESHOLD

_RULE_CHECKS = {
    "option_comparison": _check_option_comparison,
    "selection_justified": _check_selection_justified,
    "chosen_matches_option": _check_chosen_matches_option,
}

# Rule name -> (field, hint) for rejection messages
_RULE_TO_FIELD: dict[str, tuple[str, str]] = {
    rule.name: (rule.field, rule.repair_hint) for rule in _dfd.GATE_RULES
}


def evaluate_decide(pr: PhaseRecord, subtype: str | None = None) -> DecideVerdict:
    """
    Evaluate decide phase quality. Returns verdict with pass/fail + reasons.

    Threshold is 0.5 (from contract). All three rules are hard gates.

    Args:
        pr: PhaseRecord to evaluate.
        subtype: Optional subtype (unused, kept for interface consistency).
    """
    failed = []
    reasons = []
    scores = {}

    for rule_name, check_fn in _RULE_CHECKS.items():
        score = check_fn(pr)
        scores[rule_name] = score
        if score < _THRESHOLD:
            failed.append(rule_name)
            _field, hint = _RULE_TO_FIELD.get(rule_name, (rule_name, f"Fix {rule_name}"))
            reasons.append(hint)

    return DecideVerdict(
        passed=len(failed) == 0,
        failed_rules=failed,
        reasons=reasons,
        scores=scores,
    )
