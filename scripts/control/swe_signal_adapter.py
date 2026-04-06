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
    → also returns progress_evaluable_event (meta-control gate, NOT a signal)

  extract_verify_signals() — called once after controlled_verify at attempt end
    → task_success ONLY
    → must be applied as a SEPARATE update_reasoning_state() call

Design constraints:
  - actionability = pre-execution readiness (patch non-empty) — NOT post-verify success
  - evidence_gain requires test count increase, NOT just files written (C5: false progress)
  - files written alone = no signal (writing without test progress is not evidence)

B5 — progress_evaluable_event (semantic event gating):
  - Controls whether update_reasoning_state() may advance no_progress_steps
  - NOT a CognitionSignal — it is a meta-control gate on the integrator
  - True only on semantic boundary events:
      1. inner verify returned new result (verify_history grew)
      2. env failure detected (failure is information)
      3. patch first write (False → True transition, not subsequent edits)
      4. step_heartbeat: every OBSERVE_HEARTBEAT_INTERVAL steps (prevents permanent
         stagnation freeze in pure observation loops with no verify/patch signals)
  - Regular read/think/write steps: False → no_progress frozen
  - Rationale: stagnation should be measured at "could-have-progressed" moments,
    not at arbitrary step counts (hardcode) or only at attempt-end (too sparse)

B5.4 — step heartbeat (observe loop escape):
  - Without verify or patch signals, pure OBSERVE loops never advance no_progress_steps.
  - This causes permanent phase lock: decide_next() always returns CONTINUE.
  - Fix: treat every OBSERVE_HEARTBEAT_INTERVAL-th step as a weak evaluable event.
  - Interval is intentionally large (10) to avoid over-sensitivity on fast steps.
  - The heartbeat does NOT inject evidence_gain — it only gates stagnation counting.
"""

from __future__ import annotations

# B5.4: heartbeat interval — every N steps counts as an evaluable event in pure observe loops.
OBSERVE_HEARTBEAT_INTERVAL = 10


def extract_step_signals(
    *,
    tests_passed_count: int,
    tests_passed_prev: int,
    env_error_detected: bool,
    patch_non_empty: bool,
    patch_was_non_empty_prev: bool = False,
    verify_history_len: int = 0,
    verify_history_len_prev: int = 0,
    step_index: int = 0,
) -> tuple[dict, bool, str]:
    """
    Map one agent step's observable data to a partial CognitionSignals dict
    plus a progress_evaluable_event gate and its reason.

    Called once per step. Does NOT set task_success.

    Args:
        tests_passed_count:       number of tests passing after this step
        tests_passed_prev:        number of tests passing before this step
        env_error_detected:       True if the step encountered an environment error
        patch_non_empty:          True if agent has written a non-empty patch
        patch_was_non_empty_prev: True if patch was already non-empty before this step
        verify_history_len:       number of inner-verify results available now
        verify_history_len_prev:  number of inner-verify results before this step
        step_index:               current step number (0-based) — used for B5.4 heartbeat

    Returns:
        (partial_signals, progress_evaluable_event, pee_reason)
        partial_signals:          dict — only keys with signal present
        progress_evaluable_event: bool — True only on semantic boundary events
                                  Pass as update_stagnation= to update_reasoning_state()
        pee_reason:               str — which condition triggered pee (empty if not triggered)
                                  One of: "inner_verify_new", "env_error", "patch_first_write",
                                  "step_heartbeat", or comma-joined if multiple fire.
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

    # ── B5: progress_evaluable_event — meta-control gate ──────────────────────
    # True only on semantic boundary events where stagnation can meaningfully be judged.
    # NOT a signal — passed separately as update_stagnation= to update_reasoning_state().
    #
    # Evaluable events (B5 spec):
    #   1. inner verify returned a new result this step
    #   2. env failure detected (bad information is still information)
    #   3. patch first write: False → True transition (phase boundary: explore → propose)
    #      NOT subsequent patch edits (would re-introduce over-sensitivity)
    #   4. step heartbeat: every OBSERVE_HEARTBEAT_INTERVAL steps (B5.4 — observe escape)
    inner_verify_new = verify_history_len > verify_history_len_prev
    patch_first_write = patch_non_empty and not patch_was_non_empty_prev
    step_heartbeat = (step_index > 0) and (step_index % OBSERVE_HEARTBEAT_INTERVAL == 0)
    progress_evaluable_event = (
        inner_verify_new or env_error_detected or patch_first_write or step_heartbeat
    )

    # Build reason string for observability logging
    reasons = []
    if inner_verify_new:
        reasons.append("inner_verify_new")
    if env_error_detected:
        reasons.append("env_error")
    if patch_first_write:
        reasons.append("patch_first_write")
    if step_heartbeat:
        reasons.append("step_heartbeat")
    pee_reason = ",".join(reasons)

    return partial, progress_evaluable_event, pee_reason


def extract_weak_progress(
    *,
    env_error_detected: bool,
    patch_non_empty: bool,
    latest_tests_passed: int,
) -> bool:
    """
    B3.3 — Log-only weak progress indicator.

    Returns True if the step shows any observable activity (weak signal),
    even when strong evidence_gain criteria are not met.

    Does NOT affect stagnation counter — purely diagnostic / observability.
    Helps distinguish "truly idle steps" from "working but no test progress yet".

    Weak signals (any one triggers):
    - patch_non_empty: agent has written code (actionability but not evidence)
    - env_error_detected: environment noise observed (signal, just bad kind)
    - latest_tests_passed >= 0: any test data available this window
    """
    return patch_non_empty or env_error_detected or latest_tests_passed >= 0


def extract_verify_signals(*, controlled_verify_passed: bool) -> dict:
    """
    Map controlled_verify result to a partial CognitionSignals dict.
    ONLY sets task_success. Must be applied as a SEPARATE update call.

    Called once at attempt end after controlled_verify completes.
    task_success=True triggers VerdictStop(task_success) in decide_next().
    """
    return {"task_success": controlled_verify_passed}
