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

# ── Principal Guidance Source ─────────────────────────────────────────────────
# Phase 3: compile_bundle() is the only runtime path. Load principal guidance
# directly from jingu_onboard (which delegates to compile_bundle).

def _get_principal_guidance(phase: str) -> str:
    """Get principal guidance for a phase from the compiled bundle."""
    try:
        from jingu_onboard import onboard
        gov = onboard()
        return gov.get_phase_prompt(phase) or ""
    except Exception:
        return ""


# Load principal guidance for each phase.
# Exception-safe: if either source fails, fallback to empty strings (SST2).
_UNDERSTAND_PRINCIPAL = _get_principal_guidance("UNDERSTAND")
_OBSERVE_PRINCIPAL = _get_principal_guidance("OBSERVE")
_ANALYZE_PRINCIPAL = _get_principal_guidance("ANALYZE")
_DECIDE_PRINCIPAL = _get_principal_guidance("DECIDE")
_DESIGN_PRINCIPAL = _get_principal_guidance("DESIGN")
_EXECUTE_PRINCIPAL = _get_principal_guidance("EXECUTE")
_JUDGE_PRINCIPAL = _get_principal_guidance("JUDGE")

# ── Phase guidance templates ─────────────────────────────────────────────────
# Each phase has a complete structure template that the gate can validate.
# Structure is a control signal, not decoration.

_UNDERSTAND_GUIDANCE = (
    "Read the issue description and understand what is being asked.\n\n"
    "You MUST produce your understanding in this exact format:\n\n"
    "PHASE: understand\n"
    "PRINCIPALS: constraint_awareness\n\n"
    "PROBLEM_STATEMENT:\n<what exactly is broken — one clear sentence>\n\n"
    "EXPECTED_BEHAVIOR:\n<what should happen>\n\n"
    "ACTUAL_BEHAVIOR:\n<what happens instead>\n\n"
    "SCOPE:\n<which files/modules are likely involved>\n\n"
    "Rules: Do NOT start fixing yet. Do NOT read code yet. First understand the problem.\n"
)

_OBSERVE_GUIDANCE = (
    "Gather evidence by reading files and running tests.\n\n"
    "You MUST produce your observations in this exact format:\n\n"
    "PHASE: observe\n"
    "PRINCIPALS: evidence_completeness\n\n"
    "EVIDENCE:\n"
    "- file/path.py:line — what this shows\n"
    "- file/path.py:line — what this shows\n\n"
    "MISSING_EVIDENCE:\n"
    "- what you still need to check\n\n"
    "Rules: Every observation MUST reference a real file:line. "
    "Do NOT hypothesize yet. Gather facts only.\n"
    + _OBSERVE_PRINCIPAL
)

# Derived from cognition_contracts/analysis_root_cause.py (single source of truth).
from cognition_contracts import analysis_root_cause as _arc
_ANALYZE_GUIDANCE = _arc.PROMPT_GUIDANCE + _ANALYZE_PRINCIPAL

_DECIDE_GUIDANCE = (
    "Choose the best fix strategy based on your analysis.\n\n"
    "You MUST produce your decision in this exact format:\n\n"
    "PHASE: decide\n"
    "PRINCIPALS: option_comparison, constraint_satisfaction\n\n"
    "OPTIONS:\n"
    "- Option 1: <approach> — pros: ... cons: ...\n"
    "- Option 2: <approach> — pros: ... cons: ...\n\n"
    "SELECTED:\n<which option and why>\n\n"
    "CONSTRAINTS:\n<what must NOT break — existing tests, API contracts, etc.>\n\n"
    "Rules: You MUST list at least 2 options with tradeoffs. "
    "SELECTED must reference a specific option. Do NOT start coding yet.\n"
    + _DECIDE_PRINCIPAL
)

_EXECUTE_GUIDANCE = (
    "ACTION REQUIRED NOW. Write the patch. You MUST follow the root cause from ANALYZE.\n\n"
    "You MUST produce your execution plan in this exact format BEFORE writing code:\n\n"
    "PHASE: execute\n"
    "PRINCIPALS: minimal_change\n\n"
    "PLAN:\n<how you will fix it — MUST reference the ROOT_CAUSE from ANALYZE>\n\n"
    "CHANGE_SCOPE:\n<which files/functions will change>\n\n"
    "Then write the patch immediately.\n\n"
    "PLAN is MANDATORY. If you do not produce a PLAN: field listing specific files and changes, "
    "this execution step is incomplete and you will be redirected back to planning.\n\n"
    "Rules:\n"
    "1. PLAN must explicitly reference the root cause identified in ANALYZE.\n"
    "2. Do NOT re-analyze. Do NOT re-read files. You already know the root cause.\n"
    "3. Write the minimal patch to the specific location identified in ANALYZE.\n"
    "4. If no code change is produced this step, this step counts as FAILED.\n"
    "5. If this entire attempt ends without editing any file, the attempt is DISCARDED\n"
    "   and you will be asked to redo it with a stronger penalty. Execute NOW.\n"
    + _EXECUTE_PRINCIPAL
    + "\nSuccess condition: a file is edited with a concrete, minimal code change."
)

_JUDGE_GUIDANCE = (
    "Verify your fix. Run tests. Check that invariants are preserved.\n\n"
    "You MUST produce your judgment in this exact format:\n\n"
    "PHASE: judge\n"
    "PRINCIPALS: invariant_preservation, result_verification\n\n"
    "VERDICT: pass | fail | uncertain\n\n"
    "TEST_RESULTS:\n<which tests you ran and their results>\n\n"
    "CONFIDENCE: high | medium | low\n<why this level>\n\n"
    "SIDE_EFFECTS:\n<what else could break — be honest>\n\n"
    "FIX_TYPE: <fix_type>\n"
    "PRINCIPALS: <principals>\n\n"
    "Rules: You MUST run at least the failing test. VERDICT must be based on test results, "
    "not on reading code. If uncertain, say so.\n"
    + _JUDGE_PRINCIPAL
)

# Phase guidance — one entry per phase in control/reasoning_state.py Phase Literal.
# Each value is the guidance text appended after "[Phase: X]".
# All phases have complete structure templates that the gate can validate.
PHASE_GUIDANCE: dict[str, str] = {
    "UNDERSTAND": _UNDERSTAND_GUIDANCE,
    "OBSERVE": _OBSERVE_GUIDANCE,
    "ANALYZE": _ANALYZE_GUIDANCE,
    "DECIDE": _DECIDE_GUIDANCE,
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
