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
        p2p_passed: int
        p2p_failed: int
        failing_tests: str (truncated output tail)
        eval_resolved: bool or None
    """
    f2p_passed = cv_result.get("f2p_passed") or 0
    f2p_failed = cv_result.get("f2p_failed") or 0
    p2p_passed = cv_result.get("p2p_passed") or 0
    p2p_failed = cv_result.get("p2p_failed") or 0
    # output_tail contains pytest output with failing test names
    output_tail = (cv_result.get("output_tail") or "").strip()
    if not output_tail:
        # fallback: try stdout field
        output_tail = (cv_result.get("stdout") or "").strip()
    # Truncate to reasonable size for prompt injection
    if len(output_tail) > 4000:
        output_tail = output_tail[:4000] + "..."
    eval_resolved = cv_result.get("eval_resolved")
    return {
        "f2p_passed": f2p_passed,
        "f2p_failed": f2p_failed,
        "p2p_passed": p2p_passed,
        "p2p_failed": p2p_failed,
        "failing_tests": output_tail,
        "eval_resolved": eval_resolved,
    }


# Per-type repair instructions (deterministic, no LLM)
_REPAIR_INSTRUCTIONS: dict[str, str] = {
    "wrong_direction": (
        "CRITICAL: Your previous fix was COMPLETELY WRONG — zero target tests passed.\n"
        "You MUST change direction entirely.\n\n"
        "=== MANDATORY DIRECTION SEARCH PROTOCOL ===\n\n"
        "STEP 1 — REJECT PREVIOUS HYPOTHESIS:\n"
        "State explicitly why your previous root cause hypothesis was wrong.\n"
        "What evidence did you rely on that turned out to be misleading?\n\n"
        "STEP 2 — GENERATE AT LEAST 2 ALTERNATIVE HYPOTHESES:\n"
        "For EACH hypothesis, you MUST provide:\n"
        "  (a) Root cause hypothesis — what is actually causing the bug?\n"
        "  (b) Candidate files — which file(s) would you modify? "
        "(MUST be DIFFERENT from banned files)\n"
        "  (c) Supporting evidence — what code/behavior supports this hypothesis?\n\n"
        "STEP 3 — SELECT AND JUSTIFY:\n"
        "Choose ONE hypothesis and explain why it is more likely than the other(s).\n"
        "Only then proceed to write code.\n\n"
        "=== ENFORCEMENT ===\n"
        "- BANNED FILES: The system will BLOCK any write to files from attempt 1.\n"
        "- If you write to a banned file, you will receive an immediate VIOLATION.\n"
        "- Candidate files in your hypotheses MUST NOT overlap with banned files.\n"
        "- If you skip the hypothesis step and jump to code, "
        "you WILL repeat the same mistake.\n\n"
        "=== EXAMPLE FORMAT ===\n"
        "PREVIOUS HYPOTHESIS (WRONG): [what you thought was the cause]\n"
        "WHY WRONG: [evidence that disproves it]\n\n"
        "HYPOTHESIS 1:\n"
        "  Root cause: [description]\n"
        "  Files to modify: [file1.py, file2.py]\n"
        "  Evidence: [what supports this]\n\n"
        "HYPOTHESIS 2:\n"
        "  Root cause: [description]\n"
        "  Files to modify: [file3.py]\n"
        "  Evidence: [what supports this]\n\n"
        "SELECTED: Hypothesis [N] because [reasoning]\n"
    ),
    "incomplete_fix": (
        "Your fix made PARTIAL progress — some FAIL_TO_PASS tests pass, "
        "but others still fail.\n\n"
        "=== MANDATORY DESIGN EXTENSION PROTOCOL ===\n\n"
        "STEP 1 — ANALYZE WHAT PASSED vs WHAT FAILED:\n"
        "Read the test output below. Which test cases pass? Which fail?\n"
        "What is the DIFFERENCE between the passing and failing cases?\n\n"
        "STEP 2 — IDENTIFY MISSING COVERAGE:\n"
        "Your previous fix handles SOME cases but not ALL.\n"
        "List the specific scenarios your fix does NOT cover.\n"
        "Which code paths are NOT handled by your current change?\n\n"
        "STEP 3 — EXTEND (do NOT start over):\n"
        "Build ON your existing fix. Add the missing cases.\n"
        "You may need to modify ADDITIONAL files beyond what you changed before.\n"
        "Consider: does the fix need to cover more file types, edge cases, or code paths?\n\n"
        "=== CONSTRAINTS ===\n"
        "- Do NOT discard your previous fix — it is partially correct\n"
        "- Do NOT re-analyze from scratch — the direction is right\n"
        "- You MUST specify which additional files to modify (if any)\n"
        "- You MUST explain what additional cases your extended fix covers"
    ),
    "verify_gap": (
        "Your fix is on the RIGHT TRACK — all target tests pass.\n"
        "However, your change broke existing tests (PASS_TO_PASS regressions).\n\n"
        "CRITICAL: Incremental patching of the same approach will NOT work.\n"
        "The previous attempt tried multiple variations and ALL caused the same regression.\n"
        "You MUST redesign from scratch.\n\n"
        "REDESIGN STRATEGY:\n"
        "1. Read the failing test below — understand what invariant it protects\n"
        "2. Read the target test — understand what behavior it needs\n"
        "3. Identify WHY these two requirements conflict under your previous approach\n"
        "4. Design a NEW approach that satisfies BOTH constraints simultaneously\n"
        "5. The new approach should modify different code paths or use a different mechanism\n\n"
        "DO NOT:\n"
        "- Add guards/conditions to the same patch (already tried, doesn't work)\n"
        "- Narrow the same change (already tried, same regression)\n"
        "- Keep the same overall structure with minor tweaks"
    ),
    "execution_error": (
        "Fix only execution-level issues — patch apply failure, syntax error, "
        "or import error. Do NOT change your solution direction. "
        "The problem is mechanical, not logical. "
        "Ensure your patch applies cleanly and the code compiles."
    ),
    "near_miss": "",  # v0.3: replaced by _build_residual_gap_protocol (keyed on repair_mode)
}


# ── v0.3: Residual Gap Repair Protocol ────────────────────────────────────────
#
# This is a MODE SWITCH, not a longer hint.
# When repair_mode == "residual_gap_repair", the agent enters a constrained
# protocol that forbids broad search and requires structured gap analysis
# before any code change.

def _build_residual_gap_protocol(
    evidence: dict,
    patch_context: dict | None = None,
    nm_state: dict | None = None,
) -> str:
    """Build the 3-step residual gap repair protocol prompt.

    Args:
        evidence: from _extract_evidence() — f2p counts, failing test output.
        patch_context: files_written, patch_summary from previous attempt.
        nm_state: NearMissState.to_dict() — stall/backslide info. Optional for Step 2a.

    Returns:
        Non-empty protocol string with 3 mandatory sections.
    """
    parts: list[str] = []

    # ── Mode declaration ──
    parts.append(
        "=== MODE: RESIDUAL GAP REPAIR ===\n"
        "Your current fix direction is CORRECT. Most target tests pass.\n"
        "You are NOT restarting. You are NOT redesigning.\n"
        "You are closing the RESIDUAL GAP — the small remaining slice that fails."
    )

    # ── Hard constraints (4 semantics) ──
    parts.append(
        "HARD CONSTRAINTS (violations will be REJECTED by scope gate):\n"
        "- Current direction is CORRECT — do NOT restart broad search\n"
        "- Preserve ALL already-passing behavior — ZERO regression tolerated\n"
        "- Smallest possible change — prefer adding a condition over rewriting logic\n"
        "- Default scope is LOCKED to files from previous attempt\n"
        "- To expand scope, you MUST first explain why current scope is insufficient\n"
        "- Do NOT weaken validation, guards, constraints, or matching rules\n"
        "- Do NOT remove checks to make tests pass"
    )

    # ── Stall/backslide warning (from nm_state) ──
    if nm_state:
        if nm_state.get("same_patch_suspected"):
            stall_n = nm_state.get("stall_consecutive", 0)
            parts.append(
                f"⚠ STALL DETECTED: f2p_passed unchanged for {stall_n + 1} consecutive attempts.\n"
                "Your previous surgical approach is NOT making progress.\n"
                "You MUST identify a DIFFERENT mechanism for the residual gap.\n"
                "Do NOT repeat the same patch with minor variations."
            )
        if nm_state.get("backslide_detected"):
            best = nm_state.get("best_f2p_passed", 0)
            best_a = nm_state.get("best_attempt", 0)
            parts.append(
                f"⚠ BACKSLIDE DETECTED: f2p_passed decreased from {best} (attempt {best_a}) "
                f"to {evidence['f2p_passed']}.\n"
                "Your last change made things WORSE. Revert to the approach from "
                f"attempt {best_a} and try a DIFFERENT residual fix."
            )

    # ── Previous attempt context ──
    if patch_context:
        prev_files = patch_context.get("files_written") or []
        if prev_files:
            parts.append(
                f"SCOPE (locked to previous attempt files): {', '.join(prev_files)}\n"
                "Any edit outside these files requires explicit justification in Step 2."
            )

    # ── Evidence summary ──
    gap_line = (
        f"CURRENT STATE: {evidence['f2p_passed']}/{evidence['f2p_passed'] + evidence['f2p_failed']} "
        f"target tests pass. {evidence['f2p_failed']} test(s) still failing."
    )
    if evidence["p2p_failed"]:
        gap_line += f" WARNING: {evidence['p2p_failed']} existing test(s) also broken."
    parts.append(gap_line)

    # ── 3-step output protocol ──
    parts.append(
        "=== MANDATORY 3-STEP OUTPUT PROTOCOL ===\n"
        "You MUST complete each step IN ORDER. Do NOT skip to code.\n\n"
        "── Step 1: RESIDUAL FAILURE ANALYSIS ──\n"
        "Write:\n"
        "  REMAINING_FAILURES: (list each failing test by name if known)\n"
        "  SHARED_GAP: (what do the remaining failures have in common?)\n"
        "  WHY_ALMOST_CORRECT: (why does your current patch solve most tests?)\n"
        "  MISSING_ELEMENT: (the specific edge case, invariant, or code path not covered)\n\n"
        "── Step 2: MINIMAL REPAIR PLAN ──\n"
        "Write:\n"
        "  TARGET_FILES: (file:region for each edit — MUST be within scope)\n"
        "  WHY_SUFFICIENT: (why these edits close the gap without over-reaching)\n"
        "  PRESERVATION: (which already-passing behaviors are preserved and why)\n"
        "  SCOPE_EXPANSION: (NONE, or: why current scope is insufficient + what to add)\n\n"
        "── Step 3: PATCH ──\n"
        "Only after Steps 1-2 are complete, write the minimal code change.\n"
        "The patch MUST stay within the files/regions declared in Step 2.\n"
        "Maximum 30 lines changed. Prefer condition additions over rewrites."
    )

    # ── Enforcement reminder ──
    parts.append(
        "ENFORCEMENT (hard system gates, not suggestions):\n"
        "- Scope gate REJECTS patches introducing new files\n"
        "- Scope gate REJECTS patches > 30 lines changed\n"
        "- Constraint weakening (removing guards/assertions) is flagged\n"
        "- Skipping Step 1 or Step 2 means your patch lacks justification"
    )

    return "\n\n".join(parts)


def build_repair_prompt(
    failure_type: str,
    cv_result: dict,
    routing: dict,
    patch_context: dict | None = None,
    repair_mode: str | None = None,
    nm_state: dict | None = None,
) -> str:
    """Build a phase-specific repair prompt from classified failure.

    Args:
        failure_type: One of wrong_direction/incomplete_fix/verify_gap/execution_error/near_miss
        cv_result: The controlled_verify result dict (cv_flat from jingu_body)
        routing: The routing rule from get_routing() — contains next_phase,
                 repair_goal, required_principals
        patch_context: Optional dict with previous attempt patch info:
                       files_written (list[str]), patch_summary (dict)
        repair_mode: v0.3 — "residual_gap_repair" triggers 3-step protocol.
                     None falls back to failure_type-based routing.
        nm_state: v0.3 — NearMissState.to_dict() for stall/backslide context.

    Returns:
        A non-empty repair prompt string (NBR-compliant).
        Always contains: phase declaration, principals, repair goal, evidence.
    """
    evidence = _extract_evidence(cv_result)

    # v0.3: residual_gap_repair → 3-step constrained protocol (mode switch)
    if repair_mode == "residual_gap_repair":
        next_phase = routing.get("next_phase", "EXECUTE")
        protocol = _build_residual_gap_protocol(evidence, patch_context, nm_state)
        # Prepend phase declaration for routing consistency
        header = f"[REPAIR PHASE: {next_phase.upper()}]"
        result = header + "\n\n" + protocol
        if evidence["failing_tests"]:
            result += "\n\nFailing test output:\n" + evidence["failing_tests"]
        assert result.strip(), "build_repair_prompt produced empty output"
        return result

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

    # For wrong_direction: patch constraint BEFORE evidence (survives truncation)
    if failure_type == "wrong_direction" and patch_context:
        prev_files = patch_context.get("files_written") or []
        prev_summary = patch_context.get("patch_summary") or {}
        prev_root_cause = patch_context.get("prev_root_cause") or ""
        prev_strategy = patch_context.get("prev_strategy_type") or ""
        if prev_files:
            constraint_lines = [
                "PREVIOUS ATTEMPT (FAILED — do NOT repeat this direction):",
                f"  Files modified: {', '.join(prev_files)}",
            ]
            if prev_summary.get("lines_added") or prev_summary.get("lines_removed"):
                constraint_lines.append(
                    f"  Scope: {prev_summary.get('lines_added', 0)} lines added, "
                    f"{prev_summary.get('lines_removed', 0)} lines removed"
                )
            if prev_root_cause:
                # Show A1's root cause so agent knows exactly what to avoid
                rc_preview = prev_root_cause
                constraint_lines.append(
                    f"  Root cause hypothesis (PROVEN WRONG): {rc_preview}"
                )
            if prev_strategy:
                constraint_lines.append(
                    f"  Strategy used (FAILED): {prev_strategy}"
                )
            constraint_lines.append(
                f"\nBANNED FILES (will be BLOCKED by runtime gate): {', '.join(prev_files)}"
            )
            constraint_lines.append(
                "\nFollow the DIRECTION SEARCH PROTOCOL above:\n"
                "  1. State why the previous hypothesis was wrong\n"
                "  2. Generate ≥2 alternative hypotheses with candidate files + evidence\n"
                "  3. Select one and explain why\n"
                "  4. Only then write code — to DIFFERENT files"
            )
            parts.append("\n".join(constraint_lines))

    # Legacy near_miss fallback (only reached if repair_mode not set — pre-v0.3 path)
    if failure_type == "near_miss" and repair_mode != "residual_gap_repair":
        nm_lines = [
            f"RESIDUAL GAP: {evidence['f2p_passed']} of "
            f"{evidence['f2p_passed'] + evidence['f2p_failed']} target tests pass. "
            f"Only {evidence['f2p_failed']} test(s) still failing.",
        ]
        if patch_context:
            prev_files = patch_context.get("files_written") or []
            if prev_files:
                nm_lines.append(f"Files already modified (keep working in these): {', '.join(prev_files)}")
        nm_lines.append(
            "Constraint: max 30 lines changed. Do NOT introduce new files. "
            "Do NOT weaken any existing validation or guard."
        )
        parts.append("\n".join(nm_lines))

    # Evidence section (at end — may be truncated, that's OK)
    evidence_lines = []
    evidence_lines.append(
        f"F2P results: {evidence['f2p_passed']} passed, "
        f"{evidence['f2p_failed']} failed"
    )
    if evidence["p2p_failed"]:
        evidence_lines.append(
            f"P2P regressions: {evidence['p2p_failed']} existing test(s) BROKEN "
            f"(out of {evidence['p2p_passed'] + evidence['p2p_failed']} total)"
        )
    if evidence["eval_resolved"] is not None:
        evidence_lines.append(f"Eval resolved: {evidence['eval_resolved']}")

    # For verify_gap: highlight that the fix works but broke something
    if failure_type == "verify_gap" and evidence["p2p_failed"]:
        evidence_lines.append(
            "DIAGNOSIS: Your fix is CORRECT (all target tests pass). "
            "But it BROKE an existing test. Incremental narrowing of the same approach "
            "was already tried and FAILED. You must REDESIGN your approach entirely."
        )

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
