"""
phase_prompt.py — phase-aware prompt prefix injection (p189)

Provides build_phase_prefix() which returns a user-message prefix string
for the current reasoning phase. Injected at the start of every agent step
so the agent knows which phase it is in and adjusts its behavior accordingly.

Phase guidance is injected as a user message prefix (Option B — safer than
modifying system prompt since mini-SWE-agent may not support dynamic system prompts).
"""

# Phase guidance — one entry per phase in control/reasoning_state.py Phase Literal.
# Each value is the guidance text appended after "[Phase: X]".
PHASE_GUIDANCE: dict[str, str] = {
    "UNDERSTAND": (
        "Read the issue description and test failures carefully. "
        "Form a clear problem statement before doing anything else."
    ),
    "OBSERVE": (
        "Focus on gathering evidence. Read files, run tests, understand the failing case. "
        "Do NOT write code yet. Output: what you found and what it implies."
    ),
    "ANALYZE": (
        "Form hypotheses. Identify root cause. Do NOT fix yet. "
        "Output: your diagnosis and why."
    ),
    "DECIDE": (
        "Choose the best fix strategy based on your analysis. "
        "Output: which approach you will take and why."
    ),
    "EXECUTE": (
        "Write the minimal fix targeting the root cause identified in ANALYZE/DECIDE. "
        "Output: the patch and why it addresses the root cause."
    ),
    "JUDGE": (
        "Verify your fix. Run tests. Check invariants. "
        "Output: FIX_TYPE declaration + evidence that tests pass."
    ),
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
