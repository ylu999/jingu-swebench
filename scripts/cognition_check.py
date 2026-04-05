"""
cognition_check.py — Python port of jingu-cognition contradiction rules.

Checks whether an agent's fix declaration is consistent with the actual patch signals.
This is deterministic (no LLM). Port of jingu-cognition/src/validate-declaration-vs-patch.ts

CDP v1 contradiction rules (phase × signal):
  diagnosis  + is_normalization  → diagnosis claims root cause but patch is whitespace/import reorg
  execution  + is_comment_only   → execution claims code change but patch only touches comments
  execution  + causality         → causality is a diagnosis concern; execution should not claim it
"""

from typing import TypedDict


class CognitionViolation(TypedDict):
    kind: str
    reason: str


class CognitionResult(TypedDict):
    valid: bool
    violations: list[CognitionViolation]


# Contradiction rules: (cdp_type, signal_or_principal) → violation reason
# CDP v1 types: understanding / observation / analysis / diagnosis /
#               decision / design / planning / execution / validation
_SIGNAL_CONTRADICTIONS: list[tuple[str, str, str]] = [
    (
        "diagnosis",
        "is_normalization",
        "diagnosis declared but patch only reorganizes imports/whitespace — no root cause found",
    ),
    (
        "execution",
        "is_comment_only",
        "execution declared but patch only changes comments — no code mutation produced",
    ),
]

_PRINCIPAL_CONTRADICTIONS: list[tuple[str, str, str]] = [
    (
        "execution",
        "causality",
        "execution declared causality principal — causality is a diagnosis concern, not execution",
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


def check_cognition_at_judge(declaration: dict, patch_signals: list[str]) -> tuple[bool, str]:
    """
    JUDGE phase entry gate wrapper.

    Called before controlled_verify when cp_state.phase == 'JUDGE'.
    Returns (pass: bool, feedback_if_fail: str).

    Pass  → caller continues to controlled_verify.
    Fail  → caller injects feedback as pending_redirect_hint, skips controlled_verify.

    Empty declaration → always pass (opt-in gate: no FIX_TYPE means no check).
    """
    result = check_cognition(declaration, patch_signals)
    if result["valid"]:
        return True, ""
    return False, format_cognition_feedback(result)


if __name__ == "__main__":
    # Smoke tests — CDP v1 vocabulary

    # diagnosis + is_normalization → reject (no root cause in patch)
    decl = {"type": "diagnosis", "principals": ["causality"]}
    signals = ["is_normalization", "is_single_line_fix"]
    result = check_cognition(decl, signals)
    assert not result["valid"], result
    assert len(result["violations"]) == 1
    print(f"PASS diagnosis+normalization rejected: {result['violations'][0]['reason']}")

    # execution + is_single_line_fix (real code fix) → pass
    result2 = check_cognition({"type": "execution", "principals": ["minimal_change"]}, ["is_single_line_fix"])
    assert result2["valid"], result2
    print("PASS execution+single_line_fix admitted")

    # Empty declaration → pass
    assert check_cognition({}, [])["valid"]
    assert check_cognition({"type": "", "principals": []}, [])["valid"]
    print("PASS empty declaration always valid")

    # execution + causality → reject (causality is diagnosis concern)
    result3 = check_cognition(
        {"type": "execution", "principals": ["causality"]}, []
    )
    assert not result3["valid"], result3
    print(f"PASS execution+causality contradiction: {result3['violations'][0]['reason']}")

    # execution + is_comment_only → reject (no code mutation)
    result4 = check_cognition({"type": "execution", "principals": ["minimal_change"]}, ["is_comment_only"])
    assert not result4["valid"], result4
    print(f"PASS execution+comment_only rejected: {result4['violations'][0]['reason']}")

    # Format feedback
    feedback = format_cognition_feedback(result)
    assert "[cognition]" in feedback
    print(f"PASS format_cognition_feedback: {feedback}")
