"""
control/phase_result.py — PhaseResult: typed output of a completed phase.

Every phase in the mini-loop (produce → validate → judge → route) ends by
returning a PhaseResult. Routing reads PhaseResult only — never raw flags.

Design invariants:
  I1: Every phase produces exactly one PhaseResult before any phase transition.
  I2: route is derived from PhaseResult only. Direct reads of no_progress_steps,
      pee, or patch_first_write are forbidden in routing code.
  I3: verdict=REJECTED always routes STOP. No redirect allowed.
  I4: redirect_target must be set iff route=REDIRECT.
  I5: produced=False and verdict=ADMITTED is a contradiction — caught at construction.
  I6: JUDGE phase sets trust_score from p201 trust hierarchy (controlled=100, heuristic=30).

Signal pipeline model:
  [ COGNITION ] → [ EXECUTION ] → [ VERIFICATION ] → [ FEEDBACK ]

Failure taxonomy (derived from batch evidence 2026-04-06, 10 django instances):
  NO_SIGNAL_NO_PATCH            — COGNITION→EXECUTION break (8/10)
  NO_SIGNAL_NO_VERIFY           — EXECUTION→VERIFICATION break (1/10)
  NO_SIGNAL_STALLED_AFTER_VERIFY— VERIFICATION→FEEDBACK break (1/10)
  PRINCIPAL_GATE_LOOP           — contract loop, same (phase,reason) ≥ 3 (0/10 this batch)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional, List

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

# SST: phases with contracts (excludes UNDERSTAND which has no contract, and DESIGN which shares DECIDE's)
# Derived from canonical_symbols.Phase but restricted to contract-bearing phases.
PhaseName = Literal["OBSERVE", "ANALYZE", "DECIDE", "DESIGN", "EXECUTE", "JUDGE"]

PhaseVerdict = Literal[
    "ADMITTED",    # phase completed, advance
    "RETRYABLE",   # phase incomplete, retry with guidance
    "REJECTED",    # hard contract violation, stop attempt
    "SOFT_FAIL",   # quality below threshold, retry with feedback
    "HARD_FAIL",   # unrecoverable, stop attempt
]

RouteAction = Literal[
    "ADVANCE",     # move to next phase
    "RETRY",       # retry current phase with updated hint
    "REDIRECT",    # jump to named phase
    "STOP",        # terminate attempt
]

Outcome = Literal[
    "SUCCESS",
    # NO_PATCH subtypes (p202): classify why agent never produced a valid patch
    "NO_PATCH_NO_ATTEMPT",             # phase never reached EXECUTE (stopped in OBSERVE/ANALYZE/DECIDE)
    "NO_PATCH_NO_WRITE",               # EXECUTE phase entered but files_written=0
    "NO_PATCH_WRITE_FAIL",             # files written but patch empty/invalid (git diff empty)
    "NO_PATCH_ABORTED",                # no_progress_steps fired before patch was produced
    "NO_SIGNAL_NO_VERIFY",             # patch exists, no verify ran
    "NO_SIGNAL_STALLED_AFTER_VERIFY",  # verify ran, no new signal afterward
    "PRINCIPAL_GATE_LOOP",             # same (phase, reason) repeated ≥ 3
    "HARD_FAILURE",                    # unrecoverable error
]

JudgeReason = Literal[
    "controlled_tests_passed",  # SUCCESS: controlled_verify confirmed all pass
    "controlled_tests_failed",  # HARD_FAIL: controlled_verify confirmed failure
    "no_patch_no_attempt",      # NO_PATCH_NO_ATTEMPT: stopped before EXECUTE phase
    "no_patch_no_write",        # NO_PATCH_NO_WRITE: EXECUTE entered, files_written=0
    "no_patch_write_fail",      # NO_PATCH_WRITE_FAIL: wrote files but patch empty
    "no_patch_aborted",         # NO_PATCH_ABORTED: no_progress cut in before patch
    "missing_verify_step",      # NO_SIGNAL_NO_VERIFY: patch exists, no verify ran
    "verify_no_new_signal",     # NO_SIGNAL_STALLED_AFTER_VERIFY: verify ran, no signal
    "principal_gate_loop",      # same RETRYABLE (phase, reason) repeated ≥ 3
    "no_progress_timeout",      # generic P7 exhaustion (fallback)
]


# ---------------------------------------------------------------------------
# PhaseResult
# ---------------------------------------------------------------------------

@dataclass
class PhaseResult:
    """Typed output of a completed phase. All routing must read this, not raw flags."""

    phase: PhaseName
    verdict: PhaseVerdict
    route: RouteAction
    outcome: Outcome
    judge_reason: JudgeReason

    # Optional fields
    redirect_target: Optional[PhaseName] = None
    produced: bool = False          # did this phase produce its artifact?
    trust_score: Optional[int] = None  # p201: 100=controlled, 30=heuristic, None=unknown
    hint: Optional[str] = None      # injected into next attempt context
    validation_errors: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # I3: REJECTED must route STOP
        if self.verdict == "REJECTED" and self.route != "STOP":
            raise ValueError(
                f"PhaseResult invariant I3 violated: verdict=REJECTED but route={self.route}. "
                "REJECTED must always route STOP."
            )
        # I4: redirect_target iff route=REDIRECT
        if self.route == "REDIRECT" and self.redirect_target is None:
            raise ValueError(
                "PhaseResult invariant I4 violated: route=REDIRECT but redirect_target is None."
            )
        if self.route != "REDIRECT" and self.redirect_target is not None:
            raise ValueError(
                f"PhaseResult invariant I4 violated: redirect_target={self.redirect_target} "
                f"set but route={self.route} (only allowed with REDIRECT)."
            )
        # I5: produced=False and verdict=ADMITTED is contradictory
        if not self.produced and self.verdict == "ADMITTED":
            raise ValueError(
                "PhaseResult invariant I5 violated: produced=False but verdict=ADMITTED. "
                "A phase cannot be admitted without producing its artifact."
            )


# ---------------------------------------------------------------------------
# Constructor helpers — one per outcome type
# ---------------------------------------------------------------------------

def phase_result_success(
    phase: PhaseName,
    trust_score: int,
) -> PhaseResult:
    """Agent patch passed controlled_verify. Advance."""
    return PhaseResult(
        phase=phase,
        verdict="ADMITTED",
        route="ADVANCE",
        outcome="SUCCESS",
        judge_reason="controlled_tests_passed",
        produced=True,
        trust_score=trust_score,
    )


def phase_result_no_patch(
    phase: PhaseName,
    subtype: "Outcome | None" = None,
) -> "PhaseResult":
    """Agent never produced a valid patch. Redirect to EXECUTE.

    subtype selects the fine-grained NO_PATCH_* outcome; defaults to NO_PATCH_NO_ATTEMPT
    when omitted (backwards compat).
    """
    outcome: Outcome = subtype if subtype is not None else "NO_PATCH_NO_ATTEMPT"  # type: ignore[assignment]
    reason_map: dict[str, JudgeReason] = {
        "NO_PATCH_NO_ATTEMPT": "no_patch_no_attempt",
        "NO_PATCH_NO_WRITE":   "no_patch_no_write",
        "NO_PATCH_WRITE_FAIL": "no_patch_write_fail",
        "NO_PATCH_ABORTED":    "no_patch_aborted",
    }
    judge_reason: JudgeReason = reason_map.get(str(outcome), "no_patch_no_attempt")  # type: ignore[assignment]

    hint_map = {
        "NO_PATCH_NO_ATTEMPT": (
            "You never reached the execution phase. Stop reading and start writing code. "
            "Identify the target file, write a minimal patch, then run the tests."
        ),
        "NO_PATCH_NO_WRITE": (
            "You entered execution but wrote no files. You must modify source code. "
            "Write a patch to the identified target file now."
        ),
        "NO_PATCH_WRITE_FAIL": (
            "You modified files but the resulting patch was empty or invalid. "
            "Ensure your changes produce a non-empty git diff on source files, not test files."
        ),
        "NO_PATCH_ABORTED": (
            "You were making progress but stalled before producing a patch. "
            "Write the patch now — do not keep reading without writing."
        ),
    }
    hint = hint_map.get(str(outcome), (
        "You must write code. Reading and reasoning without writing a patch is not progress. "
        "Produce a minimal patch that addresses the root cause, then run the tests."
    ))

    return PhaseResult(
        phase=phase,
        verdict="SOFT_FAIL",
        route="REDIRECT",
        outcome=outcome,
        judge_reason=judge_reason,
        redirect_target="EXECUTE",
        produced=False,
        hint=hint,
    )


def phase_result_no_verify(phase: PhaseName) -> PhaseResult:
    """Patch exists but no verify ran. Redirect to JUDGE."""
    return PhaseResult(
        phase=phase,
        verdict="SOFT_FAIL",
        route="REDIRECT",
        outcome="NO_SIGNAL_NO_VERIFY",
        judge_reason="missing_verify_step",
        redirect_target="JUDGE",
        produced=True,
        hint=(
            "A patch exists but the required tests have not been run. "
            "Run the FAIL_TO_PASS tests now. Do not continue without verification results."
        ),
    )


def phase_result_verify_stall(phase: PhaseName) -> PhaseResult:
    """Verify ran but produced no new signal. Redirect to ANALYZE."""
    return PhaseResult(
        phase=phase,
        verdict="RETRYABLE",
        route="REDIRECT",
        outcome="NO_SIGNAL_STALLED_AFTER_VERIFY",
        judge_reason="verify_no_new_signal",
        redirect_target="ANALYZE",
        produced=True,
        hint=(
            "Tests ran but produced no new signal. Your current patch does not fix the root cause. "
            "Return to analysis: identify what the test output tells you about the actual failure, "
            "form a new hypothesis, and revise the patch."
        ),
    )


def phase_result_principal_loop(phase: PhaseName) -> PhaseResult:
    """Same (phase, reason) RETRYABLE repeated ≥ 3 times. Stop attempt."""
    return PhaseResult(
        phase=phase,
        verdict="REJECTED",
        route="STOP",
        outcome="PRINCIPAL_GATE_LOOP",
        judge_reason="principal_gate_loop",
        produced=False,
        hint=(
            "The same contract violation was repeated 3 or more times. "
            "Attempt terminated — this is a contract loop, not an agent failure."
        ),
    )


def phase_result_hard_failure(
    phase: PhaseName,
    trust_score: Optional[int] = None,
) -> PhaseResult:
    """Controlled verify confirmed test failure. Stop attempt."""
    return PhaseResult(
        phase=phase,
        verdict="HARD_FAIL",
        route="STOP",
        outcome="HARD_FAILURE",
        judge_reason="controlled_tests_failed",
        produced=True,
        trust_score=trust_score,
        hint=(
            "Official FAIL_TO_PASS tests confirmed your patch does not fix the required tests. "
            "Revisit your root cause analysis — the fix is incomplete."
        ),
    )


# ---------------------------------------------------------------------------
# Routing helper (I2 enforcement)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Signal classifier — pure function, no side effects
# ---------------------------------------------------------------------------

_EXECUTE_OR_LATER = {"EXECUTE", "JUDGE"}

def _classify_signal_pipeline(
    has_patch: bool,
    has_inner_verify: bool,
    controlled_passed: Optional[int],
    controlled_failed: Optional[int],
    no_progress_steps: int,
    early_stop_reason: str,
    files_written: int = 0,
    phase: str = "OBSERVE",
) -> tuple[Outcome, JudgeReason]:
    """
    Classify where in the signal pipeline the attempt broke down.

    Priority order (most specific → least specific):
      1. controlled_verify result present → SUCCESS or HARD_FAILURE
      2. principal_gate_loop              → PRINCIPAL_GATE_LOOP
      3. no patch produced                → NO_PATCH_* subtype (p202)
         a. phase never reached EXECUTE   → NO_PATCH_NO_ATTEMPT
         b. EXECUTE entered, 0 writes     → NO_PATCH_NO_WRITE
         c. files written, patch invalid  → NO_PATCH_WRITE_FAIL
         d. no_progress fired before patch→ NO_PATCH_ABORTED
      4. patch but no verify              → NO_SIGNAL_NO_VERIFY
      5. patch + verify but stalled       → NO_SIGNAL_STALLED_AFTER_VERIFY
      6. fallback                         → NO_SIGNAL_STALLED_AFTER_VERIFY
    """
    # 1. Controlled verify result is ground truth — highest priority
    if controlled_passed is not None and controlled_failed is not None:
        if controlled_failed == 0:
            return "SUCCESS", "controlled_tests_passed"
        else:
            return "HARD_FAILURE", "controlled_tests_failed"

    # 2. Contract loop — principal gate fired same violation ≥ 3 times
    if early_stop_reason == "principal_gate_loop":
        return "PRINCIPAL_GATE_LOOP", "principal_gate_loop"

    # 3. No patch — classify which stage broke down (p202 NO_PATCH subtypes)
    if not has_patch:
        # 3d. no_progress fired before any patch was produced
        if no_progress_steps > 0:
            return "NO_PATCH_ABORTED", "no_patch_aborted"
        # 3a. phase never reached EXECUTE
        if phase.upper() not in _EXECUTE_OR_LATER:
            return "NO_PATCH_NO_ATTEMPT", "no_patch_no_attempt"
        # 3b. EXECUTE or later, but zero files written
        if files_written == 0:
            return "NO_PATCH_NO_WRITE", "no_patch_no_write"
        # 3c. files written but patch still empty/invalid
        return "NO_PATCH_WRITE_FAIL", "no_patch_write_fail"

    # 4. Patch exists but no verify step ran
    if not has_inner_verify:
        return "NO_SIGNAL_NO_VERIFY", "missing_verify_step"

    # 5. Patch + verify ran but no new signal afterward (no_progress accumulated)
    if no_progress_steps > 0:
        return "NO_SIGNAL_STALLED_AFTER_VERIFY", "verify_no_new_signal"

    # 6. Fallback — patch + verify present but still stopped; treat as stall
    return "NO_SIGNAL_STALLED_AFTER_VERIFY", "no_progress_timeout"


# ---------------------------------------------------------------------------
# Builder — translates runtime state → PhaseResult
# ---------------------------------------------------------------------------

def build_phase_result(
    phase: PhaseName,
    *,
    has_patch: bool,
    has_inner_verify: bool,
    test_results: Optional[dict],
    no_progress_steps: int,
    early_stop_reason: str = "no_signal",
    files_written: int = 0,
) -> PhaseResult:
    """
    Bridge from runtime state to PhaseResult.

    This is the SINGLE entry point for deriving a PhaseResult from control-plane
    state. Callers must not build PhaseResult by reading raw flags outside this
    function — that is an I2 violation.

    Args:
        phase:              Current phase name.
        has_patch:          True iff agent produced a non-empty patch at any point
                            during this attempt (StepMonitorState._prev_patch_non_empty).
        has_inner_verify:   True iff at least one inner_verify event fired
                            (len(StepMonitorState.verify_history) > 0).
        test_results:       jingu_body["test_results"] dict, or None if unavailable.
                            Expected keys: controlled_passed, controlled_failed,
                            ran_tests, last_passed.
        no_progress_steps:  ReasoningState.no_progress_steps at attempt end.
        early_stop_reason:  Reason string from VerdictStop / StepMonitorState
                            (e.g. "no_signal", "principal_gate_loop").

    Returns:
        PhaseResult with all invariants satisfied.
    """
    tr = test_results or {}
    controlled_passed: Optional[int] = tr.get("controlled_passed")
    controlled_failed: Optional[int] = tr.get("controlled_failed")

    outcome, judge_reason = _classify_signal_pipeline(
        has_patch=has_patch,
        has_inner_verify=has_inner_verify,
        controlled_passed=controlled_passed,
        controlled_failed=controlled_failed,
        no_progress_steps=no_progress_steps,
        early_stop_reason=early_stop_reason,
        files_written=files_written,
        phase=phase,
    )

    # Derive trust_score from verification source (p201 trust hierarchy)
    trust_score: Optional[int] = None
    if controlled_passed is not None and controlled_failed is not None:
        trust_score = 100  # controlled_verify — ground truth
    elif tr.get("ran_tests") and tr.get("last_passed") is not None:
        trust_score = 30   # agent-heuristic scan — low trust

    # Dispatch to typed constructor helper
    if outcome == "SUCCESS":
        return phase_result_success(phase, trust_score=trust_score or 100)
    elif outcome == "HARD_FAILURE":
        return phase_result_hard_failure(phase, trust_score=trust_score)
    elif outcome == "PRINCIPAL_GATE_LOOP":
        return phase_result_principal_loop(phase)
    elif outcome in ("NO_PATCH_NO_ATTEMPT", "NO_PATCH_NO_WRITE", "NO_PATCH_WRITE_FAIL", "NO_PATCH_ABORTED"):
        return phase_result_no_patch(phase, subtype=outcome)  # type: ignore[arg-type]
    elif outcome == "NO_SIGNAL_NO_VERIFY":
        return phase_result_no_verify(phase)
    else:  # NO_SIGNAL_STALLED_AFTER_VERIFY
        return phase_result_verify_stall(phase)


# ---------------------------------------------------------------------------
# Routing helper (I2 enforcement)
# ---------------------------------------------------------------------------

def route_from_phase_result(result: PhaseResult) -> tuple[RouteAction, Optional[PhaseName], Optional[str]]:
    """
    Derive (route, redirect_target, hint) from a PhaseResult.

    This is the ONLY legal way to determine routing after a phase completes.
    Callers must not read no_progress_steps, pee, or patch_first_write directly
    for routing decisions — that is an I2 violation.

    Returns:
        (route, redirect_target, hint)
        redirect_target is None unless route=REDIRECT.
    """
    return result.route, result.redirect_target, result.hint
