"""
cognition_check.py — Python port of jingu-cognition contradiction rules.

Checks whether an agent's fix declaration is consistent with the actual patch signals.
This is deterministic (no LLM). Port of jingu-cognition/src/validate-declaration-vs-patch.ts

Contradiction rules:
  root_cause_fix + is_normalization  → contradiction (normalization ≠ root cause)
  root_cause_fix + is_comment_only   → contradiction (comment change ≠ root cause)
  workaround_fix + fix_cause_not_symptom principal → contradiction (self-contradiction)
"""

from typing import TypedDict


class CognitionViolation(TypedDict):
    kind: str
    reason: str


class CognitionResult(TypedDict):
    valid: bool
    violations: list[CognitionViolation]


# Contradiction rules: (fix_type, signal_or_principal) → violation reason
_SIGNAL_CONTRADICTIONS: list[tuple[str, str, str]] = [
    (
        "root_cause_fix",
        "is_normalization",
        "root_cause_fix declared but patch only reorganizes imports/whitespace",
    ),
    (
        "root_cause_fix",
        "is_comment_only",
        "root_cause_fix declared but patch only changes comments",
    ),
]

_PRINCIPAL_CONTRADICTIONS: list[tuple[str, str, str]] = [
    (
        "workaround_fix",
        "fix_cause_not_symptom",
        "workaround_fix declared but fix_cause_not_symptom principal claimed — self-contradiction",
    ),
]


def check_cognition(declaration: dict, patch_signals: list[str]) -> CognitionResult:
    """
    Check declaration consistency with patch signals.

    Args:
        declaration: {"type": str, "principals": [str]} — from declaration_extractor
        patch_signals: list of signal strings — from patch_signals.extract_patch_signals

    Returns:
        {"valid": bool, "violations": [...]}

    Empty declaration → always valid (opt-in gate).
    Empty patch signals → only principal contradictions checked.
    """
    if not declaration or not declaration.get("type"):
        return {"valid": True, "violations": []}

    fix_type = declaration.get("type", "").lower()
    principals = [p.lower() for p in declaration.get("principals", [])]
    violations: list[CognitionViolation] = []

    # Check signal contradictions
    for rule_type, signal, reason in _SIGNAL_CONTRADICTIONS:
        if fix_type == rule_type and signal in patch_signals:
            violations.append({"kind": "signal_contradiction", "reason": reason})

    # Check principal contradictions
    for rule_type, principal, reason in _PRINCIPAL_CONTRADICTIONS:
        if fix_type == rule_type and principal in principals:
            violations.append({"kind": "principal_contradiction", "reason": reason})

    return {"valid": len(violations) == 0, "violations": violations}


def format_cognition_feedback(result: CognitionResult) -> str:
    """
    Format cognition violations as a retry hint string.
    Returns "" if no violations.
    """
    if result["valid"] or not result["violations"]:
        return ""
    lines = ["[cognition] Declaration contradicts patch:"]
    for v in result["violations"]:
        lines.append(f"  - {v['reason']}")
    return "\n".join(lines)


if __name__ == "__main__":
    # Smoke tests
    from patch_signals import extract_patch_signals

    # root_cause_fix + is_normalization → reject
    decl = {"type": "root_cause_fix", "principals": ["fix_cause_not_symptom"]}
    signals = ["is_normalization", "is_single_line_fix"]
    result = check_cognition(decl, signals)
    assert not result["valid"], result
    assert len(result["violations"]) == 1
    print(f"PASS root_cause_fix+normalization rejected: {result['violations'][0]['reason']}")

    # root_cause_fix + is_single_line_fix (real fix) → pass
    result2 = check_cognition({"type": "root_cause_fix", "principals": []}, ["is_single_line_fix"])
    assert result2["valid"], result2
    print("PASS root_cause_fix+single_line_fix admitted")

    # Empty declaration → pass
    assert check_cognition({}, [])["valid"]
    assert check_cognition({"type": "", "principals": []}, [])["valid"]
    print("PASS empty declaration always valid")

    # workaround_fix + fix_cause_not_symptom → reject
    result3 = check_cognition(
        {"type": "workaround_fix", "principals": ["fix_cause_not_symptom"]}, []
    )
    assert not result3["valid"], result3
    print(f"PASS workaround+fix_cause contradiction: {result3['violations'][0]['reason']}")

    # Format feedback
    feedback = format_cognition_feedback(result)
    assert "[cognition]" in feedback
    print(f"PASS format_cognition_feedback: {feedback}")
