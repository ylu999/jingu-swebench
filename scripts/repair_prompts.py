"""Phase-specific repair prompts for the failure attribution system.
p217 addition: build_sdg_repair_prompt() for structured gate rejection feedback.

Each prompt targets a specific failure type and instructs the agent
to focus on the corresponding repair phase.

Consumed by p209 (repair routing loop) — wired into the attempt loop
in run_with_jingu_gate.py between attempts.

Failure types and routing rules come from failure_classifier.py (p208).
"""


def _extract_evidence(cv_result: dict) -> dict:
    """Extract concrete evidence from a controlled_verify result.

    Returns a dict with:
        f2p_passed: int
        f2p_failed: int
        failing_tests: str (truncated output tail)
        eval_resolved: bool or None
    """
    f2p_passed = cv_result.get("f2p_passed") or 0
    f2p_failed = cv_result.get("f2p_failed") or 0
    # output_tail contains pytest output with failing test names
    output_tail = (cv_result.get("output_tail") or "").strip()
    if not output_tail:
        # fallback: try stdout field
        output_tail = (cv_result.get("stdout") or "").strip()
    # Truncate to reasonable size for prompt injection
    if len(output_tail) > 400:
        output_tail = output_tail[:400] + "..."
    eval_resolved = cv_result.get("eval_resolved")
    return {
        "f2p_passed": f2p_passed,
        "f2p_failed": f2p_failed,
        "failing_tests": output_tail,
        "eval_resolved": eval_resolved,
    }


# Per-type repair instructions (deterministic, no LLM)
_REPAIR_INSTRUCTIONS: dict[str, str] = {
    "wrong_direction": (
        "Re-analyze the root cause from scratch. Your previous fix did NOT address "
        "the actual problem — zero FAIL_TO_PASS tests passed. "
        "Do NOT propose a fix yet. First understand WHY the tests fail, "
        "then form a new hypothesis before writing any code."
    ),
    "incomplete_fix": (
        "Your fix made partial progress — some FAIL_TO_PASS tests now pass, "
        "but others still fail. Identify the remaining failing scenarios "
        "and extend your fix to cover them. Do NOT start over; "
        "refine your existing approach to handle the missing cases."
    ),
    "verify_gap": (
        "Your fix may be correct, but verification is incomplete. "
        "All FAIL_TO_PASS tests pass, yet the instance is not marked resolved. "
        "Check for PASS_TO_PASS regressions — your fix may have broken existing tests. "
        "Narrow your change to avoid side effects."
    ),
    "execution_error": (
        "Fix only execution-level issues — patch apply failure, syntax error, "
        "or import error. Do NOT change your solution direction. "
        "The problem is mechanical, not logical. "
        "Ensure your patch applies cleanly and the code compiles."
    ),
}


def build_repair_prompt(failure_type: str, cv_result: dict, routing: dict) -> str:
    """Build a phase-specific repair prompt from classified failure.

    Args:
        failure_type: One of wrong_direction/incomplete_fix/verify_gap/execution_error
        cv_result: The controlled_verify result dict (cv_flat from jingu_body)
        routing: The routing rule from get_routing() — contains next_phase,
                 repair_goal, required_principals

    Returns:
        A non-empty repair prompt string (NBR-compliant).
        Always contains: phase declaration, principals, repair goal, evidence.
    """
    evidence = _extract_evidence(cv_result)
    next_phase = routing.get("next_phase", "unknown")
    repair_goal = routing.get("repair_goal", "")
    principals = routing.get("required_principals", [])
    instruction = _REPAIR_INSTRUCTIONS.get(failure_type, "")

    # Build structured prompt
    parts = []

    # Phase declaration
    parts.append(f"[REPAIR PHASE: {next_phase.upper()}]")

    # Required principals
    if principals:
        parts.append(f"Required principals: {', '.join(principals)}")

    # Repair goal
    if repair_goal:
        parts.append(f"Goal: {repair_goal}")

    # Type-specific instruction
    if instruction:
        parts.append(instruction)

    # Evidence section
    evidence_lines = []
    evidence_lines.append(
        f"F2P results: {evidence['f2p_passed']} passed, "
        f"{evidence['f2p_failed']} failed"
    )
    if evidence["eval_resolved"] is not None:
        evidence_lines.append(f"Eval resolved: {evidence['eval_resolved']}")
    if evidence["failing_tests"]:
        evidence_lines.append(f"Test output:\n{evidence['failing_tests']}")

    if evidence_lines:
        parts.append("Evidence from previous attempt:\n" + "\n".join(evidence_lines))

    result = "\n\n".join(parts)
    # NBR safety: must never return empty
    assert result.strip(), "build_repair_prompt produced empty output"
    return result


# ── p217: SDG repair prompt from GateRejection ──────────────────────────────

def build_sdg_repair_prompt(rejection) -> str:
    """Build a repair prompt from a GateRejection object.

    Combines structured gate feedback (contract + field failures + extracted)
    into a format suitable for agent retry injection.

    Args:
        rejection: GateRejection from gate_rejection.py

    Returns:
        Non-empty repair prompt string. Falls back to basic format if
        build_repair_from_rejection import fails.
    """
    try:
        from gate_rejection import build_repair_from_rejection
        return build_repair_from_rejection(rejection)
    except Exception:
        # Fallback: basic formatting if SDG types unavailable
        parts = [f"[GATE REJECT: {getattr(rejection, 'gate_name', 'unknown')}]"]
        failures = getattr(rejection, "failures", [])
        for f in failures:
            field = getattr(f, "field", "?")
            hint = getattr(f, "hint", "fix this field")
            parts.append(f"- {field}: {hint}")
        return "\n".join(parts) if parts else "Gate rejected. Fix the missing fields."
