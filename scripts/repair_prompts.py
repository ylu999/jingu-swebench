"""Phase-specific repair prompts for the failure attribution system.
p217 addition: build_sdg_repair_prompt() for structured gate rejection feedback.
v0.3 addition: ResidualGapPayload for evidence-carrying near-miss repair.

Each prompt targets a specific failure type and instructs the agent
to focus on the corresponding repair phase.

Consumed by p209 (repair routing loop) — wired into the attempt loop
in run_with_jingu_gate.py between attempts.

Failure types and routing rules come from failure_classifier.py (p208).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


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


# ── v0.3 Step 3: Structured Residual Failure Detail ──────────────────────────

# Truncation limits (prevent prompt bloat)
_MAX_MESSAGE_LEN = 200
_MAX_TRACEBACK_LEN = 400
_MAX_FAILING_TESTS_EXPANDED = 3


@dataclass
class ResidualFailureDetail:
    """One failing test with structured evidence."""
    test_name: str
    status: str  # "failed" | "error"
    message: str  # short assertion/exception summary, capped
    traceback_excerpt: str  # compact traceback, capped
    file_hint: str | None = None
    line_hint: int | None = None
    symptom_type: str | None = None  # assertion_mismatch / exception / missing_branch / unknown


@dataclass
class ResidualGapPayload:
    """Structured residual gap evidence for near-miss repair prompt."""
    residual_gap_size: int
    failing_tests: list[ResidualFailureDetail] = field(default_factory=list)
    shared_gap_hypothesis: str | None = None
    additional_fail_count: int = 0
    additional_fail_names: list[str] = field(default_factory=list)


def _classify_symptom(message: str, traceback: str) -> str:
    """Light heuristic to classify symptom type from error text."""
    combined = (message + " " + traceback).lower()
    if "assertionerror" in combined or "assert " in combined:
        return "assertion_mismatch"
    if any(e in combined for e in ("keyerror", "attributeerror", "typeerror", "valueerror")):
        return "exception"
    if any(w in combined for w in ("none", "missing", "not found", "not in", "does not exist")):
        return "missing_branch"
    return "unknown"


def _extract_file_hint(traceback: str) -> tuple[str | None, int | None]:
    """Extract most likely file:line from traceback excerpt."""
    # Match Python traceback lines: File "path/to/file.py", line 42
    matches = re.findall(r'File "([^"]+)", line (\d+)', traceback)
    if not matches:
        # Try simpler pattern: path.py:42
        matches = re.findall(r'(\S+\.py):(\d+)', traceback)
    if matches:
        # Take the last match (closest to actual error)
        file_path, line_no = matches[-1]
        # Strip /testbed/ prefix for readability
        file_path = re.sub(r'^/testbed/', '', file_path)
        return file_path, int(line_no)
    return None, None


def _extract_test_traceback(stdout: str, test_name: str) -> tuple[str, str]:
    """Extract assertion message and traceback for a specific failing test.

    Searches stdout for the test's FAIL/ERROR block and extracts the
    assertion message and a compact traceback excerpt.

    Returns (message, traceback_excerpt) — both capped to limits.
    """
    if not stdout or not test_name:
        return "", ""

    # Strategy: find the test name in output, then grab the traceback block.
    # Django unittest format:
    #   ======...
    #   FAIL: test_name (module.Class)
    #   ------...
    #   Traceback (most recent call last):
    #     File "...", line N, in ...
    #   AssertionError: ...
    #   ======...  (next block or end)
    lines = stdout.split("\n")
    block_lines: list[str] = []
    found_test = False
    in_traceback = False

    for line in lines:
        # Start of new test failure block
        if line.startswith("=" * 20):
            if found_test and block_lines:
                break  # end of our test's block — hit next separator
            block_lines = []
            found_test = False
            in_traceback = False
            continue

        # Check if this is the header for our test
        if not found_test and test_name in line:
            found_test = True
            continue

        # Dash separator between header and traceback
        if found_test and line.startswith("-" * 20):
            in_traceback = True
            continue

        # Collect traceback lines
        if found_test and in_traceback:
            block_lines.append(line)

    if not found_test or not block_lines:
        # Fallback: search for test name anywhere and grab surrounding lines
        for i, line in enumerate(lines):
            if test_name in line and any(w in line.upper() for w in ("FAIL", "ERROR")):
                start = max(0, i - 2)
                end = min(len(lines), i + 10)
                block_lines = lines[start:end]
                found_test = True
                break

    if not block_lines:
        return "", ""

    block_text = "\n".join(block_lines)

    # Extract assertion message (last line with "Error" or "assert")
    message = ""
    for bl in reversed(block_lines):
        bl_s = bl.strip()
        if bl_s and ("Error" in bl_s or "assert" in bl_s.lower()):
            message = bl_s
            break
    if not message and block_lines:
        # Fallback: last non-empty line
        for bl in reversed(block_lines):
            if bl.strip():
                message = bl.strip()
                break

    # Cap lengths
    message = message[:_MAX_MESSAGE_LEN]
    traceback_excerpt = block_text[:_MAX_TRACEBACK_LEN]

    return message, traceback_excerpt


def build_residual_gap_payload(
    cv_result: dict,
    nm_state: dict | None = None,
) -> ResidualGapPayload | None:
    """Build structured residual gap payload from CV result.

    Args:
        cv_result: controlled_verify result dict with f2p counts, stdout,
                   and f2p_failing_names (added in v0.3).
        nm_state: NearMissState.to_dict() for stall/backslide info.

    Returns:
        ResidualGapPayload or None if not a near-miss scenario.
    """
    if not cv_result or not isinstance(cv_result, dict):
        return None

    f2p_failed = cv_result.get("f2p_failed") or 0
    if f2p_failed == 0:
        return None  # not a near-miss — all pass

    f2p_failing_names = cv_result.get("f2p_failing_names") or []
    stdout = cv_result.get("stdout") or ""

    # Build per-test details
    details: list[ResidualFailureDetail] = []
    expanded_names = f2p_failing_names[:_MAX_FAILING_TESTS_EXPANDED]
    for test_name in expanded_names:
        message, traceback = _extract_test_traceback(stdout, test_name)
        file_hint, line_hint = _extract_file_hint(traceback)
        symptom = _classify_symptom(message, traceback)
        details.append(ResidualFailureDetail(
            test_name=test_name,
            status="failed",
            message=message,
            traceback_excerpt=traceback,
            file_hint=file_hint,
            line_hint=line_hint,
            symptom_type=symptom,
        ))

    # Additional failures beyond expansion limit
    additional_count = max(0, len(f2p_failing_names) - _MAX_FAILING_TESTS_EXPANDED)
    additional_names = f2p_failing_names[_MAX_FAILING_TESTS_EXPANDED:]

    # Shared gap hypothesis (light heuristic)
    hypothesis = _compute_shared_gap_hypothesis(details)

    return ResidualGapPayload(
        residual_gap_size=f2p_failed,
        failing_tests=details,
        shared_gap_hypothesis=hypothesis,
        additional_fail_count=additional_count,
        additional_fail_names=additional_names,
    )


def _compute_shared_gap_hypothesis(details: list[ResidualFailureDetail]) -> str | None:
    """Light heuristic for shared gap across failing tests."""
    if not details:
        return None

    # Check if failures cluster around same file
    file_hints = [d.file_hint for d in details if d.file_hint]
    if len(file_hints) >= 2:
        from collections import Counter
        file_counts = Counter(file_hints)
        top_file, top_count = file_counts.most_common(1)[0]
        if top_count >= 2:
            return f"Failures cluster around {top_file}"

    # Check if failures share same symptom type
    symptoms = [d.symptom_type for d in details if d.symptom_type and d.symptom_type != "unknown"]
    if len(symptoms) >= 2:
        from collections import Counter
        sym_counts = Counter(symptoms)
        top_sym, top_count = sym_counts.most_common(1)[0]
        if top_count >= 2:
            return f"Failures share symptom type: {top_sym}"

    # Single test — use its symptom as hint
    if len(details) == 1 and details[0].symptom_type:
        return f"Single residual failure: {details[0].symptom_type}"

    return None


def render_residual_gap_evidence(payload: ResidualGapPayload) -> str:
    """Render ResidualGapPayload into prompt-ready text.

    Format: structured but readable, not JSON dump.
    """
    lines = [f"RESIDUAL GAP EVIDENCE\nRemaining failing tests: {payload.residual_gap_size}"]

    for i, detail in enumerate(payload.failing_tests, 1):
        parts = [f"\n[{i}] {detail.test_name}"]
        if detail.symptom_type:
            parts.append(f"  Symptom: {detail.symptom_type}")
        if detail.message:
            parts.append(f"  Message: {detail.message}")
        if detail.file_hint:
            loc = detail.file_hint
            if detail.line_hint:
                loc += f":{detail.line_hint}"
            parts.append(f"  Location hint: {loc}")
        if detail.traceback_excerpt:
            parts.append(f"  Traceback:\n    {detail.traceback_excerpt.replace(chr(10), chr(10) + '    ')}")
        lines.append("\n".join(parts))

    if payload.additional_fail_count > 0:
        names = ", ".join(payload.additional_fail_names[:5])
        lines.append(f"\n(+{payload.additional_fail_count} additional: {names})")

    if payload.shared_gap_hypothesis:
        lines.append(f"\nShared residual gap hypothesis: {payload.shared_gap_hypothesis}")

    return "\n".join(lines)


def _build_residual_gap_protocol(
    evidence: dict,
    patch_context: dict | None = None,
    nm_state: dict | None = None,
    residual_payload: ResidualGapPayload | None = None,
) -> str:
    """Build the 3-step residual gap repair protocol prompt.

    Args:
        evidence: from _extract_evidence() — f2p counts, failing test output.
        patch_context: files_written, patch_summary from previous attempt.
        nm_state: NearMissState.to_dict() — stall/backslide info.
        residual_payload: v0.3 Step 3 — structured failing test details.

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

    # ── v0.3 Step 3: structured residual evidence ──
    if residual_payload and residual_payload.failing_tests:
        parts.append(render_residual_gap_evidence(residual_payload))

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
    residual_payload: ResidualGapPayload | None = None,
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
        residual_payload: v0.3 Step 3 — structured failing test details.

    Returns:
        A non-empty repair prompt string (NBR-compliant).
        Always contains: phase declaration, principals, repair goal, evidence.
    """
    evidence = _extract_evidence(cv_result)

    # v0.3: residual_gap_repair → 3-step constrained protocol (mode switch)
    if repair_mode == "residual_gap_repair":
        next_phase = routing.get("next_phase", "EXECUTE")
        protocol = _build_residual_gap_protocol(
            evidence, patch_context, nm_state, residual_payload,
        )
        # Prepend phase declaration for routing consistency
        header = f"[REPAIR PHASE: {next_phase.upper()}]"
        result = header + "\n\n" + protocol
        # Only append raw test output if no structured payload (avoid duplication)
        if not residual_payload and evidence["failing_tests"]:
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
