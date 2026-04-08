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
    _ANALYZE_PRINCIPAL = _build_pg("ANALYZE") or ""
    _EXECUTE_PRINCIPAL = _build_pg("EXECUTE") or ""
    _JUDGE_PRINCIPAL   = _build_pg("JUDGE")   or ""
except Exception:
    # Fallback: subtype_contracts unavailable. Use empty string rather than stale
    # hardcoded names — consumers should degrade gracefully without principal hint.
    _ANALYZE_PRINCIPAL = ""
    _EXECUTE_PRINCIPAL = ""
    _JUDGE_PRINCIPAL   = ""

# Phase guidance = behavior text + principal guidance from contract (p193)
_ANALYZE_GUIDANCE = (
    "Identify the root cause with causal evidence. Do NOT write any fix yet.\n\n"
    "You MUST produce your analysis in this exact format:\n\n"
    "ROOT_CAUSE:\n<one specific root cause — not vague>\n\n"
    "EVIDENCE:\n- file/path.py:line - what this shows\n- file/path.py:line - what this shows\n\n"
    "CAUSAL_CHAIN:\n<step-by-step reasoning from evidence to root cause>\n\n"
    "ALTERNATIVES:\n- <other hypothesis> — why ruled out\n\n"
    "Rules: ROOT_CAUSE must be specific. EVIDENCE must reference real files. "
    "CAUSAL_CHAIN must connect evidence → root cause. Do NOT propose fixes here.\n"
    + _ANALYZE_PRINCIPAL
)
_EXECUTE_GUIDANCE = (
    "ACTION REQUIRED NOW. Write the patch. You MUST follow the root cause from ANALYZE.\n\n"
    "You MUST produce your execution plan in this exact format BEFORE writing code:\n\n"
    "PHASE: execution\n"
    "PRINCIPALS: minimal_change\n\n"
    "PLAN:\n<how you will fix it — MUST reference the ROOT_CAUSE from ANALYZE>\n\n"
    "CHANGE_SCOPE:\n<which files/functions will change>\n\n"
    "Then write the patch immediately.\n\n"
    "Rules:\n"
    "1. PLAN must explicitly reference the root cause identified in ANALYZE.\n"
    "2. Do NOT re-analyze. Do NOT re-read files. You already know the root cause.\n"
    "3. Write the minimal patch to the specific location identified in ANALYZE.\n"
    "4. If no code change is produced this step, this step counts as FAILED.\n"
    + _EXECUTE_PRINCIPAL
    + "\nSuccess condition: a file is edited with a concrete, minimal code change."
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
        "Form a clear problem statement before doing anything else. "
        "You may transition to OBSERVE when you have a clear understanding of the problem."
    ),
    "OBSERVE": (
        "Suggested phase: OBSERVE. Gather evidence by reading files and running tests. "
        "Output must include at least one file reference in the format path/to/file.py:line — "
        "for example: EVIDENCE: django/db/models/query.py:234. "
        "You may transition to ANALYZE when you have sufficient evidence to form a hypothesis."
    ),
    "ANALYZE": _ANALYZE_GUIDANCE,
    "DECIDE": (
        "Choose the best fix strategy based on your analysis. "
        "Output: which approach you will take and why. "
        "You may transition to EXECUTE when you have a clear, grounded plan."
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
