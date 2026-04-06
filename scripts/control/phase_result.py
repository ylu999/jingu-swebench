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

PhaseName = Literal["OBSERVE", "ANALYZE", "DECIDE", "EXECUTE", "JUDGE"]

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
    "NO_SIGNAL_NO_PATCH",              # agent never produced a patch
    "NO_SIGNAL_NO_VERIFY",             # patch exists, no verify ran
    "NO_SIGNAL_STALLED_AFTER_VERIFY",  # verify ran, no new signal afterward
    "PRINCIPAL_GATE_LOOP",             # same (phase, reason) repeated ≥ 3
    "HARD_FAILURE",                    # unrecoverable error
]

JudgeReason = Literal[
    "controlled_tests_passed",  # SUCCESS: controlled_verify confirmed all pass
    "controlled_tests_failed",  # HARD_FAIL: controlled_verify confirmed failure
    "execution_not_reached",    # NO_PATCH: agent never produced a patch
    "missing_verify_step",      # PATCH_NO_VERIFY: patch exists, no verify ran
    "verify_no_new_signal",     # PATCH_VERIFY_STALL: verify ran, no new signal
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


def phase_result_no_patch(phase: PhaseName) -> PhaseResult:
    """Agent never produced a patch. Redirect to EXECUTE."""
    return PhaseResult(
        phase=phase,
        verdict="SOFT_FAIL",
        route="REDIRECT",
        outcome="NO_SIGNAL_NO_PATCH",
        judge_reason="execution_not_reached",
        redirect_target="EXECUTE",
        produced=False,
        hint=(
            "You must write code. Reading and reasoning without writing a patch is not progress. "
            "Produce a minimal patch that addresses the root cause, then run the tests."
        ),
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
