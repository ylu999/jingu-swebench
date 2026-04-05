"""
phase_prompt.py — phase-aware prompt prefix injection (p189)

Provides build_phase_prefix() which returns a user-message prefix string
for the current reasoning phase. Injected at the start of every agent step
so the agent knows which phase it is in and adjusts its behavior accordingly.

Phase guidance is injected as a user message prefix (Option B — safer than
modifying system prompt since mini-SWE-agent may not support dynamic system prompts).

ANALYZE/EXECUTE/JUDGE guidance is derived from subtype_contracts.py (p193)
so prompt vocabulary stays in sync with principal_gate.py enforcement.
"""

# Load canonical guidance from subtype_contracts (p193).
# Exception-safe: if import fails, fallback to static strings below.
try:
    from subtype_contracts import build_phase_principal_guidance as _build_pg
    _ANALYZE_GUIDANCE = _build_pg("ANALYZE") or (
        "Form hypotheses. Identify root cause with causal evidence. Do NOT fix yet. "
        "You MUST declare PRINCIPALS: causal_grounding. "
        "Output: your diagnosis and why."
    )
    _EXECUTE_GUIDANCE = _build_pg("EXECUTE") or (
        "Write the minimal fix targeting the root cause identified in ANALYZE/DECIDE. "
        "You MUST declare PRINCIPALS: minimal_change. "
        "Output: the patch and why it addresses the root cause."
    )
    _JUDGE_GUIDANCE = _build_pg("JUDGE") or (
        "Verify your fix. Run tests. Check invariants. "
        "You MUST declare PRINCIPALS: invariant_preservation. "
        "Output: FIX_TYPE declaration + evidence that tests pass."
    )
except Exception:
    # Fallback: static strings with correct vocabulary
    _ANALYZE_GUIDANCE = (
        "Form hypotheses. Identify root cause with causal evidence. Do NOT fix yet. "
        "You MUST declare PRINCIPALS: causal_grounding. "
        "Output: your diagnosis and why."
    )
    _EXECUTE_GUIDANCE = (
        "Write the minimal fix targeting the root cause identified in ANALYZE/DECIDE. "
        "You MUST declare PRINCIPALS: minimal_change. "
        "Output: the patch and why it addresses the root cause."
    )
    _JUDGE_GUIDANCE = (
        "Verify your fix. Run tests. Check invariants. "
        "You MUST declare PRINCIPALS: invariant_preservation. "
        "Output: FIX_TYPE declaration + evidence that tests pass."
    )

# Phase guidance — one entry per phase in control/reasoning_state.py Phase Literal.
# Each value is the guidance text appended after "[Phase: X]".
# ANALYZE/EXECUTE/JUDGE are derived from SUBTYPE_CONTRACTS (p193) for vocab alignment.
PHASE_GUIDANCE: dict[str, str] = {
    "UNDERSTAND": (
        "Read the issue description and test failures carefully. "
        "Form a clear problem statement before doing anything else."
    ),
    "OBSERVE": (
        "Focus on gathering evidence. Read files, run tests, understand the failing case. "
        "Do NOT write code yet. Output: what you found and what it implies."
    ),
    "ANALYZE": _ANALYZE_GUIDANCE,
    "DECIDE": (
        "Choose the best fix strategy based on your analysis. "
        "Output: which approach you will take and why."
    ),
    "EXECUTE": _EXECUTE_GUIDANCE,
    "JUDGE": _JUDGE_GUIDANCE,
}


def build_phase_prefix(phase: str) -> str:
    """
    Build a user-message prefix string for the given phase.

    Returns "[Phase: OBSERVE] Focus on gathering evidence...\n\n" if phase is known,
    or "" if phase is unknown (safe fallback — no injection).

    Args:
        phase: phase string (e.g. "OBSERVE", "EXECUTE"). String, not enum.
               cp_state.phase is already a plain string in reasoning_state.py.
    """
    guidance = PHASE_GUIDANCE.get(phase, "")
    if not guidance:
        return ""
    return f"[Phase: {phase}] {guidance}\n\n"
