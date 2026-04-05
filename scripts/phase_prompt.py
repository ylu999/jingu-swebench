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

# Load canonical principal guidance from subtype_contracts (p193).
# build_phase_principal_guidance returns "You MUST declare ... You SHOULD also declare ..."
# This is appended to the phase behavior text below.
# Exception-safe: if import fails, fallback to static principal strings.
try:
    from subtype_contracts import build_phase_principal_guidance as _build_pg
    _ANALYZE_PRINCIPAL = _build_pg("ANALYZE") or "You MUST declare PRINCIPALS: causal_grounding."
    _EXECUTE_PRINCIPAL = _build_pg("EXECUTE") or "You MUST declare PRINCIPALS: minimal_change."
    _JUDGE_PRINCIPAL   = _build_pg("JUDGE")   or "You MUST declare PRINCIPALS: result_verification, uncertainty_honesty."
except Exception:
    _ANALYZE_PRINCIPAL = "You MUST declare PRINCIPALS: causal_grounding."
    _EXECUTE_PRINCIPAL = "You MUST declare PRINCIPALS: minimal_change."
    _JUDGE_PRINCIPAL   = "You MUST declare PRINCIPALS: result_verification, uncertainty_honesty."

# Phase guidance = behavior text + principal guidance from contract (p193)
_ANALYZE_GUIDANCE = (
    "Form hypotheses. Identify root cause with causal evidence. Do NOT fix yet. "
    + _ANALYZE_PRINCIPAL
    + " Output: your diagnosis, the causal chain, and why."
)
_EXECUTE_GUIDANCE = (
    "Write the minimal fix targeting the root cause identified in ANALYZE/DECIDE. "
    + _EXECUTE_PRINCIPAL
    + " Output: the patch and why it addresses the root cause."
)
_JUDGE_GUIDANCE = (
    "Verify your fix. Run tests. Check that invariants are preserved. "
    + _JUDGE_PRINCIPAL
    + " Output: FIX_TYPE declaration + evidence that tests pass."
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
