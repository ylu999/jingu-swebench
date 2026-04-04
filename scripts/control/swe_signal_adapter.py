"""
control/swe_signal_adapter.py — SWE-bench domain adapter for the reasoning control plane.

Weak adapter contract (same as TypeScript DomainSignalAdapter<TInput>):
  - Pure function: no history, no state, no side effects
  - No phase decisions
  - Returns only signals that are PRESENT (absent = caller uses defaults)

IMPORTANT — two separate event types must NOT be mixed in one update call:

  extract_step_signals()   — called once per agent step
    → evidence_gain, hypothesis_narrowing, env_noise, actionability
    → task_success is NEVER set here

  extract_verify_signals() — called once after controlled_verify at attempt end
    → task_success ONLY
    → must be applied as a SEPARATE update_reasoning_state() call

Design constraints:
  - actionability = pre-execution readiness (patch non-empty) — NOT post-verify success
  - evidence_gain requires test count increase, NOT just files written (C5: false progress)
  - files written alone = no signal (writing without test progress is not evidence)
"""
from __future__ import annotations


def extract_step_signals(
    *,
    tests_passed_count: int,
    tests_passed_prev: int,
    env_error_detected: bool,
    patch_non_empty: bool,
) -> dict:
    """
    Map one agent step's observable data to a partial CognitionSignals dict.
    Called once per step. Does NOT set task_success.

    Args:
        tests_passed_count:  number of tests passing after this step
        tests_passed_prev:   number of tests passing before this step
        env_error_detected:  True if the step encountered an environment error
        patch_non_empty:     True if agent has written a non-empty patch

    Returns partial dict — only keys with signal present.
    Absent keys use DEFAULT_SIGNALS (conservative baseline).
    """
    partial: dict = {}

    # evidence_gain: test count increased (not just files written — C5)
    if tests_passed_count > tests_passed_prev:
        partial["evidence_gain"] = 1

    # hypothesis_narrowing: test count increased (same signal — more specific)
    if tests_passed_count > tests_passed_prev:
        partial["hypothesis_narrowing"] = 1

    # actionability: pre-execution readiness — patch exists (not waiting for verify)
    if patch_non_empty:
        partial["actionability"] = 1

    # env_noise: environment error in this step
    if env_error_detected:
        partial["env_noise"] = True

    return partial


def extract_verify_signals(*, controlled_verify_passed: bool) -> dict:
    """
    Map controlled_verify result to a partial CognitionSignals dict.
    ONLY sets task_success. Must be applied as a SEPARATE update call.

    Called once at attempt end after controlled_verify completes.
    task_success=True triggers VerdictStop(task_success) in decide_next().
    """
    return {"task_success": controlled_verify_passed}
