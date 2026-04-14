"""
phase_prompt.py — phase-aware prompt prefix injection (p189)

Provides build_phase_prefix() which returns a user-message prefix string
for the current reasoning phase. Injected at the start of every agent step
so the agent knows which phase it is in and adjusts its behavior accordingly.

Phase guidance is injected as a user message prefix (Option B — safer than
modifying system prompt since mini-SWE-agent may not support dynamic system prompts).

Phase guidance is derived from cognition_contracts/*.py PROMPT_GUIDANCE (single source
of truth) so prompt vocabulary stays in sync with gate enforcement.
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

# Derived from cognition_contracts/observation_fact_gathering.py (single source of truth).
try:
    from cognition_contracts import observation_fact_gathering as _ofg
    _OBSERVE_GUIDANCE = _ofg.PROMPT_GUIDANCE + _OBSERVE_PRINCIPAL
except Exception:
    _OBSERVE_GUIDANCE = "" + _OBSERVE_PRINCIPAL

# Derived from cognition_contracts/analysis_root_cause.py (single source of truth).
try:
    from cognition_contracts import analysis_root_cause as _arc
    _ANALYZE_GUIDANCE = _arc.PROMPT_GUIDANCE + _ANALYZE_PRINCIPAL
except Exception:
    _ANALYZE_GUIDANCE = "" + _ANALYZE_PRINCIPAL

# Derived from cognition_contracts/decision_fix_direction.py (single source of truth).
try:
    from cognition_contracts import decision_fix_direction as _dfd
    _DECIDE_GUIDANCE = _dfd.PROMPT_GUIDANCE + _DECIDE_PRINCIPAL
except Exception:
    _DECIDE_GUIDANCE = "" + _DECIDE_PRINCIPAL

# Derived from cognition_contracts/execution_code_patch.py (single source of truth).
try:
    from cognition_contracts import execution_code_patch as _ecp
    _EXECUTE_GUIDANCE = _ecp.PROMPT_GUIDANCE + _EXECUTE_PRINCIPAL
except Exception:
    _EXECUTE_GUIDANCE = "" + _EXECUTE_PRINCIPAL

# Derived from cognition_contracts/judge_verification.py (single source of truth).
try:
    from cognition_contracts import judge_verification as _jv
    _JUDGE_GUIDANCE = _jv.PROMPT_GUIDANCE + _JUDGE_PRINCIPAL
except Exception:
    _JUDGE_GUIDANCE = "" + _JUDGE_PRINCIPAL

# Derived from cognition_contracts/design_solution_shape.py (single source of truth).
try:
    from cognition_contracts import design_solution_shape as _dss
    _DESIGN_GUIDANCE = _dss.PROMPT_GUIDANCE + _DESIGN_PRINCIPAL
except Exception:
    _DESIGN_GUIDANCE = "" + _DESIGN_PRINCIPAL

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


def get_phase_guidance(phase: str) -> str:
    """Return the behavioral guidance text for a phase.

    Looks up from PHASE_GUIDANCE dict (which derives from contract PROMPT_GUIDANCE).
    Returns "" for unknown phases (SST fallback).
    """
    return PHASE_GUIDANCE.get(phase.upper(), "")


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
