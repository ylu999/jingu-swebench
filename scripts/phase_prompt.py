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
    "Rules:\n"
    "1. List at least 2 options with tradeoffs before choosing.\n"
    "2. Your selected option must reference a specific option by name.\n"
    "3. Do NOT start coding yet.\n"
    + _DECIDE_PRINCIPAL
)

_EXECUTE_GUIDANCE = (
    "ACTION REQUIRED NOW. Write the patch. Follow the root cause from ANALYZE.\n\n"
    "Rules:\n"
    "1. Write the minimal patch to the location identified in ANALYZE.\n"
    "2. Do NOT re-analyze. Do NOT re-read files. You already know the root cause.\n"
    "3. If no code change is produced this step, the step counts as FAILED.\n"
    "4. Before editing, grep for ALL callers/importers of any function you change.\n"
    "   If you change a signature, decorator, or return type, check every call site.\n"
    "5. Do NOT add backward-compatibility shims unless the issue explicitly requires it.\n"
    + _EXECUTE_PRINCIPAL
)

_JUDGE_GUIDANCE = (
    "Verify your fix. Run tests. Check that invariants are preserved.\n\n"
    "Rules:\n"
    "1. You MUST run at least the failing test.\n"
    "2. Your verdict must be based on test results, not on reading code.\n"
    "3. If uncertain, say so.\n"
    "4. Check scope_completeness: were ALL callers of modified functions checked?\n"
    + _JUDGE_PRINCIPAL
)

_DESIGN_GUIDANCE = (
    "Define the solution shape before writing code.\n\n"
    "Rules:\n"
    "1. Identify which files will be modified and bound the scope.\n"
    "2. List invariants that the fix must preserve.\n"
    "3. If you choose an allowlist approach, justify its completeness.\n"
    "4. Do NOT write production code yet.\n"
    + _DESIGN_PRINCIPAL
)

# Phase guidance — one entry per phase in control/reasoning_state.py Phase Literal.
# Each value is the guidance text appended after "[Phase: X]".
# All phases have complete structure templates that the gate can validate.
PHASE_GUIDANCE: dict[str, str] = {
    "UNDERSTAND": _UNDERSTAND_GUIDANCE,
    "OBSERVE": _OBSERVE_GUIDANCE,
    "ANALYZE": _ANALYZE_GUIDANCE,
    "DECIDE": _DECIDE_GUIDANCE,
    "DESIGN": _DESIGN_GUIDANCE,
    "EXECUTE": _EXECUTE_GUIDANCE,
    "JUDGE": _JUDGE_GUIDANCE,
}


def _get_schema_field_guidance(phase: str) -> str:
    """Render field guidance from bundle schema (SST).

    Returns rendered field list, or "" if phase has no schema (e.g. UNDERSTAND).
    Import failure = deployment broken → let it raise.
    """
    from jingu_onboard import onboard
    from schema_field_guidance import render_schema_field_guidance
    gov = onboard()
    schema = gov.get_constrained_schema(phase.upper())
    if schema:
        return render_schema_field_guidance(schema, phase=phase.upper())
    return ""


def build_phase_prefix(phase: str) -> str:
    """
    Build a user-message prefix string for the given phase.

    Returns "[Phase: OBSERVE] Focus on gathering evidence...\n\n" if phase is known,
    or "" if phase is unknown (safe fallback — no injection).

    Field guidance is rendered from the bundle schema (SST) — not hardcoded here.
    Behavioral guidance (goals, rules, constraints) comes from PHASE_GUIDANCE constants.

    Args:
        phase: phase string (e.g. "OBSERVE", "EXECUTE"). String, not enum.
               cp_state.phase is already a plain string in reasoning_state.py.
    """
    guidance = PHASE_GUIDANCE.get(phase, "")
    if not guidance:
        return ""

    # Append schema-derived field guidance for phases with structured submission
    schema_guidance = _get_schema_field_guidance(phase)
    if schema_guidance:
        guidance = f"{guidance}\n\n{schema_guidance}\n\nWhen ready, call submit_phase_record with these fields."

    return f"[Phase: {phase}] {guidance}\n\n"
