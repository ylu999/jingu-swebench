#!/usr/bin/env python3
"""
mini-SWE-agent + Jingu Gate integration.

Runs mini-SWE-agent on SWE-bench instances, then applies Jingu gates
(structural check, apply check) to each submission. Retries with failure
hint if gate fails. Selects best candidate across attempts.

Usage:
  python scripts/run_with_jingu_gate.py \
    --instance-ids django__django-11039 \
    --max-attempts 3 \
    --output results/mini-swe-agent/

Environment:
  Uses Docker (local SWE-bench eval images) for sandbox execution.
  Uses Bedrock (global.anthropic.claude-sonnet-4-5-20250929-v1:0) for LLM.
  Images must be pre-built via: python -m swebench.harness.prepare_images
  Image naming: swebench/sweb.eval.x86_64.<id_with_1776>:latest
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# B1: jingu-trust-gate bridge (subprocess → TS gate)
from jingu_gate_bridge import evaluate_patch_from_traj, build_support_pool, run_patch_gate
# B2: adversarial reviewer (cognitive governance)
from patch_reviewer import review_patch_bedrock, ReviewResult
# B3: retry controller (failure → diagnosis → next strategy)
from retry_controller import build_retry_plan, RetryPlan
from strategy_logger import log_strategy_entry, make_entry as make_strategy_entry
# B4: cognition gate (declaration-vs-patch consistency check)
from declaration_extractor import extract_declaration, extract_last_agent_message
from patch_signals import extract_patch_signals
from cognition_check import check_cognition, format_cognition_feedback
from preflight import run_preflight
# B1-CP: reasoning control plane (Python port of jingu-control-plane v0.3)
from control.reasoning_state import (
    initial_reasoning_state, update_reasoning_state, decide_next,
    normalize_signals, ReasoningState,
    VerdictStop, VerdictRedirect, VerdictAdvance, VerdictContinue,
)
from control.swe_signal_adapter import extract_verify_signals, extract_step_signals, extract_weak_progress

# P-INV-001: run environment invariant checks before any batch work
run_preflight()

# B1 gate mode: "trust_gate" (B1) or "structural" (B0 fallback)
GATE_MODE = "trust_gate"
REVIEWER_ENABLED = False  # B2 reviewer — set True to re-enable
RETRY_CONTROLLER_ENABLED = True  # B3 retry-controller — diagnoses attempt 1, guides attempt 2
# p178: strategy learning — set paths to enable log + table
STRATEGY_LOG_PATH = os.environ.get("STRATEGY_LOG_PATH")   # e.g. /root/results/strategy_log.jsonl
STRATEGY_TABLE_PATH = os.environ.get("STRATEGY_TABLE_PATH")  # e.g. /root/results/strategy_table.json

# ── Execution Identity (RT1/RT6: artifact provenance) ─────────────────────────

def get_execution_identity() -> dict:
    """
    Collect runtime provenance: git commit, image digest, build timestamp.
    RT6: run artifacts must carry their own provenance.
    These values are baked into the image at build time via Dockerfile or entrypoint.
    """
    import subprocess

    def _run(cmd):
        try:
            return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True).strip() or None
        except Exception:
            return None

    git_commit     = os.environ.get("GIT_COMMIT") or _run(["git", "-C", "/app", "rev-parse", "HEAD"])
    image_digest   = os.environ.get("IMAGE_DIGEST") or _run(["cat", "/app/.image_digest"])
    build_timestamp= os.environ.get("BUILD_TIMESTAMP") or _run(["cat", "/app/.build_timestamp"])

    return {
        "git_commit":      git_commit,
        "image_digest":    image_digest,
        "build_timestamp": build_timestamp,
        "runner_version":  os.environ.get("RUNNER_VERSION", "unknown"),
    }

def print_activation_proof(identity: dict) -> None:
    """
    RT4: every critical control-plane feature must emit activation proof at startup.
    Log format is machine-readable: key=value, one per line, prefixed [init].
    """
    print(f"[init] git_commit={identity.get('git_commit') or 'UNKNOWN'}")
    print(f"[init] image_digest={identity.get('image_digest') or 'UNKNOWN'}")
    print(f"[init] build_timestamp={identity.get('build_timestamp') or 'UNKNOWN'}")
    print(f"[init] runner_version={identity.get('runner_version') or 'unknown'}")
    print(f"[init] gate_mode={GATE_MODE}")
    print(f"[init] reviewer_enabled={REVIEWER_ENABLED}")
    print(f"[init] retry_controller_enabled={RETRY_CONTROLLER_ENABLED}")
    print(f"[init] cognition_gate_enabled=True")
    print(f"[init] declaration_protocol=enabled")
    print(f"[init] strategy_log_path={STRATEGY_LOG_PATH or 'disabled'}")
    print(f"[init] strategy_table_path={STRATEGY_TABLE_PATH or 'disabled'}")
    # p186: verdict-driven attempt control — activation proof (RT4)
    from control.reasoning_state import NO_PROGRESS_THRESHOLD as _NPT
    print(f"[init] verdict_routing_enabled=True")
    print(f"[init] no_progress_threshold={_NPT}")
    # p189: stage-aware prompt injection — activation proof (RT4)
    print(f"[init] phase_injection_enabled=True")
    # p191: in-loop judge — activation proof (RT4)
    print(f"[init] in_loop_judge_enabled=True")
    # p192: unified verify prerequisite gate — activation proof (RT4)
    print(f"[init] verify_gate_enabled=True")
    # p188: principal enforcement with phase routing — activation proof (RT4)
    print(f"[init] principal_gate_enabled=True")
    # p194: system-inferred principal diff — activation proof (RT4)
    print(f"[init] principal_inference_enabled=True")

# ── Verify prerequisite gate (p192) ────────────────────────────────────────────

def _verify_prerequisites(cognition_result: str | None = None, judge_result=None) -> tuple[bool, str]:
    """
    Unified prerequisite check before controlled_verify.
    Returns (all_pass, reason_if_fail).

    Checks (in order):
    1. cognition gate result (from p187) — if already checked, use cached result
    2. in-loop judge result (from p191) — if already checked, use cached result

    Exception-safe: any unexpected error returns (True, "") — conservative fallback
    allows controlled_verify to run.
    """
    try:
        # Check cognition gate result
        if cognition_result is not None and cognition_result == "fail":
            return False, "cognition_fail"

        # Check in-loop judge result
        if judge_result is not None and not judge_result.all_pass:
            if not judge_result.patch_non_empty:
                return False, "empty_patch"
            elif not judge_result.patch_format:
                return False, "patch_format_error"
            elif not judge_result.no_semantic_weakening:
                return False, "semantic_weakening"
            else:
                return False, "judge_fail"

        return True, ""
    except Exception:
        # Conservative fallback: allow controlled_verify to run on unexpected errors
        return True, ""

# ── Traj watcher: real-time per-step log ───────────────────────────────────────

class StepMonitorState:
    """
    Shared mutable state for one agent run's step monitor.

    Holds the container_id (available after env starts), the last patch snapshot
    seen during verify, and the verify result history.

    verify_history entries:
      {"step": N, "tests_passed": K, "tests_failed": J, "delta": D, "elapsed_ms": T}
    """
    def __init__(self, instance_id: str, attempt: int, instance: dict):
        self.instance_id = instance_id
        self.attempt = attempt
        self.instance = instance
        self.container_id: str | None = None       # set once container starts
        self.last_verified_patch: str = ""         # patch snapshot at last verify
        self.last_verify_time: float = 0.0         # monotonic timestamp
        self.verify_history: list[dict] = []       # structured signal log
        self.verify_in_flight: bool = False        # debounce flag
        self._lock = __import__("threading").Lock()
        # B2-CP: reasoning control plane state for this attempt
        # Owned here so step monitor can update it on each step.
        # run_with_jingu() reads self.cp_state at attempt boundary.
        self.cp_state = initial_reasoning_state("OBSERVE")
        self._prev_step_tests_passed: int = -1     # tests_passed before current step
        self._last_step_env_error: bool = False    # env mutation seen in latest step
        # B5: state needed to detect semantic boundary events
        self._prev_verify_history_len: int = 0     # inner-verify count before current step
        self._prev_patch_non_empty: bool = False   # patch state before current step
        # p186: verdict-driven attempt control
        # early_stop_verdict: set by _monitored_step when decide_next returns VerdictStop;
        #   run_with_jingu checks this after run_agent() returns to break the attempt loop.
        self.early_stop_verdict = None             # VerdictStop if set, else None
        # pending_redirect_hint: set by _monitored_step when decide_next returns VerdictRedirect;
        #   injected as a user message at the start of the next agent step.
        self.pending_redirect_hint: str = ""       # hint to inject into next step
        # p190: per-phase records — one PhaseRecord appended on each VerdictAdvance.
        # Written into jingu_body["phase_records"] at attempt end.
        self.phase_records: list = []              # list[PhaseRecord]

    def update_cp_with_step_signals(
        self,
        *,
        env_error_detected: bool,
        patch_non_empty: bool,
        cp_state_holder: list | None = None,
    ) -> tuple[bool, str]:
        """
        B5: update control-plane state with step-level signals.
        Called once per agent step from _monitored_step.
        Uses latest_tests_passed() for evidence_gain (requires inner-verify data).

        B5 — progress_evaluable_event semantic gating:
        update_stagnation is now driven by progress_evaluable_event, not hardcoded False.
        Stagnation advances only on semantic boundary events:
          - new inner-verify result
          - env failure (failure is information)
          - patch first write (False → True transition)
        Regular read/think steps: no_progress frozen.

        If cp_state_holder is provided (a single-element list from run_with_jingu),
        reads/writes holder[0] so cp_state persists across attempts.
        Otherwise updates self.cp_state (attempt-scoped).

        Returns (progress_evaluable_event, pee_reason) for logging.
        """
        tests_now = self.latest_tests_passed()
        tests_prev = self._prev_step_tests_passed
        verify_len_now = len(self.verify_history)
        verify_len_prev = self._prev_verify_history_len
        patch_prev = self._prev_patch_non_empty

        # Update prev-state tracking BEFORE computing signals (monotone invariant)
        if tests_now >= 0:
            self._prev_step_tests_passed = tests_now
        self._prev_verify_history_len = verify_len_now
        # B5 latch: _prev_patch_non_empty is monotone — once True, stays True.
        # This tracks "has a patch ever been written this attempt", not "did last step write".
        # Without the latch, any read step followed by a write step re-triggers patch_first_write.
        if patch_non_empty:
            self._prev_patch_non_empty = True

        step_partial, progress_evaluable_event, _pee_reason = extract_step_signals(
            tests_passed_count=tests_now,
            tests_passed_prev=tests_prev,
            env_error_detected=env_error_detected,
            patch_non_empty=patch_non_empty,
            patch_was_non_empty_prev=patch_prev,
            verify_history_len=verify_len_now,
            verify_history_len_prev=verify_len_prev,
        )
        if cp_state_holder is not None:
            cp_state_holder[0] = update_reasoning_state(
                cp_state_holder[0], normalize_signals(step_partial),
                update_stagnation=progress_evaluable_event,  # B5: semantic gate
            )
            _s = cp_state_holder[0]
        else:
            self.cp_state = update_reasoning_state(
                self.cp_state, normalize_signals(step_partial),
                update_stagnation=progress_evaluable_event,  # B5: semantic gate
            )
            _s = self.cp_state
        # B3.1: step log moved to _monitored_step section 3 (has instance_id + attempt)
        return progress_evaluable_event, _pee_reason

    def record_verify(self, step: int, result: dict) -> None:
        with self._lock:
            prev = self.verify_history[-1]["tests_passed"] if self.verify_history else -1
            passed = result.get("tests_passed", -1)
            delta = (passed - prev) if passed >= 0 and prev >= 0 else None
            entry = {
                "step": step,
                "tests_passed": passed,
                "tests_failed": result.get("tests_failed", -1),
                "exit_code": result.get("exit_code", -1),
                "elapsed_ms": result.get("elapsed_ms", 0),
                "delta": delta,
                "kind": result.get("verification_kind", "unknown"),
            }
            self.verify_history.append(entry)
            delta_str = f"  delta={delta:+d}" if delta is not None else ""
            print(f"    [inner-verify] step={step}  "
                  f"passed={passed}  failed={result.get('tests_failed', -1)}"
                  f"{delta_str}  elapsed={result.get('elapsed_ms', 0):.0f}ms  "
                  f"kind={result.get('verification_kind', '?')}",
                  flush=True)

    def latest_tests_passed(self) -> int:
        """Return most recent known tests_passed count, or -1."""
        with self._lock:
            for entry in reversed(self.verify_history):
                if entry["tests_passed"] >= 0:
                    return entry["tests_passed"]
        return -1


def _install_step_monitor(
    instance_id: str,
    attempt: int,
    instance: dict,
    cp_state_holder: list | None = None,
    mode: str = "baseline",
) -> StepMonitorState:
    """
    Replace step logger with a step monitor that:
    1. Logs each step (existing behavior)
    2. Detects patch writes via _msg_has_signal()
    3. Runs controlled_verify in background when patch changes (debounced)

    Returns StepMonitorState — caller must set .container_id once container starts.
    The state's verify_history is the structured inner-loop signal source.

    Debounce rules:
    - Only verify if patch content changed since last verify
    - Only verify if >= VERIFY_DEBOUNCE_S seconds since last verify started
    - At most one verify in flight at a time
    """
    import threading

    VERIFY_DEBOUNCE_S = 5.0   # minimum seconds between verify runs

    from minisweagent.run.benchmarks.swebench import ProgressTrackingAgent
    _orig_step = ProgressTrackingAgent.step
    state = StepMonitorState(instance_id, attempt, instance)

    def _monitored_step(self):
        result = _orig_step(self)

        # ── 1. Log step (existing behavior) ──────────────────────────────────
        snippet = ""
        _latest_assistant_text = ""
        for msg in reversed(self.messages):
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            content = c["text"]
                            break
                if isinstance(content, str):
                    _latest_assistant_text = content
                    snippet = content.replace("\n", " ")[:80]
                break
        print(f"    [step {self.n_calls}] ${self.cost:.2f}  {snippet}", flush=True)

        # ── 1c. Phase 2: in-loop cognition record parse + validate ────────────
        if mode == "jingu" and _latest_assistant_text:
            try:
                from cognition_schema import check_step_cognition, format_violation_feedback
                _cog_record, _cog_violations = check_step_cognition(
                    _latest_assistant_text, step_n=self.n_calls
                )
                if _cog_record is not None:
                    _principal_str = " ".join(_cog_record.principals) if _cog_record.principals else "(none)"
                    print(
                        f"    [cognition] step={self.n_calls} phase={_cog_record.phase}"
                        f" principals=[{_principal_str}]"
                        f" evidence={len(_cog_record.evidence_refs)}"
                        f" violations={len(_cog_violations)}",
                        flush=True,
                    )
                    if _cog_violations:
                        _feedback = format_violation_feedback(_cog_violations, _cog_record)
                        print(f"    [cognition] VIOLATION — injecting feedback", flush=True)
                        # Inject violation feedback as a user message so agent
                        # sees it on next step and must correct before proceeding
                        self.messages.append({
                            "role": "user",
                            "content": _feedback,
                        })
            except Exception as _cog_exc:
                print(f"    [cognition] parse error (non-fatal): {_cog_exc}", flush=True)

        # ── 1b. Detect env mutation (ENVIRONMENT_NOT_AGENT_WORK) ─────────────
        _step_env_error = False
        for msg in reversed(self.messages):
            if msg.get("role") == "assistant":
                has_mut, trigger = _msg_has_env_mutation(msg)
                if has_mut:
                    _step_env_error = True
                    print(
                        f"    [env-mutation] ENVIRONMENT_MUTATION_IN_AGENT_LOOP "
                        f"step={self.n_calls} trigger={trigger!r} — "
                        f"agent is doing env work (pip/conda/setup.py). "
                        f"This belongs to infrastructure, not agent reasoning.",
                        flush=True
                    )
                break

        # ── 2. Check for patch write signal ──────────────────────────────────
        # Look at the most recent assistant message for write signals
        _step_patch_non_empty = False
        for msg in reversed(self.messages):
            if msg.get("role") == "assistant":
                if not _msg_has_signal(msg):
                    break  # latest assistant msg, no signal — skip verify
                _step_patch_non_empty = True
                # Signal detected — check debounce conditions
                cid = state.container_id
                if not cid:
                    break  # container not yet started (early steps)
                now = time.monotonic()
                with state._lock:
                    too_soon = (now - state.last_verify_time) < VERIFY_DEBOUNCE_S
                    in_flight = state.verify_in_flight
                if too_soon or in_flight:
                    break  # debounced
                # P1 fix (v2): signal-event driven — trigger verify whenever patch
                # signal is present and debounce passes, regardless of patch content.
                # The original content-diff approach (patch_changed) was wrong because
                # agent writes files directly via editor/bash, so git diff stays the
                # same after the first write → verify never re-triggered.
                # We still fetch git diff to pass as patch_text to run_controlled_verify.
                import subprocess as _sp_iv
                # P4 fix: use "git diff HEAD" to capture both staged and unstaged changes.
                # "git diff" alone only shows unstaged changes — if the agent already ran
                # "git add", the staged diff is invisible and patch_text is empty →
                # run_controlled_verify immediately returns controlled_error(elapsed=0ms).
                _git_diff_result = _sp_iv.run(
                    ["docker", "exec", "-w", "/testbed", cid,
                     "git", "diff", "HEAD"],
                    capture_output=True, text=True, timeout=10,
                )
                current_patch = _git_diff_result.stdout.strip() if _git_diff_result.returncode == 0 else ""
                with state._lock:
                    state.last_verified_patch = current_patch
                    state.last_verify_time = now
                    state.verify_in_flight = True

                step_n = self.n_calls
                print(f"    [inner-verify] triggering verify at step={step_n} "
                      f"(patch changed, container={cid[:12]}...)", flush=True)

                def _run_verify(patch=current_patch, container=cid, step=step_n):
                    try:
                        cv_result = run_controlled_verify(
                            patch, state.instance, container, timeout_s=45
                        )
                        state.record_verify(step, cv_result)
                    except Exception as exc:
                        print(f"    [inner-verify] ERROR: {exc}", flush=True)
                    finally:
                        with state._lock:
                            state.verify_in_flight = False

                t = threading.Thread(target=_run_verify, daemon=True)
                t.start()
                break

        # ── 3. B3-CP: update control-plane state with step-level signals ──────
        # B3.2: step-level does NOT advance no_progress_steps (gated to verify window).
        # B3.3: weak_progress_seen captured for log-only observability (no stagnation effect).
        # task_success is NEVER set here (CORR1).
        _pee, _pee_reason = state.update_cp_with_step_signals(
            env_error_detected=_step_env_error,
            patch_non_empty=_step_patch_non_empty,
            cp_state_holder=cp_state_holder,
        )
        # B3.1+B3.3+B5: emit per-step log with instance/attempt context
        _cp_s = cp_state_holder[0] if cp_state_holder is not None else state.cp_state
        _step_signals_present = bool(_step_env_error or _step_patch_non_empty)
        _weak_progress = extract_weak_progress(
            env_error_detected=_step_env_error,
            patch_non_empty=_step_patch_non_empty,
            latest_tests_passed=state.latest_tests_passed(),
        )
        if _step_signals_present or _weak_progress:
            _pee_str = f"True({_pee_reason})" if _pee else "False"
            print(
                f"    [cp-step] instance={state.instance_id} attempt={state.attempt}"
                f" signals={[k for k,v in [('env',_step_env_error),('patch',_step_patch_non_empty)] if v]}"
                f" no_progress:{_cp_s.no_progress_steps} step:{_cp_s.step_index}"
                f" env_noise:{_cp_s.env_noise} actionability:{_cp_s.actionability}"
                f" weak_progress:{_weak_progress} pee:{_pee_str}",
                flush=True,
            )

        # ── p186: verdict-driven attempt control ─────────────────────────────
        # Call decide_next() on every step so control plane can act in real time.
        # VerdictStop  → set early_stop_verdict; run_with_jingu breaks attempt loop
        # VerdictRedirect → set pending_redirect_hint; injected as user message next step
        # VerdictAdvance  → phase transition telemetry only (observable, no other action)
        # VerdictContinue → no-op
        _step_verdict = decide_next(_cp_s)
        _verdict_to_log = f"step={_cp_s.step_index} verdict={_step_verdict.type}"
        if hasattr(_step_verdict, "to") and _step_verdict.to is not None:
            _verdict_to_log += f" to={_step_verdict.to}"
        if hasattr(_step_verdict, "reason") and _step_verdict.reason:
            _verdict_to_log += f" reason={_step_verdict.reason}"
        print(f"    [cp] {_verdict_to_log}", flush=True)

        if isinstance(_step_verdict, VerdictStop):
            # Early stop: set flag — run_with_jingu will break the attempt loop
            state.early_stop_verdict = _step_verdict
            # P3 fix: actually terminate the agent step loop now, not just set a flag.
            # default.py:105 raises LimitsExceeded when n_calls >= step_limit.
            # Setting n_calls = step_limit causes termination on the NEXT step start,
            # so the current step still completes normally (patch is still extractable).
            _sl = getattr(getattr(self, "config", None), "step_limit", 0)
            if _sl > 0:
                self.n_calls = _sl
                print(
                    f"    [cp] VerdictStop enforcement: n_calls set to step_limit={_sl}"
                    f" — agent will stop after this step",
                    flush=True,
                )
        elif isinstance(_step_verdict, VerdictRedirect):
            # Mid-attempt redirect: inject correction hint into next step's context
            state.pending_redirect_hint = (
                f"[REDIRECT:{_step_verdict.to}] {_step_verdict.reason}"
            )
            # Also inject immediately as a user message so agent sees it on next step
            self.messages.append({
                "role": "user",
                "content": (
                    f"[Control-plane redirect: {_step_verdict.reason}] "
                    f"Re-examine your environment assumptions. "
                    f"Transition to phase {_step_verdict.to} before patching."
                ),
            })
        elif isinstance(_step_verdict, VerdictAdvance):
            # P2 fix: actually advance the phase in cp_state so decide_next sees
            # the new phase on subsequent steps. Without this, phase stays OBSERVE
            # forever and no downstream features (phase_record, principal_gate, etc.)
            # are ever reached.
            _old_phase = _cp_s.phase
            if _step_verdict.to is not None:
                import dataclasses as _dc_adv
                if cp_state_holder is not None:
                    cp_state_holder[0] = _dc_adv.replace(cp_state_holder[0], phase=_step_verdict.to)
                    _cp_s = cp_state_holder[0]
                else:
                    state.cp_state = _dc_adv.replace(state.cp_state, phase=_step_verdict.to)
                    _cp_s = state.cp_state
            print(
                f"    [cp] phase_advance from={_old_phase} to={_step_verdict.to}",
                flush=True,
            )
            # p190: collect PhaseRecord for the phase that just completed.
            # _latest_assistant_text is already captured above (line ~294).
            # Wrapped in try/except: collection failure must not crash main flow.
            _pr = None
            try:
                from declaration_extractor import extract_phase_record as _extract_pr
                _pr = _extract_pr(_latest_assistant_text, str(_cp_s.phase))
                state.phase_records.append(_pr)
                print(
                    f"    [phase_record] phase={_pr.phase} subtype={_pr.subtype}"
                    f" principals={_pr.principals}",
                    flush=True,
                )
            except Exception as _pr_exc:
                print(f"    [phase_record] error (non-fatal): {_pr_exc}", flush=True)
            # p188: principal gate — check required principals for completed phase
            try:
                if _pr is None:
                    raise RuntimeError("phase_record unavailable, skipping principal gate")
                from principal_gate import (
                    check_principal_gate as _check_pg,
                    get_principal_feedback as _get_pg_feedback,
                )
                _pg_violation = _check_pg(_pr, str(_cp_s.phase))
                if _pg_violation:
                    _pg_feedback = _get_pg_feedback(_pg_violation)
                    # p193: add repair_target phase to hint so agent knows where to go
                    try:
                        from subtype_contracts import get_repair_target as _get_repair_target
                        _repair_phase = _get_repair_target(str(_cp_s.phase))
                    except Exception:
                        _repair_phase = ""
                    _repair_suffix = (
                        f" Repair phase: {_repair_phase}." if _repair_phase else ""
                    )
                    state.pending_redirect_hint = (
                        f"[PRINCIPAL_VIOLATION:{_pg_violation}] {_pg_feedback}"
                        f"{_repair_suffix}"
                    )
                    print(
                        f"    [principal_gate] phase={_pr.phase} violation={_pg_violation}"
                        f" repair_target={_repair_phase or 'none'}",
                        flush=True,
                    )
                else:
                    print(
                        f"    [principal_gate] phase={_pr.phase} violation=none",
                        flush=True,
                    )
            except Exception as _pg_exc:
                print(f"    [principal_gate] error={_pg_exc}", flush=True)
            # p194: inference check — three-way diff (fake / missing_required / missing_expected)
            # Exception-safe: any failure here must not crash main flow.
            _inf_violation: str | None = None
            _inferred: list = []
            _inf_diff: dict = {}
            try:
                if _pr is None:
                    raise RuntimeError("phase_record unavailable, skipping inference check")
                from principal_gate import check_principal_inference as _check_pi
                _inf_violation = _check_pi(_pr, str(_cp_s.phase))
                if _inf_violation and "fake_principal" in _inf_violation:
                    try:
                        from subtype_contracts import get_repair_target as _get_repair_target
                        _inf_repair = _get_repair_target(str(_cp_s.phase))
                    except Exception:
                        _inf_repair = ""
                    state.pending_redirect_hint = (
                        f"[PRINCIPAL_MISMATCH:{_inf_violation}] "
                        f"Your declared principals are not supported by your reasoning. "
                        f"Strengthen your reasoning before declaring these principals. "
                        f"Redirect to {_inf_repair or 'previous phase'} phase."
                    )
                elif _inf_violation and "missing_required" in _inf_violation:
                    # Hard reject for missing_required — already handled by principal_gate,
                    # but log the inference perspective for telemetry clarity
                    pass
            except Exception as _pi_exc:
                print(f"    [principal_inference] check error={_pi_exc}", flush=True)
            # Telemetry: write inference results into jingu_body (best-effort, p195)
            try:
                if _pr is None:
                    raise RuntimeError("phase_record unavailable, skipping telemetry")
                from principal_inference import run_inference, diff_principals
                from subtype_contracts import _PHASE_TO_SUBTYPE as _pi_phase_map
                _pi_subtype = _pi_phase_map.get(str(_cp_s.phase).upper(), "")
                _inf_rich = run_inference(_pr, _pi_subtype)
                _inf_diff = diff_principals(
                    getattr(_pr, "principals", []) or [],
                    _inf_rich,
                    phase=str(_cp_s.phase),
                )
            except Exception:
                pass
        # VerdictContinue: no action needed

        # ── p189: phase-aware prompt injection ───────────────────────────────
        # Inject current phase as a user message prefix so agent knows which
        # phase it is in and adjusts behavior accordingly (Option B: user msg).
        # Injected every step so phase context is always fresh.
        # Phase is a plain string in ReasoningState — no .value needed.
        try:
            from phase_prompt import build_phase_prefix as _build_phase_prefix
            _phase_str = str(_cp_s.phase)
            _phase_prefix = _build_phase_prefix(_phase_str)
            if _phase_prefix:
                self.messages.append({
                    "role": "user",
                    "content": _phase_prefix.rstrip("\n"),
                })
                print(f"    [phase_injection] phase={_phase_str} injected=true", flush=True)
        except Exception as _phase_exc:
            print(f"    [phase_injection] error (non-fatal): {_phase_exc}", flush=True)

        return result

    ProgressTrackingAgent.step = _monitored_step
    # p186: register state so run_with_jingu can read early_stop_verdict after run_agent returns
    _STEP_MONITOR_STATES[(instance_id, attempt)] = state
    return state


def _extract_current_patch_from_messages(messages: list[dict]) -> str:
    """
    Extract a lightweight patch fingerprint from agent messages.
    Used for change-detection debounce (not for actual patch application).
    Looks at tool outputs containing diff/patch content.
    """
    for msg in reversed(messages):
        if msg.get("role") != "tool":
            continue
        content = str(msg.get("content", ""))
        if "diff --git" in content or "+++ b/" in content:
            # Return first 500 chars as fingerprint — enough to detect changes
            return content[:500]
    return ""


# ── Telemetry helpers ──────────────────────────────────────────────────────────

def classify_admission(gate_result, patch: str, agent_exit: str | None) -> str:
    """
    Map gate outcome → structured admission reason category.

    Categories:
      admitted                  — gate approved all hunks, no downgrade
      admitted_speculative      — gate admitted but downgraded (LimitsExceeded / no_files / no_traj)
      gate_reject_parse_failed  — patch has no valid diff markers
      gate_reject_apply_failed  — git apply reported failure
      gate_reject_empty_patch   — patch is empty
      gate_reject_too_many_files — patch touches too many files
      gate_reject_other         — any other rejection
      gate_error                — gate runner crashed / timeout
      no_patch                  — agent produced no patch at all
    """
    if patch is None or patch.strip() == "":
        return "no_patch"
    if not gate_result.ok:
        return "gate_error"
    if gate_result.admitted:
        exp = gate_result.explanation
        if exp and exp.downgraded > 0:
            return "admitted_speculative"
        return "admitted"
    # Rejected — classify by reason code
    codes = set(gate_result.reason_codes)
    if "PARSE_FAILED" in codes or "EMPTY_PATCH" in codes:
        return "gate_reject_parse_failed"
    if "APPLY_FAILED" in codes:
        return "gate_reject_apply_failed"
    if "TOO_MANY_FILES" in codes:
        return "gate_reject_too_many_files"
    if "GATE_RUNNER_CRASH" in codes or "GATE_TIMEOUT" in codes:
        return "gate_error"
    return "gate_reject_other"


def patch_fingerprint(patch: str) -> dict:
    """Lightweight structural summary of a patch for attempt_delta comparison."""
    if not patch:
        return {"files": [], "hunks": 0, "lines_added": 0, "lines_removed": 0}
    lines = patch.splitlines()
    files = [l[6:].strip() for l in lines if l.startswith("+++ b/")]
    hunks = sum(1 for l in lines if l.startswith("@@"))
    added = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
    return {"files": sorted(set(files)), "hunks": hunks,
            "lines_added": added, "lines_removed": removed}


# Tool names (structured tool calls) that produce a meaningful write/submit signal
_SIGNAL_TOOL_NAMES: frozenset[str] = frozenset({
    "edit_file", "write_file", "create_file",
    "str_replace_editor", "str_replace", "apply_patch",
    "bash_write", "patch", "submit",
})

# Bash command fragments that indicate a write or submit signal
# Covers: shell file writes (cat > file, tee file), submit sentinel, inline patches
_SIGNAL_BASH_PATTERNS: tuple[str, ...] = (
    "cat >",           # shell file write: cat > /path/to/file
    "tee ",            # shell file write via tee
    "COMPLETE_TASK_AND_SUBMIT",   # SWE-bench submit sentinel
    "> /testbed/",     # redirect-write into testbed
    "str_replace",     # bash str_replace call
    "apply_patch",     # bash apply_patch call
)



# Bash command fragments that indicate environment mutation inside agent loop.
# ENVIRONMENT_NOT_AGENT_WORK: agent must not install packages or modify env during reasoning.
_ENV_MUTATION_PATTERNS: tuple[str, ...] = (
    "pip install",
    "pip3 install",
    "uv pip install",
    "uv add ",
    "python setup.py install",
    "python setup.py develop",
    "poetry install",
    "conda install",
    "apt install",
    "apt-get install",
    "dnf install",
    "yum install",
    "brew install",
)


def _msg_has_env_mutation(msg: dict) -> tuple[bool, str]:
    """
    Return (True, trigger) if an assistant message attempts environment mutation.

    Detects pip install, setup.py install, conda install, etc. inside agent steps.
    These belong to infrastructure/harness, not agent reasoning.
    Violation: ENVIRONMENT_MUTATION_IN_AGENT_LOOP
    """
    def _check_cmd(cmd: str) -> str | None:
        cmd_lower = cmd.lower()
        for pat in _ENV_MUTATION_PATTERNS:
            if pat in cmd_lower:
                return pat
        return None

    # Source 1: structured tool_calls bash commands
    for tc in msg.get("tool_calls", []):
        if tc.get("function", {}).get("name", "").lower() == "bash":
            try:
                import json as _json
                args = tc.get("function", {}).get("arguments", "")
                cmd = (_json.loads(args) if isinstance(args, str) else args).get("command", "")
            except Exception:
                cmd = ""
            trigger = _check_cmd(cmd)
            if trigger:
                return True, trigger

    # Source 2: extra.actions
    for action in msg.get("extra", {}).get("actions", []):
        cmd = action.get("command", "") if isinstance(action, dict) else ""
        trigger = _check_cmd(cmd)
        if trigger:
            return True, trigger

    return False, ""


def _msg_has_signal(msg: dict) -> bool:
    """
    Return True if an assistant message contains at least one write/submit signal.

    Checks two sources:
    1. msg.tool_calls[].function.name — structured tool calls (str_replace_editor etc.)
    2. msg.extra.actions[].command — bash shell commands (cat >, SUBMIT sentinel etc.)

    Both formats appear in real trajs: structured tool calls are in tool_calls,
    the corresponding shell commands are mirrored in extra.actions with a 'command' key.
    """
    # Source 1: structured tool_calls (non-bash tool names)
    for tc in msg.get("tool_calls", []):
        name = tc.get("function", {}).get("name", "").lower()
        if any(sig in name for sig in _SIGNAL_TOOL_NAMES):
            return True
        # bash tool — check command content below
        if name == "bash":
            cmd = ""
            try:
                import json as _json
                args = tc.get("function", {}).get("arguments", "")
                cmd = (_json.loads(args) if isinstance(args, str) else args).get("command", "")
            except Exception:
                pass
            if any(p in cmd for p in _SIGNAL_BASH_PATTERNS):
                return True

    # Source 2: extra.actions (may have 'tool' key or just 'command' key)
    for action in msg.get("extra", {}).get("actions", []):
        if not isinstance(action, dict):
            action_str = str(action).lower()
            if any(sig in action_str for sig in _SIGNAL_TOOL_NAMES):
                return True
            continue
        # Structured action with tool name
        tool_name = action.get("tool", action.get("name", "")).lower()
        if tool_name and any(sig in tool_name for sig in _SIGNAL_TOOL_NAMES):
            return True
        # Shell command content
        cmd = action.get("command", "")
        if cmd and any(p in cmd for p in _SIGNAL_BASH_PATTERNS):
            return True

    return False


def compute_steps_since_last_signal(traj_msgs: list[dict]) -> int:
    """
    Count consecutive trailing steps with no write/submit signal.

    p164 runner layer: feeds steps_since_last_signal into build_retry_plan()
    for P7 no-signal detection (STOP_NO_SIGNAL when >= NO_SIGNAL_THRESHOLD).

    A "step" is one assistant turn. A "signal" is any write or submit action.
    Counts from the end of the conversation backward to the most recent signal.

    Signal detection covers both structured tool calls (str_replace_editor etc.)
    and bash shell commands (cat > file, COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT).
    """
    steps_without_signal = 0
    for msg in reversed(traj_msgs):
        if msg.get("role") != "assistant":
            continue
        if _msg_has_signal(msg):
            break
        steps_without_signal += 1
    return steps_without_signal


# Enforced violation codes detectable from a cognition declaration (Python-side check)
# Mirrors ENFORCED_VIOLATION_CODES in retry_controller.py — keep in sync.
_LOCAL_PATH_PATTERNS = ("/root/", "/home/", "/Users/", "~/.claude", "/tmp/jingu")
_ENV_CHECK_KEYWORDS = (
    "env check", "smoke test", "activation proof", "preflight",
    "node_modules", "npm install", "pip install",
)
_FEEDBACK_KEYWORDS = (
    "verify", "check", "test", "observe", "measure", "confirm",
    "run", "result", "output", "pass", "fail",
)


def extract_principal_violation_codes(decl: dict | None) -> list[str]:
    """
    Lightweight Python-side detection of enforced-principal violations.

    Returns violation codes from ENFORCED_VIOLATION_CODES that are detectable
    from the cognition declaration alone — feeds into build_retry_plan() as
    principal_violation_codes for targeted hint injection.

    Checks:
      ENV_LEAKAGE_HARDCODE_PATH — P_DEBUG_ENV_INDEPENDENCE declared but no env
        validation evidence, OR evidence contains local path patterns
      PLAN_NO_FEEDBACK_LOOP — P_PLAN_CLOSE_THE_LOOP declared but no feedback
        evidence keywords
    """
    if not decl:
        return []
    codes: list[str] = []
    principals = decl.get("principals_used", decl.get("principals", []))
    evidence_items = decl.get("evidence", [])
    evidence_texts = [
        (e.get("content", "") if isinstance(e, dict) else str(e)).lower()
        for e in evidence_items
    ]
    combined_evidence = " ".join(evidence_texts)

    if "P_DEBUG_ENV_INDEPENDENCE" in principals:
        has_local_path = any(p.lower() in combined_evidence for p in _LOCAL_PATH_PATTERNS)
        has_env_check = any(kw in combined_evidence for kw in _ENV_CHECK_KEYWORDS)
        if has_local_path or not has_env_check:
            codes.append("ENV_LEAKAGE_HARDCODE_PATH")

    if "P_PLAN_CLOSE_THE_LOOP" in principals:
        has_feedback = any(kw in combined_evidence for kw in _FEEDBACK_KEYWORDS)
        if not has_feedback:
            codes.append("PLAN_NO_FEEDBACK_LOOP")

    return codes


def build_execution_feedback(
    jingu_body: dict,
    fail_to_pass_tests: list[str],
    patch_fp: dict,
) -> str:
    """
    Build a structured retry hint from execution signal — deterministic, no LLM.

    Converts: test_results + patch fingerprint → actionable hint for attempt 2.
    Three layers: summary → failing tests → example failure excerpt.
    """
    test_results = jingu_body.get("test_results", {})
    tests_ran = test_results.get("ran_tests", False)
    test_passed = test_results.get("last_passed")
    excerpt = test_results.get("excerpt", "")

    if not tests_ran:
        return (
            "Previous attempt submitted without running tests. "
            "Run the required tests FIRST, verify they pass, then submit."
        )

    if test_passed:
        # Agent's own tests passed but fast_eval may still fail — give benefit of doubt
        # Remind agent to verify against the specific FAIL_TO_PASS tests
        tests_str = ", ".join(fail_to_pass_tests[:4])
        return (
            f"Previous attempt's tests passed locally. "
            f"Ensure these specific tests pass: {tests_str}. "
            f"If they already pass, submit immediately."
        )

    # Tests failed — build structured feedback
    parts = ["Previous attempt failed tests.\n"]

    # Layer 1: extract failure/error counts from excerpt
    failures = 0
    errors = 0
    if excerpt:
        fm = re.search(r'(\d+) failure', excerpt)
        em = re.search(r'(\d+) error', excerpt)
        if fm:
            failures = int(fm.group(1))
        if em:
            errors = int(em.group(1))
    if failures or errors:
        parts.append(f"Test results: {failures} failure(s), {errors} error(s)\n")

    # Layer 2: failing test names from FAIL_TO_PASS (most relevant signal)
    if fail_to_pass_tests:
        tests_str = "\n".join(f"  - {t.split('.')[-1]}" for t in fail_to_pass_tests[:6])
        parts.append(f"Tests that must pass:\n{tests_str}\n")

    # Layer 3: compress excerpt to most useful part
    # pytest output: errors/failures section is most useful, summary line is at end
    if excerpt:
        # Try to extract the failure section (between === FAILURES === and === short test summary ===)
        fail_section = re.search(
            r'(={3,} FAILURES ={3,}.*?)(?:={3,}|$)', excerpt, re.DOTALL
        )
        if fail_section:
            parts.append(f"Failure detail:\n{fail_section.group(1)[:600]}\n")
        else:
            # Fallback: last 400 chars of excerpt (usually has summary)
            useful = excerpt[-400:].strip()
            if useful:
                parts.append(f"Test output tail:\n{useful}\n")

    # Files changed (to surface if agent went to wrong files)
    files = patch_fp.get("files", []) if patch_fp else []
    if files:
        parts.append(f"Files you changed: {files}\n")

    parts.append(
        "You must: fix the underlying logic (not just suppress warnings or add code). "
        "Run the failing tests and verify they pass before submitting."
    )

    return "\n".join(parts)


def run_controlled_verify(
    patch_text: str,
    instance: dict,
    container_id: str,
    timeout_s: int = 60,
) -> dict:
    """
    Orchestrator-controlled verification: apply patch + run FAIL_TO_PASS tests.

    Uses the already-running swebench container (same image agent used, no re-pull needed).
    Runs specified tests directly via docker exec, returns structured results.

    Returns a dict with:
      verification_kind: "controlled_fail_to_pass" | "controlled_no_tests" | "controlled_error"
      tests_passed: int (-1 if unknown)
      tests_failed: int (-1 if unknown)
      exit_code: int
      elapsed_ms: float
      output_tail: str  (last 500 chars of test output for debugging)
      error: str (if verification_kind == "controlled_error")

    This is the PRIMARY signal source for tests_passed_after.
    extract_test_counts() is the fallback for when controlled verify is unavailable.
    """
    import subprocess as _sp
    import tempfile as _tf

    t0 = time.monotonic()

    fail_to_pass = instance.get("FAIL_TO_PASS", [])
    if not fail_to_pass:
        return {
            "verification_kind": "controlled_no_tests",
            "tests_passed": -1, "tests_failed": -1,
            "exit_code": -1, "elapsed_ms": 0.0, "output_tail": "",
        }

    if not patch_text or not patch_text.strip():
        return {
            "verification_kind": "controlled_error",
            "tests_passed": 0, "tests_failed": len(fail_to_pass),
            "exit_code": 1, "elapsed_ms": 0.0, "output_tail": "",
            "error": "no patch to apply",
        }

    try:
        # Step 1: write patch to a temp file inside container
        with _tf.NamedTemporaryFile(suffix=".patch", delete=False, mode="w") as f:
            f.write(patch_text)
            host_patch_path = f.name

        # Copy patch into container
        cp_result = _sp.run(
            ["docker", "cp", host_patch_path, f"{container_id}:/tmp/jingu_verify.patch"],
            capture_output=True, text=True, timeout=10,
        )
        os.unlink(host_patch_path)
        if cp_result.returncode != 0:
            return {
                "verification_kind": "controlled_error",
                "tests_passed": -1, "tests_failed": -1,
                "exit_code": -1, "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
                "output_tail": "", "error": f"docker cp failed: {cp_result.stderr[:200]}",
            }

        # Step 2: reset to clean state before applying patch.
        # Agent may have directly modified files in the container via bash tool
        # (agentic mode allows this). git apply will fail with exit_code=128 if
        # the same files are already modified. git stash drops those changes so
        # we can apply the patch cleanly from the original base_commit state.
        _sp.run(
            ["docker", "exec", "-w", "/testbed", container_id,
             "bash", "-c", "git stash --include-untracked -q 2>/dev/null || true"],
            capture_output=True, text=True, timeout=15,
        )

        # Step 3: apply patch (git apply in testbed)
        apply_result = _sp.run(
            ["docker", "exec", "-w", "/testbed", container_id,
             "bash", "-c", "git apply /tmp/jingu_verify.patch 2>&1"],
            capture_output=True, text=True, timeout=30,
        )
        if apply_result.returncode != 0:
            return {
                "verification_kind": "controlled_error",
                "tests_passed": 0, "tests_failed": len(fail_to_pass),
                "exit_code": apply_result.returncode,
                "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
                "output_tail": apply_result.stdout[-300:],
                "error": f"git apply failed: {apply_result.stdout[:200]}",
            }

        # Step 4: run FAIL_TO_PASS tests using official harness command
        test_cmd = _build_test_command(instance)

        test_result = _sp.run(
            ["docker", "exec", container_id, "bash", "-c", test_cmd],
            capture_output=True, text=True, timeout=timeout_s,
        )
        output = (test_result.stdout or "") + (test_result.stderr or "")
        output_tail = output[-500:]

        # Step 5: parse results from output
        passed, failed = _parse_test_output_counts(output)
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)

        # Rollback patch so container is clean for next attempt (if any)
        _sp.run(
            ["docker", "exec", "-w", "/testbed", container_id,
             "bash", "-c", "git apply -R /tmp/jingu_verify.patch 2>/dev/null || git checkout . 2>/dev/null"],
            capture_output=True, text=True, timeout=15,
        )

        return {
            "verification_kind": "controlled_fail_to_pass",
            "tests_passed": passed,
            "tests_failed": failed,
            "exit_code": test_result.returncode,
            "elapsed_ms": elapsed_ms,
            "output_tail": output_tail,
        }

    except _sp.TimeoutExpired:
        return {
            "verification_kind": "controlled_error",
            "tests_passed": -1, "tests_failed": -1,
            "exit_code": -1,
            "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
            "output_tail": "", "error": "controlled verify timed out",
        }
    except Exception as e:
        return {
            "verification_kind": "controlled_error",
            "tests_passed": -1, "tests_failed": -1,
            "exit_code": -1,
            "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
            "output_tail": "", "error": str(e)[:200],
        }


def _build_test_command(instance: dict) -> str:
    """
    Build the exact test command used by the SWE-bench official harness.

    Uses MAP_REPO_VERSION_TO_SPECS[repo][version]["test_cmd"] + get_test_directives(instance),
    wrapped in conda activate testbed — exactly what the official eval script does.

    Returns a bash string suitable for: docker exec ... bash -c "<this>"
    """
    from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
    from swebench.harness.test_spec.python import get_test_directives

    repo = instance["repo"]
    version = instance["version"]
    specs = MAP_REPO_VERSION_TO_SPECS[repo][version]
    test_cmd = specs["test_cmd"]
    directives = get_test_directives(instance)
    directives_str = " ".join(directives)

    # Official harness wraps in: source activate + conda activate testbed + cd /testbed
    return (
        "source /opt/miniconda3/bin/activate && "
        "conda activate testbed && "
        f"cd /testbed && "
        f"{test_cmd} {directives_str} 2>&1"
    )


def _check_onboarding(instance: dict) -> tuple[bool, str]:
    """
    ONBOARDING_FIRST enforcement gate.

    Verifies the instance can be run via the official SWE-bench harness path
    before any agent execution begins. Prevents OFFICIAL_PATH_NOT_CONFIRMED and
    ASSUMED_ENV_BEHAVIOR failure classes.

    Returns (ok, reason).
    """
    if not instance.get("repo") or not instance.get("version"):
        return False, "MISSING_REPO_OR_VERSION"

    try:
        from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
        repo = instance["repo"]
        version = instance["version"]
        if repo not in MAP_REPO_VERSION_TO_SPECS:
            return False, f"OFFICIAL_PATH_NOT_CONFIRMED: repo '{repo}' not in harness specs"
        if version not in MAP_REPO_VERSION_TO_SPECS[repo]:
            return False, f"OFFICIAL_PATH_NOT_CONFIRMED: version '{version}' not in harness specs for {repo}"
    except ImportError as e:
        return False, f"HARNESS_NOT_AVAILABLE: {e}"

    try:
        cmd = _build_test_command(instance)
        if "conda activate testbed" not in cmd:
            return False, "ASSUMED_ENV_BEHAVIOR: test command missing 'conda activate testbed'"
    except Exception as e:
        return False, f"TEST_COMMAND_BUILD_FAILED: {e}"

    if not instance.get("FAIL_TO_PASS"):
        return False, "NO_FAIL_TO_PASS_DEFINED"

    return True, "OK"


def _build_execution_model(instance: dict) -> dict:
    """
    Derive the explicit execution model from the official SWE-bench harness.

    This is the ground truth for what will actually run — not inferred from
    prior experience. Printed as [execution-model] before any agent run.
    """
    from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
    from swebench.harness.test_spec.python import get_test_directives

    repo = instance["repo"]
    version = instance["version"]
    specs = MAP_REPO_VERSION_TO_SPECS[repo][version]

    return {
        "repo": repo,
        "version": version,
        "env": {
            "conda_env": "testbed",
            "workdir": "/testbed",
            "activate": "source /opt/miniconda3/bin/activate && conda activate testbed",
        },
        "test": {
            "runner": "runtests.py" if "runtests.py" in specs["test_cmd"] else "pytest",
            "test_cmd": specs["test_cmd"],
            "directives": get_test_directives(instance),
        },
        "verify": {
            "mode": "controlled",
            "source": "swebench_harness",
        },
    }


def _print_execution_model(model: dict) -> None:
    """Print execution model to stdout (visible in log as [execution-model] block)."""
    print("[execution-model]")
    print(f"  repo: {model['repo']}  version: {model['version']}")
    print(f"  env: conda_env={model['env']['conda_env']}  workdir={model['env']['workdir']}")
    print(f"  test.runner: {model['test']['runner']}")
    print(f"  test.cmd: {model['test']['test_cmd']}")
    print(f"  test.directives: {model['test']['directives']}")
    print(f"  verify: mode={model['verify']['mode']}  source={model['verify']['source']}")


def _parse_test_output_counts(output: str) -> tuple[int, int]:
    """
    Parse passed/failed counts from test output.
    Returns (passed, failed). Both -1 if unparseable.
    """
    # pytest: "3 passed, 2 failed"
    m_pass = re.search(r'(\d+) passed', output)
    m_fail = re.search(r'(\d+) failed', output)
    if m_pass or m_fail:
        passed = int(m_pass.group(1)) if m_pass else 0
        failed = int(m_fail.group(1)) if m_fail else 0
        return passed, failed
    # unittest: "Ran N tests ... OK" or "FAILED (failures=K)"
    ran_m = re.search(r'Ran (\d+) tests? in', output)
    if ran_m:
        total = int(ran_m.group(1))
        fail_m = re.search(r'FAILED \((?:failures=(\d+))?(?:,\s*)?(?:errors=(\d+))?\)', output)
        if fail_m:
            f = int(fail_m.group(1) or 0)
            e = int(fail_m.group(2) or 0)
            return max(0, total - f - e), f + e
        return total, 0  # OK
    # Error exit with no parseable output
    return -1, -1


def extract_test_counts(jingu_body: dict) -> int:
    """
    Extract number of passing tests.

    Priority (highest to lowest):
    1. controlled_verify result (orchestrator-controlled, structured, reliable)
    2. test_results.controlled_passed (promoted from controlled_verify into test_results)
    3. excerpt parsing (fallback for legacy / non-controlled runs)

    Returns total passed count, or -1 if unknown.
    p179: primary reward channel — tests_delta = count(N) - count(N-1).
    """
    jb = jingu_body or {}

    # Priority 1: controlled_verify (orchestrator-controlled)
    cv = jb.get("controlled_verify", {})
    if cv.get("verification_kind") == "controlled_fail_to_pass":
        passed = cv.get("tests_passed", -1)
        if passed >= 0:
            return passed

    # Priority 2: controlled_passed promoted into test_results
    tr_cv_passed = jb.get("test_results", {}).get("controlled_passed")
    if tr_cv_passed is not None and tr_cv_passed >= 0:
        return tr_cv_passed

    # Priority 3: excerpt parsing (fallback)
    tr = jb.get("test_results", {})
    excerpt = tr.get("excerpt", "")
    exit_code = tr.get("exit_code")

    if not excerpt:
        if exit_code == 0:
            return 1
        if exit_code not in (None, ""):
            return 0
        return -1

    # 1. pytest summary: "3 passed" (may have ", 2 failed" after)
    m = re.search(r'(\d+) passed', excerpt)
    if m:
        return int(m.group(1))

    # 2. unittest "Ran N tests in X.XXs" + OK or FAILED
    ran_m = re.search(r'Ran (\d+) tests? in', excerpt)
    if ran_m:
        total = int(ran_m.group(1))
        fail_m = re.search(r'FAILED \((?:failures=(\d+))?(?:,\s*)?(?:errors=(\d+))?\)', excerpt)
        if fail_m:
            failures = int(fail_m.group(1) or 0)
            errors = int(fail_m.group(2) or 0)
            return max(0, total - failures - errors)
        return total  # No FAILED marker -> all passed

    # 3. unittest minimal: ends with "\nOK"
    if re.search(r'\nOK\s*$', excerpt) or excerpt.rstrip() == 'OK':
        ok_count = len(re.findall(r'\.\.\. ok', excerpt))
        return max(1, ok_count)

    # 4. Explicit failure markers (no pass count)
    if re.search(r'FAILED \((?:failures|errors)=\d+\)|\d+ failed|\d+ error', excerpt):
        return 0
    if re.search(r'\nFAILED\s*$', excerpt) or excerpt.rstrip() == 'FAILED':
        return 0

    # 5. Custom script: "ALL TESTS PASSED", unicode checkmark summary
    if re.search(r'ALL TESTS PASSED|all tests passed', excerpt):
        checkmarks = len(re.findall(r'[✓✔]', excerpt))
        return max(1, checkmarks)

    # 6. Custom script: "PASS:" / "FAIL:" line-by-line markers
    pass_lines = len(re.findall(r'\bPASS\b|Test passed!|PASS:', excerpt))
    fail_lines = len(re.findall(r'\bFAIL\b|Test failed!|FAIL:', excerpt))
    if pass_lines > 0 or fail_lines > 0:
        return max(0, pass_lines - fail_lines)

    # 7. exit_code fallback (excerpt is non-test content: code diff, source code)
    if exit_code == 0:
        return 1
    if exit_code not in (None, ""):
        return 0

    return -1


def check_test_progress_invariant(
    tests_passed_prev: int,
    tests_passed_now: int,
) -> tuple[bool, str]:
    """
    p179 gate invariant: TEST_PROGRESS_MONOTONICITY.

    Enforces that attempt N+1 must not regress or stagnate when test counts are known.

    Returns (pass: bool, reason_code: str).

    reason_code values:
      SKIP_NO_PREV       — first attempt or prev count unknown (-1): invariant not applicable
      SKIP_NO_CURRENT    — current count unknown (-1): signal missing, cannot enforce
      POSITIVE_PROGRESS  — tests_delta > 0: invariant satisfied
      NO_TEST_PROGRESS   — tests_delta == 0: stagnant, invariant violated
      TEST_REGRESSION    — tests_delta < 0: regression, invariant violated

    Important: only enforced when BOTH counts are known (≥ 0).
    When signal is missing, falls back to SKIP (soft fail handled by classify_failure_v2).
    """
    if tests_passed_prev < 0:
        return True, "SKIP_NO_PREV"
    if tests_passed_now < 0:
        return True, "SKIP_NO_CURRENT"

    delta = tests_passed_now - tests_passed_prev
    if delta > 0:
        return True, "POSITIVE_PROGRESS"
    if delta == 0:
        return False, "NO_TEST_PROGRESS"
    return False, "TEST_REGRESSION"


def compute_attempt_delta(attempts_log: list[dict]) -> dict | None:
    """
    Compare attempt 1 and attempt 2 fingerprints.
    Returns None if fewer than 2 attempts with patches.
    """
    with_patch = [a for a in attempts_log if a.get("patch_fp")]
    if len(with_patch) < 2:
        return None
    a1, a2 = with_patch[0], with_patch[1]
    fp1, fp2 = a1["patch_fp"], a2["patch_fp"]
    files_changed = set(fp1["files"]) != set(fp2["files"])
    size_delta = (fp2["lines_added"] + fp2["lines_removed"]) - (fp1["lines_added"] + fp1["lines_removed"])
    same_admission = a1["admission_reason"] == a2["admission_reason"]
    return {
        "files_changed": files_changed,
        "size_delta_lines": size_delta,
        "same_admission_reason": same_admission,
        "a1_admission": a1["admission_reason"],
        "a2_admission": a2["admission_reason"],
        "a1_hunks": fp1["hunks"],
        "a2_hunks": fp2["hunks"],
    }

# ── Timing ────────────────────────────────────────────────────────────────────

_t0_global = time.monotonic()

class Timer:
    """Hierarchical timing recorder."""
    def __init__(self, name: str, parent: "Timer | None" = None):
        self.name = name
        self.parent = parent
        self.t0 = time.monotonic()
        self.t1: float | None = None
        self.children: list["Timer"] = []
        if parent is not None:
            parent.children.append(self)

    def stop(self) -> float:
        self.t1 = time.monotonic()
        return self.elapsed

    @property
    def elapsed(self) -> float:
        end = self.t1 if self.t1 is not None else time.monotonic()
        return end - self.t0

    def report(self, indent: int = 0) -> list[str]:
        bar_width = 30
        total = _timing_root.elapsed if _timing_root else self.elapsed
        frac = self.elapsed / total if total > 0 else 0
        bar = "█" * int(frac * bar_width) + "░" * (bar_width - int(frac * bar_width))
        prefix = "  " * indent
        lines = [f"{prefix}{bar} {self.elapsed:6.1f}s  {self.name}"]
        for c in self.children:
            lines.extend(c.report(indent + 1))
        return lines

_timing_root: Timer | None = None
_instance_timers: dict[str, Timer] = {}  # iid -> Timer
# p186: registry mapping (instance_id, attempt) → StepMonitorState.
# Allows run_with_jingu to read early_stop_verdict after run_agent() returns.
_STEP_MONITOR_STATES: dict[tuple, "StepMonitorState"] = {}

# ── Model Usage Tracker ───────────────────────────────────────────────────────

class ModelUsage:
    """Usage data for one instance × attempt."""
    def __init__(self, instance_id: str, attempt: int):
        self.instance_id = instance_id
        self.attempt = attempt
        self.api_calls: int = 0
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.cost_usd: float = 0.0

    def load_from_traj(self, traj_path: Path) -> None:
        """Parse traj.json — primary source is info.model_stats; tokens from messages."""
        if not traj_path.exists():
            return
        try:
            traj = json.loads(traj_path.read_text())
        except (json.JSONDecodeError, OSError):
            return

        stats = traj.get("info", {}).get("model_stats", {})
        self.api_calls = int(stats.get("api_calls", 0))
        self.cost_usd  = float(stats.get("instance_cost", 0.0))

        for m in traj.get("messages", []):
            if m.get("role") != "assistant":
                continue
            usage = m.get("extra", {}).get("response", {}).get("usage", {})
            if usage:
                self.input_tokens  += int(usage.get("prompt_tokens", 0))
                self.output_tokens += int(usage.get("completion_tokens", 0))

    def as_dict(self) -> dict:
        return {
            "api_calls":     self.api_calls,
            "input_tokens":  self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd":      round(self.cost_usd, 4),
        }


class ModelUsageTracker:
    """Aggregates ModelUsage across all instances and attempts."""
    def __init__(self):
        self._by_instance: dict[str, list[ModelUsage]] = {}

    def record(self, usage: ModelUsage) -> None:
        self._by_instance.setdefault(usage.instance_id, []).append(usage)

    def per_instance(self) -> dict[str, dict]:
        out = {}
        for iid, usages in self._by_instance.items():
            out[iid] = {
                "api_calls":     sum(u.api_calls for u in usages),
                "input_tokens":  sum(u.input_tokens for u in usages),
                "output_tokens": sum(u.output_tokens for u in usages),
                "cost_usd":      round(sum(u.cost_usd for u in usages), 4),
                "attempts":      len(usages),
            }
        return out

    def totals(self) -> dict:
        all_u = [u for usages in self._by_instance.values() for u in usages]
        return {
            "api_calls":     sum(u.api_calls for u in all_u),
            "input_tokens":  sum(u.input_tokens for u in all_u),
            "output_tokens": sum(u.output_tokens for u in all_u),
            "cost_usd":      round(sum(u.cost_usd for u in all_u), 4),
        }


_usage_tracker = ModelUsageTracker()

# ── Jingu gates ───────────────────────────────────────────────────────────────

def normalize_patch(patch_text: str) -> str:
    """Pad truncated hunks so git apply does not fail with 'corrupt patch'.

    LLMs sometimes omit the last 1-2 trailing context lines of a hunk.
    git apply counts lines strictly against the @@ header count; a short hunk
    causes 'corrupt patch at line N'.  We detect each hunk's claimed line count
    and append missing blank context lines (' ') at the end of short hunks.
    """
    lines = patch_text.splitlines()
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r'^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@', line)
        if m:
            old_count = int(m.group(1)) if m.group(1) is not None else 1
            new_count = int(m.group(2)) if m.group(2) is not None else 1
            result.append(line)
            i += 1
            old_seen = new_seen = 0
            hunk_lines = []
            while i < len(lines):
                nl = lines[i]
                if re.match(r'^(@@ |diff --git |--- )', nl) or nl.startswith('+++ '):
                    break
                hunk_lines.append(nl)
                if nl.startswith('+') and not nl.startswith('+++'):
                    new_seen += 1
                elif nl.startswith('-') and not nl.startswith('---'):
                    old_seen += 1
                else:
                    old_seen += 1
                    new_seen += 1
                i += 1
            old_missing = old_count - old_seen
            new_missing = new_count - new_seen
            pad = max(old_missing, new_missing)
            for _ in range(pad):
                hunk_lines.append(' ')
            result.extend(hunk_lines)
        else:
            result.append(line)
            i += 1
    normalized = '\n'.join(result)
    if not normalized.endswith('\n'):
        normalized += '\n'
    return normalized


def jingu_structural_check(patch_text: str) -> dict:
    """Check patch has --- / +++ / @@ markers."""
    if not patch_text or len(patch_text.strip()) < 10:
        return {"pass": False, "code": "EMPTY_PATCH", "message": "Patch is empty"}
    if not re.search(r'^(---|[+]{3}|@@)', patch_text, re.MULTILINE):
        return {"pass": False, "code": "PARSE_FAILED", "message": "No diff markers found"}
    return {"pass": True, "code": "ACCEPTED"}

def score_patch(patch_text: str) -> float:
    """Score: prefer small, single-file patches."""
    lines = patch_text.splitlines()
    files = sum(1 for l in lines if l.startswith("+++ b/"))
    changed = sum(1 for l in lines if l.startswith(("+", "-")) and not l.startswith(("+++", "---")))
    score = 1000.0 - files * 50
    return score


def extract_jingu_body(traj: dict, patch_text: str, problem_statement: str = "") -> dict:
    """
    Derive structured jingu_body from traj messages — no LLM call needed.

    jingu_body schema v0: deterministic extraction from observable agent behavior.
    Used by jingu-trust-gate B1+ as structured evidence for admission decisions.
    """
    messages = traj.get("messages", [])
    info = traj.get("info", {})
    exit_status = info.get("exit_status", "")

    # Files read and written — parse from tool call content
    files_read: set[str] = set()
    files_written: set[str] = set()
    test_ran = False
    last_test_passed: bool | None = None
    last_test_excerpt = ""
    tool_calls_made = 0

    # Write signals: collected from multiple sources
    # 1. Patch is ground truth — if patch touches a file, agent wrote it
    for line in (patch_text or "").splitlines():
        if line.startswith("+++ b/"):
            fp = line[6:].strip()
            if fp:
                files_written.add(fp)

    for msg in messages:
        role = msg.get("role", "")
        extra = msg.get("extra", {})
        actions = extra.get("actions", []) if role == "assistant" else []
        for action in actions:
            tool_calls_made += 1
            # Actions may be dicts (structured tool calls) or strings (bash commands)
            if isinstance(action, dict):
                tool_name = action.get("tool", action.get("name", ""))
                tool_input = action.get("input", action.get("arguments", {}))
                # Structured tool calls: look for path/file fields
                path_val = ""
                if isinstance(tool_input, dict):
                    path_val = (tool_input.get("path") or tool_input.get("file_path")
                                or tool_input.get("filename") or "")
                if path_val and ("/" in path_val or path_val.endswith(".py")):
                    write_tools = {"edit_file", "write_file", "create_file",
                                   "str_replace_editor", "str_replace", "apply_patch",
                                   "bash_write", "patch"}
                    read_tools  = {"open_file", "view_file", "read_file",
                                   "str_replace_editor_view", "cat"}
                    if any(t in tool_name.lower() for t in write_tools):
                        files_written.add(path_val)
                    elif any(t in tool_name.lower() for t in read_tools):
                        files_read.add(path_val)
            else:
                # String action (bash command) — limited heuristic, patch is authoritative
                action_str = str(action)
                if any(kw in action_str for kw in ("open_file", "view_file", "cat ")):
                    parts = action_str.split()
                    for i, p in enumerate(parts):
                        if p in ("open_file", "view_file") and i + 1 < len(parts):
                            path_candidate = parts[i + 1].strip("'\"")
                            if "/" in path_candidate or path_candidate.endswith(".py"):
                                files_read.add(path_candidate)

        # Detect test results from tool outputs
        if role == "tool":
            content = str(msg.get("content", ""))
            if any(kw in content for kw in ("PASSED", "FAILED", "passed", "failed", "ERROR", "error")):
                test_ran = True
                if "FAILED" in content or "failed" in content.lower() or "ERROR" in content:
                    last_test_passed = False
                else:
                    last_test_passed = True
                # Extract from <output> tag if present; take last 1500 chars (summary is at end)
                out_match = re.search(r'<output>(.*?)</output>', content, re.DOTALL)
                raw_out = out_match.group(1) if out_match else content
                last_test_excerpt = raw_out[-1500:]

    # Patch summary from patch structure
    patch_lines = patch_text.splitlines() if patch_text else []
    patch_files_changed = sum(1 for l in patch_lines if l.startswith("+++ b/"))
    patch_hunks = sum(1 for l in patch_lines if l.startswith("@@"))
    patch_lines_added = sum(1 for l in patch_lines if l.startswith("+") and not l.startswith("+++"))
    patch_lines_removed = sum(1 for l in patch_lines if l.startswith("-") and not l.startswith("---"))

    return {
        "schema_version": "jingu-body-v0",
        "exit_status": exit_status,
        "problem_understanding": (problem_statement or info.get("problem_statement", ""))[:300],
        "tool_calls_made": tool_calls_made,
        "files_read": sorted(files_read)[:20],
        "files_written": sorted(files_written)[:10],
        "test_results": {
            "ran_tests": test_ran,
            "last_passed": last_test_passed,
            "excerpt": last_test_excerpt,
        },
        "patch_summary": {
            "files_changed": patch_files_changed,
            "hunks": patch_hunks,
            "lines_added": patch_lines_added,
            "lines_removed": patch_lines_removed,
        },
    }

# ── mini-SWE-agent runner (direct Python API) ─────────────────────────────────

# Official mini-swe-agent Verified run config (collection 737e5dd2, run b6e8010b)
# Uses Anthropic direct API with interleaved thinking (reasoning_effort=high)
MODEL = "bedrock/global.anthropic.claude-sonnet-4-5-20250929-v1:0"

BASE_CONFIG = {
    "model": {
        "model_class": "litellm",
        "model_name": MODEL,
        "model_kwargs": {
            "drop_params": True,
            # litellm 1.83 bug: parallel_tool_calls=true/false sends malformed tool_choice to Bedrock.
            # Setting None suppresses the param entirely, which works correctly.
            "parallel_tool_calls": None,
            # Extended thinking via Bedrock: temperature must be 1 (Bedrock requirement)
            "thinking": {"type": "enabled", "budget_tokens": 10000},
            "temperature": 1,
        },
    },
    "environment": {
        "environment_class": "docker",
        "container_timeout": "2h",
        "pull_timeout": 600,  # 10 min — first pull of swebench eval images is slow
    },
    "agent": {
        "mode": "yolo",
        "confirm_exit": False,  # critical: don't wait for user input
        # step_limit comes from jingu-swebench.yaml (250) — matches official swebench.yaml
    },
}

_INSTANCE_CACHE: dict[str, dict] = {}

def _load_instances(instance_ids: list[str], dataset: str = "Lite") -> dict[str, dict]:
    """Load SWE-bench instances in one dataset pass.

    dataset: "Lite"     → SWE-bench/SWE-bench_Lite (300 instances)
             "Verified" → SWE-bench/SWE-bench_Verified (500 instances)
    """
    from datasets import load_dataset
    dataset_name = f"SWE-bench/SWE-bench_{dataset}"
    needed = set(instance_ids) - set(_INSTANCE_CACHE)
    if needed:
        ds = load_dataset(dataset_name, split="test")
        for inst in ds:
            if inst["instance_id"] in needed:
                _INSTANCE_CACHE[inst["instance_id"]] = dict(inst)
    missing = set(instance_ids) - set(_INSTANCE_CACHE)
    if missing:
        raise ValueError(f"Instances not found in {dataset_name}: {missing}")
    return {iid: _INSTANCE_CACHE[iid] for iid in instance_ids}


def _load_instance(instance_id: str, dataset: str = "Lite") -> dict:
    return _load_instances([instance_id], dataset=dataset)[instance_id]

def run_agent(
    instance: dict,
    output_dir: Path,
    attempt: int,
    previous_failure: str = "",
    parent_timer: Timer | None = None,
    mode: str = "jingu",
    cp_state_holder: list | None = None,
) -> tuple[str | None, str | None, dict | None]:
    """Run mini-SWE-agent on one instance. Returns (submission patch or None, exit_status, jingu_body or None)."""
    from minisweagent.run.benchmarks.swebench import process_instance
    from minisweagent.config import get_config_from_spec
    from minisweagent.utils.serialize import recursive_merge

    instance_id = instance["instance_id"]
    attempt_dir = output_dir / f"attempt_{attempt}"
    attempt_dir.mkdir(parents=True, exist_ok=True)

    t_agent = Timer(f"agent attempt={attempt}", parent=parent_timer)

    # Start from jingu-swebench.yaml (fork of swebench.yaml with FORBIDDEN ACTIONS block,
    # patched system_template, and Recommended Workflow steps 2/4/5 removed).
    # Config lives in mini-swe-agent/src/minisweagent/config/benchmarks/jingu-swebench.yaml.
    t_cfg = Timer("config load", parent=t_agent)
    config = get_config_from_spec("jingu-swebench.yaml")
    config = recursive_merge(config, BASE_CONFIG)

    # Build instance_template_extra: tests that must pass + optional retry hint
    extra_parts = []

    # jingu-specific constraint: prevent ENVIRONMENT_NOT_AGENT_WORK violations.
    # baseline uses the official prompt without this block.
    if mode == "jingu":
        extra_parts.append(
            "## FORBIDDEN ACTIONS\n\n"
            "The following actions are STRICTLY FORBIDDEN. Do NOT do any of these:\n\n"
            "- `pip install`, `pip3 install`, `uv pip install`, `python setup.py install`, `conda install`\n"
            "- `apt install`, `apt-get install`, `dnf install`, `brew install`\n"
            "- Installing or configuring any software or dependencies\n\n"
            "The environment is already fully set up. If something appears missing, "
            "read the existing code more carefully — the solution is always a code change, not an environment change."
        )

    # B4: phase-structured reasoning protocol — injects 4-phase structure into
    # agent reasoning loop (Phase 1 activation: prompt-only injection).
    # Vocabulary: CDP v1 taxonomy (p170) — 9 types + 12 principal atoms.
    # Key change vs prior version: FIX_TYPE is derived from ANALYSIS findings,
    # not pre-suggested. Removed "almost always execution" bias.
    extra_parts.append(
        "REASONING PROTOCOL (output these markers as you work — they are parsed by the governance system):\n\n"
        "## STEP 1 — before writing any code, output all three:\n"
        "  PHASE: analysis\n"
        "  PRINCIPALS: causality\n"
        "  EVIDENCE: <file:line or test name that shows the bug>\n"
        "  ROOT_CAUSE: <the specific line or logic that causes the failure>\n\n"
        "## STEP 2 — once root cause is clear, output:\n"
        "  PHASE: decision\n"
        "  PRINCIPALS: constraint_awareness\n"
        "  CLAIMS: <chosen fix type — execution | diagnosis | design | planning>\n"
        "  SCOPE: <which files/functions will be changed>\n\n"
        "## STEP 3 — after writing the patch, output:\n"
        "  PHASE: execution\n"
        "  PRINCIPALS: scope_control minimal_change\n"
        "  EVIDENCE: <which analysis step or file:line justified this change>\n\n"
        "## STEP 4 — before calling submit, output these two lines exactly:\n"
        "  FIX_TYPE: <one of: understanding | observation | analysis | diagnosis | decision | design | planning | execution | validation>\n"
        "  PRINCIPALS: <space-separated list — must satisfy the contract for your chosen type>\n\n"
        "Type contracts:\n"
        "  execution:   required=[scope_control, minimal_change]  forbidden=[causality, hypothesis_testing]\n"
        "  diagnosis:   required=[evidence_based, causality]       forbidden=[minimal_change]\n"
        "  analysis:    required=[causality]                       forbidden=[execution_first, scope_control]\n"
        "  decision:    required=[constraint_awareness]            forbidden=[execution_first]\n"
        "  design:      required=[constraint_awareness, completeness]  forbidden=[execution_first]\n\n"
        "Rules:\n"
        "  - Output PHASE: markers exactly as shown — the governance system parses them\n"
        "  - FIX_TYPE must match CLAIMS from STEP 2\n"
        "  - PRINCIPALS must include ALL required for your type, none of the forbidden"
    )

    fail_to_pass = instance.get("FAIL_TO_PASS", [])
    if fail_to_pass:
        tests_str = "\n".join(f"  - {t}" for t in fail_to_pass[:10])
        extra_parts.append(
            f"IMPORTANT: Your fix must make the following tests pass:\n{tests_str}\n\n"
            f"Run the failing tests FIRST to understand what they expect. "
            f"Fix only the minimal code needed to make the tests pass. "
            f"SUBMIT IMMEDIATELY once these tests pass — do NOT add extra tests, "
            f"demonstration scripts, or comment updates. "
            f"Every step matters — go straight to submission as soon as the required tests pass."
        )
    if previous_failure:
        extra_parts.append(f"Previous attempt failed: {previous_failure[:300]}")
    if extra_parts:
        # Append directly to instance_template — instance_template_extra is NOT a recognized
        # AgentConfig field and would never be rendered. Direct append is the only correct path.
        config["agent"]["instance_template"] = (
            config["agent"]["instance_template"] + "\n\n" + "\n\n".join(extra_parts)
        )
    t_cfg.stop()

    print(f"    [agent] running {instance_id} attempt={attempt}...")

    from minisweagent.run.benchmarks.swebench import RunBatchProgressManager

    preds_path = attempt_dir / "preds.json"
    progress = RunBatchProgressManager(num_instances=1)

    # Install step monitor: logs steps + triggers inner-loop verify on patch writes
    # B2-CP: cp_state_holder (from run_with_jingu) passed through so step signals
    # update the cross-attempt cp_state directly.
    _monitor = _install_step_monitor(instance_id, attempt, instance, cp_state_holder=cp_state_holder, mode=mode)

    # Hook DefaultAgent.run() to:
    # 1. Inject container_id into _monitor as soon as container is started
    # 2. Run a final controlled_verify BEFORE env cleanup (end-of-attempt signal)
    from minisweagent.agents.default import DefaultAgent as _DA
    _orig_run = _DA.run

    def _verifying_run(self_agent, *args, **kwargs):
        # Inject container_id so step monitor can start verifying mid-run
        cid = getattr(getattr(self_agent, 'env', None), 'container_id', None)
        if cid and not _monitor.container_id:
            _monitor.container_id = cid
            print(f"    [inner-verify] container ready: {cid[:12]}...", flush=True)

        result = _orig_run(self_agent, *args, **kwargs)

        # Final controlled_verify at end-of-attempt (before container destroyed)
        # Uses the actual submission patch — most accurate final signal
        cid = getattr(getattr(self_agent, 'env', None), 'container_id', None)
        if not cid:
            return result
        submitted = result.get("submission", "") if isinstance(result, dict) else ""
        if not submitted:
            return result
        print(f"    [controlled-verify] final verify on container {cid[:12]}...", flush=True)

        # p187: cognition gate — check declaration quality before controlled_verify
        # Fires when cp_state.phase == "JUDGE" (EXECUTE->JUDGE advance by verdict routing).
        # Pass  → continue to controlled_verify as normal.
        # Fail  → inject feedback as pending_redirect_hint, skip controlled_verify.
        _cg_result_str: str | None = None
        if cp_state_holder is not None and cp_state_holder[0].phase == "JUDGE":
            _cg_decl = {}
            try:
                _cg_msgs = getattr(self_agent, "messages", [])
                _cg_last = extract_last_agent_message(_cg_msgs)
                _cg_decl = extract_declaration(_cg_last) if _cg_last else {}
            except Exception:
                pass
            _cg_signals = extract_patch_signals(submitted) if submitted else []
            from cognition_check import check_cognition_at_judge as _cg_judge
            _cg_pass, _cg_feedback = _cg_judge(_cg_decl, _cg_signals)
            _cg_result_str = "pass" if _cg_pass else "fail"
            print(f"    [cognition_gate] phase=JUDGE result={_cg_result_str}", flush=True)
            if not _cg_pass:
                # Inject feedback as redirect hint — agent receives it on next attempt
                _monitor.pending_redirect_hint = f"[COGNITION_FAIL] {_cg_feedback}"
                print(
                    f"    [cognition_gate] skipping controlled_verify — feedback injected",
                    flush=True,
                )

        # p191: in-loop judge — patch format + semantic weakening checks
        # Runs after cognition gate, before controlled_verify.
        # Hard checks (block): patch_non_empty, patch_format, no_semantic_weakening.
        # Soft check (warn only): changed_file_relevant.
        # Exception-safe: judge failure never crashes main flow.
        _judge_result = None
        try:
            from in_loop_judge import run_in_loop_judge as _run_ilj
            _judge_result = _run_ilj(submitted)
            print(
                f"    [in_loop_judge] "
                f"patch_non_empty={'pass' if _judge_result.patch_non_empty else 'fail'} "
                f"patch_format={'pass' if _judge_result.patch_format else 'fail'} "
                f"semantic_weakening={'pass' if _judge_result.no_semantic_weakening else 'fail'} "
                f"changed_file_relevant={'pass' if _judge_result.changed_file_relevant else 'warn'}",
                flush=True,
            )
            if _judge_result.all_pass and not _judge_result.changed_file_relevant:
                # Soft check — warn only, do not block
                print(
                    f"    [in_loop_judge] warn: changed_file_relevant=fail "
                    f"(soft check — controlled_verify continues)",
                    flush=True,
                )
            if not _judge_result.all_pass:
                # Hard check failures — set redirect hints (controlled_verify gated below)
                if not _judge_result.patch_non_empty:
                    _monitor.early_stop_verdict = VerdictStop(reason="empty_patch")
                elif not _judge_result.patch_format:
                    _monitor.pending_redirect_hint = "[REDIRECT:EXECUTE] patch_format_error"
                elif not _judge_result.no_semantic_weakening:
                    _monitor.pending_redirect_hint = "[REDIRECT:ANALYZE] semantic_weakening_detected"
                print(
                    f"    [in_loop_judge] skipping controlled_verify (hard check failed)",
                    flush=True,
                )
        except Exception as _ilj_exc:
            print(f"    [in_loop_judge] error (non-fatal): {_ilj_exc}", flush=True)

        # p192: unified prerequisite gate — aggregates cognition + judge results
        _prereq_pass, _prereq_reason = _verify_prerequisites(
            cognition_result=_cg_result_str,
            judge_result=_judge_result,
        )
        print(
            f"    [verify_gate] prerequisite={'pass' if _prereq_pass else f'fail({_prereq_reason})'} "
            f"controlled_verify={'run' if _prereq_pass else 'skipped'}",
            flush=True,
        )

        if not _prereq_pass:
            _monitor._verify_skipped = True
            _monitor._verify_skip_reason = _prereq_reason
            return result

        t_cv0 = time.monotonic()
        cv_result = run_controlled_verify(submitted, instance, cid, timeout_s=60)
        cv_result["elapsed_ms"] = round((time.monotonic() - t_cv0) * 1000, 1)
        # Store as last verify_history entry (step=-1 means end-of-attempt)
        _monitor.record_verify(-1, cv_result)
        return result

    _DA.run = _verifying_run

    t_llm = Timer("LLM agent loop (Bedrock)", parent=t_agent)
    try:
        process_instance(instance, attempt_dir, config, progress)
    except Exception as e:
        print(f"    [agent] ERROR: {e}")
        traceback.print_exc()
    finally:
        _DA.run = _orig_run  # always restore
    t_llm.stop()

    # Parse traj for usage + submission
    traj_path = attempt_dir / instance_id / f"{instance_id}.traj.json"
    usage = ModelUsage(instance_id, attempt)
    usage.load_from_traj(traj_path)
    _usage_tracker.record(usage)

    sub_from_traj = None
    sub_from_traj_diff = None  # fallback: last valid git diff in tool outputs
    exit_status = None
    jingu_body = None
    if traj_path.exists():
        try:
            traj = json.loads(traj_path.read_text())
            sub_from_traj = traj.get("info", {}).get("submission", "")
            exit_status = traj.get("info", {}).get("exit_status", "")
            # Fallback: if agent hit LimitsExceeded without calling submit,
            # extract the last valid git diff from tool output messages.
            if not sub_from_traj:
                for m in reversed(traj.get("messages", [])):
                    if m.get("role") != "tool":
                        continue
                    content = str(m.get("content", ""))
                    output_match = re.search(r"<output>(.*?)</output>", content, re.DOTALL)
                    if not output_match:
                        continue
                    output = output_match.group(1).strip()
                    if (output.startswith("diff --git")
                            and re.search(r"^---", output, re.MULTILINE)
                            and re.search(r"^\+\+\+", output, re.MULTILINE)
                            and re.search(r"^@@", output, re.MULTILINE)):
                        sub_from_traj_diff = output
                        print(f"    [agent] fallback: extracted git diff from traj "
                              f"({len(output)} chars)")
                        break
            # Build jingu_body from traj (deterministic, no LLM call)
            patch_for_body = sub_from_traj or sub_from_traj_diff or ""
            problem_stmt = instance.get("problem_statement", "")
            jingu_body = extract_jingu_body(traj, patch_for_body, problem_stmt)
            # Merge verify signal from _monitor into jingu_body.
            # Priority: final verify (step=-1) > last inner-loop verify > nothing.
            # verify_history[-1] is the end-of-attempt verify (most accurate).
            _final_cv = None
            if _monitor.verify_history:
                # Use the last controlled_fail_to_pass result (final verify or last mid-run)
                for _vh in reversed(_monitor.verify_history):
                    if _vh["kind"] == "controlled_fail_to_pass":
                        _final_cv = _vh
                        break
            if _final_cv:
                cv_flat = {
                    "verification_kind": _final_cv["kind"],
                    "tests_passed": _final_cv["tests_passed"],
                    "tests_failed": _final_cv["tests_failed"],
                    "exit_code": _final_cv["exit_code"],
                    "elapsed_ms": _final_cv["elapsed_ms"],
                    "step": _final_cv["step"],
                }
                jingu_body["controlled_verify"] = cv_flat
                jingu_body["test_results"]["ran_tests"] = True
                jingu_body["test_results"]["controlled_passed"] = _final_cv["tests_passed"]
                jingu_body["test_results"]["controlled_failed"] = _final_cv["tests_failed"]
                jingu_body["test_results"]["controlled_exit_code"] = _final_cv["exit_code"]
            # Store full verify_history for observability
            jingu_body["verify_history"] = _monitor.verify_history
            # p190: per-phase records — one entry per VerdictAdvance during this attempt
            jingu_body["phase_records"] = [r.as_dict() for r in _monitor.phase_records]
            # p195: principal inference telemetry — rich result with signals/explanation
            try:
                from principal_inference import run_inference, diff_principals
                from subtype_contracts import _PHASE_TO_SUBTYPE
                _pi_telemetry = []
                for _telem_pr in _monitor.phase_records:
                    _telem_phase = str(getattr(_telem_pr, "phase", ""))
                    _telem_subtype = _PHASE_TO_SUBTYPE.get(_telem_phase.upper(), "")
                    _telem_rich = run_inference(_telem_pr, _telem_subtype)
                    _telem_diff = diff_principals(
                        getattr(_telem_pr, "principals", []) or [],
                        _telem_rich,
                        phase=_telem_phase,
                    )
                    _telem_details = {
                        p: {
                            "score": round(r.score, 2),
                            "signals": r.signals,
                            "explanation": r.explanation,
                        }
                        for p, r in _telem_rich.details.items()
                    }
                    _pi_telemetry.append({
                        "phase": _telem_phase,
                        "subtype": _telem_subtype,
                        "declared": list(getattr(_telem_pr, "principals", []) or []),
                        "inferred": {
                            "present": _telem_rich.present,
                            "absent": _telem_rich.absent,
                        },
                        "details": _telem_details,
                        "diff": {
                            "missing_required": _telem_diff.get("missing_required", []),
                            "missing_expected": _telem_diff.get("missing_expected", []),
                            "fake": _telem_diff.get("fake", []),
                        },
                    })
                jingu_body["principal_inference"] = _pi_telemetry
            except Exception:
                pass
            # p192: verify_skipped — distinct from controlled_verify fail
            # Only set when prereq gate blocked controlled_verify from running.
            if getattr(_monitor, "_verify_skipped", False):
                jingu_body["verify_skipped"] = True
                jingu_body["verify_skip_reason"] = getattr(_monitor, "_verify_skip_reason", "unknown")
                jingu_body["controlled_verify_result"] = "skipped"
            # Write jingu_body back into traj.json so gate_runner.js can read it
            traj["jingu_body"] = jingu_body
            traj_path.write_text(json.dumps(traj, indent=2))
            cv_summary = ""
            if _final_cv:
                cv_summary = (f" cv_kind={_final_cv['kind']}"
                              f" cv_passed={_final_cv['tests_passed']}"
                              f" cv_failed={_final_cv['tests_failed']}"
                              f" cv_step={_final_cv['step']}")
            print(f"    [jingu_body] extracted: exit={jingu_body['exit_status']} "
                  f"files_written={len(jingu_body['files_written'])} "
                  f"tests_ran={jingu_body['test_results']['ran_tests']} "
                  f"patch_hunks={jingu_body['patch_summary']['hunks']}"
                  f"{cv_summary}")
        except (json.JSONDecodeError, OSError):
            pass

    t_agent.llm_calls = usage.api_calls  # stash for timing tree
    avg_s = t_llm.elapsed / usage.api_calls if usage.api_calls else 0
    print(f"    [agent] LLM loop done in {t_llm.elapsed:.1f}s  "
          f"bedrock_calls={usage.api_calls}  avg={avg_s:.1f}s/call  "
          f"tokens={usage.input_tokens}in/{usage.output_tokens}out  "
          f"cost=${usage.cost_usd:.4f}")

    t_agent.stop()

    # Read submission from preds.json
    if preds_path.exists():
        preds = json.loads(preds_path.read_text())
        if instance_id in preds:
            sub = preds[instance_id].get("model_patch", "")
            if sub:
                return sub, exit_status, jingu_body

    if sub_from_traj:
        return sub_from_traj, exit_status, jingu_body

    if sub_from_traj_diff:
        return sub_from_traj_diff, exit_status, jingu_body

    return None, exit_status, jingu_body

# ── Main loop ─────────────────────────────────────────────────────────────────

def run_with_jingu(instance_id: str, output_dir: Path, max_attempts: int = 3,
                   mode: str = "jingu") -> dict:
    """Run agent + Jingu gate with retry. Returns best result.

    mode="jingu"    — full pipeline: B1 gate + B3 structured retry (default)
    mode="baseline" — no gate, no structured retry; attempt 2 gets no hint (truly naive)
    """
    t_inst = Timer(f"instance: {instance_id}", parent=_timing_root)
    _instance_timers[instance_id] = t_inst

    print(f"  [jingu] loading instance {instance_id}...")
    t_load = Timer("dataset load", parent=t_inst)
    instance = _load_instance(instance_id)
    t_load.stop()

    # ONBOARDING_FIRST: verify official harness path is known before any execution
    _ok, _reason = _check_onboarding(instance)
    if not _ok:
        print(f"[onboarding-check] FAIL: {_reason}")
        return {
            "instance_id": instance_id,
            "status": "rejected",
            "failure_type": "ONBOARDING_REQUIRED",
            "reason": _reason,
            "patch": "",
            "accepted": False,
        }
    print("[onboarding-check] PASS")
    _print_execution_model(_build_execution_model(instance))


    candidates = []
    attempts_log: list[dict] = []   # telemetry: one entry per attempt
    last_failure = ""
    total_llm_calls = 0
    # p178: per-attempt strategy metadata (populated when retry_controller runs)
    _strategy_entries: list[dict] = []
    # p179: track test counts per attempt for delta computation
    _test_counts_by_attempt: dict[int, int] = {}  # attempt → passed count (-1 if unknown)
    # B2-CP: reasoning control plane state — one per instance, persists across attempts.
    # Wrapped in list so step monitor (inside run_agent) can update it via closure.
    # Step signals (B2) update cp_state_holder[0] on every step.
    # Verify signals (B1) are applied at attempt boundary below.
    cp_state_holder: list = [initial_reasoning_state("OBSERVE")]

    for attempt in range(1, max_attempts + 1):
        print(f"  [attempt {attempt}/{max_attempts}] {instance_id}")

        # NBR enforcement: No Blind Retry — attempt N+1 must have concrete failure signal
        # Bypass in baseline mode: naive retry intentionally has no hint.
        if attempt > 1 and not last_failure.strip() and mode != "baseline":
            raise RuntimeError(
                f"[NBR violation] attempt {attempt} has empty last_failure. "
                "Execution feedback is required before retry. "
                "Check build_execution_feedback() and ensure tests_ran signal is captured."
            )

        patch, agent_exit, jingu_body = run_agent(instance, output_dir, attempt,
                                                  previous_failure=last_failure, parent_timer=t_inst,
                                                  mode=mode, cp_state_holder=cp_state_holder)

        # p186: check early_stop_verdict set by _monitored_step during the attempt.
        # VerdictStop(no_signal) replaces the steps_since_last_signal >= threshold path:
        #   decide_next() fires no_signal when no_progress_steps >= NO_PROGRESS_THRESHOLD.
        # VerdictStop(task_success) fires when task_success signal received.
        # Both cases break the attempt loop immediately — no gate, no retry needed.
        _monitor_ref = None
        # _install_step_monitor returns state; we need to reach it.
        # The state is captured in the _monitored_step closure inside run_agent.
        # We surface it via a module-level dict keyed by (instance_id, attempt).
        _early_stop_key = (instance_id, attempt)
        _early_stop_state = _STEP_MONITOR_STATES.get(_early_stop_key)
        if _early_stop_state is not None and _early_stop_state.early_stop_verdict is not None:
            _esv = _early_stop_state.early_stop_verdict
            print(
                f"  [cp] early_stop instance={instance_id} attempt={attempt}"
                f" reason={_esv.reason} — verdict-driven attempt termination",
                flush=True,
            )
            if _esv.reason == "no_signal":
                # Equivalent to steps_since_last_signal >= NO_SIGNAL_THRESHOLD path.
                # Preserve last_failure for NBR compliance if more attempts remain.
                last_failure = (
                    "Previous attempt stopped early: no progress signal detected "
                    "(control-plane verdict=STOP no_signal). "
                    "Change your approach entirely — avoid repeated reads without writing code."
                )
            # For task_success: controlled_verify confirmed pass, no retry needed.
            break

        # p179: record test counts for this attempt (used later for tests_delta)
        _test_counts_by_attempt[attempt] = extract_test_counts(jingu_body)

        # llm_calls are recorded in _usage_tracker; no separate accumulation needed

        t_gate = Timer(f"jingu gate attempt={attempt}", parent=t_inst)
        if not patch:
            print(f"    [gate] EMPTY — no submission (exit={agent_exit})")
            attempts_log.append({
                "attempt": attempt,
                "admission_reason": "no_patch",
                "patch_fp": None,
                "gate_reason_codes": [],
                "exit_status": agent_exit,
            })
            if agent_exit and "LimitsExceeded" in agent_exit:
                last_failure = (
                    "You ran out of steps before submitting. "
                    "SKIP all exploration and testing this time. "
                    "Go DIRECTLY to the fix: read the failing test, identify the exact line to change, "
                    "make the minimal edit, then call submit IMMEDIATELY."
                )
            else:
                last_failure = "No patch was generated"
            t_gate.stop()
            continue

        patch = normalize_patch(patch)

        if mode == "baseline":
            # Baseline: no gate, no structured retry — accept every patch as-is.
            score = score_patch(patch)
            fp = patch_fingerprint(patch)
            print(f"    [gate] BASELINE (no gate)  score={score:.0f}  lines={len(patch.splitlines())}")
            attempts_log.append({
                "attempt": attempt,
                "admission_reason": "baseline_no_gate",
                "patch_fp": fp,
                "gate_reason_codes": [],
                "exit_status": agent_exit,
            })
            candidates.append({"attempt": attempt, "patch": patch, "score": score,
                                "gate_code": "BASELINE_NO_GATE"})
            # Truly naive retry: no hint at all.
            # This is the control condition — isolates jingu's structured retry value.
            last_failure = ""
            agent_exit = None
        elif GATE_MODE == "trust_gate":
            # B1: run jingu-trust-gate via subprocess
            attempt_dir = output_dir / f"attempt_{attempt}"
            traj_path = attempt_dir / instance_id / f"{instance_id}.traj.json"
            gate_result = evaluate_patch_from_traj(
                patch_text=patch,
                traj_path=traj_path if traj_path.exists() else None,
                exit_status=agent_exit,
                proposal_id=f"{instance_id}-attempt-{attempt}",
                jingu_body=jingu_body,
            )
            exp = gate_result.explanation
            exp_str = (f"units={exp.total_units} approved={exp.approved} "
                       f"downgraded={exp.downgraded} rejected={exp.rejected}"
                       if exp else "no explanation")
            admission = classify_admission(gate_result, patch, agent_exit)
            fp = patch_fingerprint(patch)
            attempts_log.append({
                "attempt": attempt,
                "admission_reason": admission,
                "patch_fp": fp,
                "gate_reason_codes": gate_result.reason_codes,
                "exit_status": agent_exit,
            })
            if gate_result.admitted:
                score = score_patch(patch)
                patch_lines = len(patch.splitlines())
                grade = gate_result.gate_code  # ADMITTED or ADMITTED_SPECULATIVE
                print(f"    [gate] {grade}  score={score:.0f}  lines={patch_lines}  {exp_str}")
                print(f"    [telemetry] admission={admission}  files={fp['files']}  "
                      f"hunks={fp['hunks']}  +{fp['lines_added']}/-{fp['lines_removed']}")
                t_gate.stop()

                candidates.append({
                    "attempt": attempt,
                    "patch": patch,
                    "score": score,
                    "gate_code": gate_result.gate_code,
                    "gate_reason_codes": gate_result.reason_codes,
                })
                # Patch bloat detection: warn if attempt 2 is much larger than attempt 1
                if attempt >= 2 and len(attempts_log) >= 2:
                    prev = attempts_log[-2].get("patch_fp", {})
                    prev_size = prev.get("lines_added", 0) + prev.get("lines_removed", 0)
                    curr_size = fp["lines_added"] + fp["lines_removed"]
                    if prev_size > 0 and curr_size > prev_size * 1.5:
                        print(f"    [bloat-warn] attempt {attempt} patch is {curr_size} lines "
                              f"(+{curr_size - prev_size} vs attempt {attempt-1} {prev_size}). "
                              f"Possible wrong direction.")
                # B3: retry-controller — diagnose attempt N, guide attempt N+1
                if attempt < max_attempts:
                    fail_to_pass = instance.get("FAIL_TO_PASS", [])
                    if not isinstance(fail_to_pass, list):
                        fail_to_pass = []
                    # Phase 2A: deterministic execution feedback (always runs)
                    exec_feedback = build_execution_feedback(
                        jingu_body=jingu_body or {},
                        fail_to_pass_tests=fail_to_pass,
                        patch_fp=fp,
                    )
                    print(f"    [exec-feedback] {exec_feedback[:200]}")
                    # EFR enforcement: Execution Feedback Required
                    tests_ran = (jingu_body or {}).get("test_results", {}).get("ran_tests", False)
                    if tests_ran and not exec_feedback.strip():
                        raise RuntimeError(
                            "[EFR violation] tests ran but exec_feedback is empty. "
                            "build_execution_feedback() must extract test output."
                        )
                    # B4: cognition gate — check declaration consistency with patch
                    # Additive: enriches exec_feedback when contradiction detected.
                    # Opt-in: no FIX_TYPE declaration → check skipped silently.
                    _traj_path = output_dir / f"attempt_{attempt}" / instance_id / f"{instance_id}.traj.json"
                    _decl = None
                    _traj_msgs_for_signal: list[dict] = []
                    if _traj_path.exists():
                        try:
                            _traj_msgs_for_signal = json.loads(_traj_path.read_text()).get("messages", [])
                            _last_msg = extract_last_agent_message(_traj_msgs_for_signal)
                            _decl = extract_declaration(_last_msg)
                            if _decl:
                                _signals = extract_patch_signals(patch)
                                _cog = check_cognition(_decl, _signals)
                                _cog_fb = format_cognition_feedback(_cog)
                                if _cog_fb:
                                    print(f"    [cognition] violation: {_cog_fb[:200]}")
                                    exec_feedback = exec_feedback + "\n" + _cog_fb if exec_feedback else _cog_fb
                                else:
                                    print(f"    [cognition] pass  type={_decl['type']}  signals={_signals}")
                            else:
                                print(f"    [cognition] skip  (no FIX_TYPE declaration)")
                        except (json.JSONDecodeError, OSError):
                            pass
                    # p164 runner layer: no-signal streak detection
                    _steps_since_signal = compute_steps_since_last_signal(_traj_msgs_for_signal)
                    if _steps_since_signal > 0:
                        print(f"    [no-signal] steps_since_last_signal={_steps_since_signal}")
                    # p175/p176: enforced-principal violation codes (Python-side)
                    _principal_viol_codes = extract_principal_violation_codes(_decl)
                    if _principal_viol_codes:
                        print(f"    [principal-viol] {_principal_viol_codes}")
                    if RETRY_CONTROLLER_ENABLED:
                        # Phase 2B: retry-controller builds on execution feedback + p177/p179 extensions
                        # prev_patch_fp: fingerprint of the attempt before this one
                        prev_fp = attempts_log[-2]["patch_fp"] if len(attempts_log) >= 2 else None
                        # p179: compute tests_delta before build_retry_plan (used in classify_failure_v2)
                        _tests_now = _test_counts_by_attempt.get(attempt, -1)
                        _tests_prev = _test_counts_by_attempt.get(attempt - 1, -1)
                        # Three-state delta: None when baseline unknown (prevents false "no_progress")
                        _tests_delta = (_tests_now - _tests_prev) if _tests_now >= 0 and _tests_prev >= 0 else None
                        # p179 gate: TEST_PROGRESS_MONOTONICITY invariant
                        _progress_ok, _progress_code = check_test_progress_invariant(_tests_prev, _tests_now)
                        print(f"    [test-progress] ok={_progress_ok}  code={_progress_code}  "
                              f"prev={_tests_prev}  now={_tests_now}  delta={_tests_delta}")
                        t_ctrl = Timer(f"B3 retry-controller attempt={attempt}", parent=t_inst)
                        retry_plan = build_retry_plan(
                            problem_statement=instance.get("problem_statement", ""),
                            patch_text=patch,
                            jingu_body=jingu_body or {},
                            fail_to_pass_tests=fail_to_pass,
                            gate_admitted=True,
                            gate_reason_codes=gate_result.reason_codes,
                            instance_id=instance_id,
                            patch_fp=fp,
                            prev_patch_fp=prev_fp,
                            exec_feedback=exec_feedback,
                            attempt=attempt,
                            steps_since_last_signal=_steps_since_signal,
                            principal_violation_codes=_principal_viol_codes,
                            strategy_table_path=STRATEGY_TABLE_PATH,
                            tests_delta=_tests_delta,
                            tests_passed_after=_tests_now,
                        )
                        t_ctrl.stop()
                        # p179: override control_action based on TEST_PROGRESS_MONOTONICITY
                        # Invariant violation overrides retry-controller's decision:
                        #   TEST_REGRESSION → STOP_FAIL (cannot continue if tests got worse)
                        #   NO_TEST_PROGRESS → ADJUST (force different strategy)
                        if not _progress_ok and _progress_code == "TEST_REGRESSION":
                            print(f"    [test-progress-gate] REGRESSION detected — overriding to STOP_FAIL")
                            retry_plan = RetryPlan(
                                root_causes=retry_plan.root_causes + [f"invariant=TEST_REGRESSION"],
                                must_do=["Revert the direction of your fix — you made tests worse"],
                                must_not_do=["Do not continue in the same direction as the previous attempt"],
                                validation_requirement="Run required tests and confirm delta > 0",
                                next_attempt_prompt=(
                                    "REGRESSION: Your previous patch made the tests worse. "
                                    "You must completely change your approach. "
                                    "Do NOT expand the previous change. "
                                    "Reread the failing tests from scratch and fix the actual root cause."
                                )[:600],
                                control_action="STOP_FAIL",
                                principal_violations=retry_plan.principal_violations,
                            )
                        elif not _progress_ok and _progress_code == "NO_TEST_PROGRESS":
                            # Ensure ADJUST — don't let unknown classification leave it at CONTINUE
                            if retry_plan.control_action == "CONTINUE":
                                print(f"    [test-progress-gate] NO_PROGRESS — upgrading CONTINUE → ADJUST")
                                retry_plan = RetryPlan(
                                    root_causes=retry_plan.root_causes + [f"invariant=NO_TEST_PROGRESS"],
                                    must_do=retry_plan.must_do,
                                    must_not_do=retry_plan.must_not_do,
                                    validation_requirement=retry_plan.validation_requirement,
                                    next_attempt_prompt=retry_plan.next_attempt_prompt,
                                    control_action="ADJUST",
                                    principal_violations=retry_plan.principal_violations,
                                )
                        print(f"    [retry-ctrl] action={retry_plan.control_action}  "
                              f"root_causes={retry_plan.root_causes}")
                        print(f"    [retry-ctrl] must_not_do={retry_plan.must_not_do}")
                        print(f"    [retry-ctrl] hint={retry_plan.next_attempt_prompt[:200]}")
                        # Store strategy metadata for p178/p179 logging
                        _strategy_failure_class = next(
                            (rc.split("=", 1)[1] for rc in retry_plan.root_causes if rc.startswith("failure_type=") and not rc.startswith("failure_type_v2=")),
                            "unknown",
                        )
                        _strategy_failure_class_v2 = next(
                            (rc.split("=", 1)[1] for rc in retry_plan.root_causes if rc.startswith("failure_type_v2=")),
                            "signal_missing",
                        )
                        _strategy_entries.append({
                            "attempt": attempt,
                            "failure_class": _strategy_failure_class,
                            "failure_class_v2": _strategy_failure_class_v2,
                            "control_action": retry_plan.control_action,
                            "steps_since_signal": _steps_since_signal,
                            "enforced_violations": retry_plan.principal_violations,
                            "hint_used": retry_plan.next_attempt_prompt[:300],
                            # p179 signal fields
                            "tests_passed_count": _tests_now,
                            "tests_passed_prev": _tests_prev,
                            "tests_delta": _tests_delta,
                            "progress_code": _progress_code,
                            "files_written_paths": (jingu_body or {}).get("files_written", []),
                        })
                        # B3-CP: update reasoning state with verify result FIRST (before any break)
                        # B3.2: verify-window IS the stagnation gate — update_stagnation=True (default).
                        # Step-level signals (B2) updated env_noise/actionability but NOT no_progress.
                        # Here we apply verify signal (B1) with stagnation update + task_success.
                        # Two separate calls enforced (CORR1: signal separation):
                        #   call 1 (step-level, B2): update_stagnation=False
                        #   call 2 (verify-level, here): update_stagnation=True (default)
                        _cv_passed = (_strategy_failure_class_v2 == "verified_pass")
                        _verify_partial = extract_verify_signals(controlled_verify_passed=_cv_passed)
                        cp_state_holder[0] = update_reasoning_state(
                            cp_state_holder[0], normalize_signals(_verify_partial)
                            # update_stagnation=True (default) — verify window advances stagnation
                        )
                        _cp_state_now = cp_state_holder[0]
                        cp_verdict = decide_next(_cp_state_now)
                        # B3.1: add instance + attempt to control-plane logs
                        _iid_short = instance_id.split("__")[-1] if "__" in instance_id else instance_id
                        print(f"    [control-plane] instance={_iid_short} attempt={attempt}"
                              f" state=phase:{_cp_state_now.phase}"
                              f" step:{_cp_state_now.step_index} no_progress:{_cp_state_now.no_progress_steps}"
                              f" task_success:{_cp_state_now.task_success}")
                        print(f"    [control-plane] instance={_iid_short} attempt={attempt} verdict={cp_verdict}")
                        if isinstance(cp_verdict, VerdictStop):
                            print(f"    [control-plane] instance={_iid_short} STOPPING — reason={cp_verdict.reason}")
                            break
                        if isinstance(cp_verdict, VerdictRedirect):
                            # Unconditional override (CORR3): REDIRECT always forces ADJUST
                            print(f"    [control-plane] instance={_iid_short} REDIRECT → forcing ADJUST  reason={cp_verdict.reason}")
                            import dataclasses as _dc
                            retry_plan = _dc.replace(
                                retry_plan,
                                control_action="ADJUST",
                                next_attempt_prompt=(
                                    retry_plan.next_attempt_prompt
                                    + f"\n\n[Control-plane redirect: {cp_verdict.reason} — re-examine environment assumptions before patching]"
                                ),
                            )

                        # Honor control_action: stop when verify passed or no signal
                        if retry_plan.control_action in ("STOP_FAIL", "STOP_NO_SIGNAL"):
                            print(f"    [retry-ctrl] STOPPING — action={retry_plan.control_action}")
                            break
                        # verified_pass: controlled_verify confirmed all tests pass — no retry needed
                        # (kept as fallback; VerdictStop(task_success) above is the primary path)
                        if _strategy_failure_class_v2 == "verified_pass":
                            print(f"    [retry-ctrl] STOPPING — verified_pass (controlled_verify tests_failed=0)")
                            break

                        # next_attempt_prompt already merges hint_prefix + exec_feedback
                        last_failure = retry_plan.next_attempt_prompt[:600]
                    else:
                        last_failure = exec_feedback[:400]
                else:
                    last_failure = ""
                agent_exit = None
            else:
                codes = ", ".join(gate_result.reason_codes)
                print(f"    [gate] REJECTED  codes={codes}  {exp_str}")
                if gate_result.error:
                    print(f"    [gate-error] {gate_result.error[:300]}")
                print(f"    [telemetry] admission={admission}  files={fp['files']}  "
                      f"hunks={fp['hunks']}  +{fp['lines_added']}/-{fp['lines_removed']}")
                # Use gate's retry feedback as next attempt hint
                hint = gate_result.retry_hint
                if not hint:
                    if "APPLY_FAILED" in gate_result.reason_codes:
                        hint = ("Previous patch failed to apply. Check for merge conflicts "
                                "or incorrect line numbers. Generate a clean diff.")
                    elif "PARSE_FAILED" in gate_result.reason_codes:
                        hint = ("Previous patch was malformed (missing ---, +++, @@ markers). "
                                "Use git diff format exactly.")
                    else:
                        hint = f"Gate rejected patch ({codes}). Generate a better patch."
                last_failure = hint[:400]
                t_gate.stop()
                continue
        else:
            # B0 fallback: structural check only
            sg = jingu_structural_check(patch)
            if not sg["pass"]:
                print(f"    [gate] FAIL structural: {sg['code']} — {sg.get('message','')}")
                last_failure = f"Structural gate failed: {sg['message']}"
                t_gate.stop()
                continue
            score = score_patch(patch)
            patch_lines = len(patch.splitlines())
            print(f"    [gate] OK  score={score:.0f}  lines={patch_lines}")
            t_gate.stop()
            candidates.append({"attempt": attempt, "patch": patch, "score": score,
                                "gate_code": "STRUCTURAL_OK"})
            last_failure = ""
            agent_exit = None

    t_inst.stop()

    inst_usage = _usage_tracker.per_instance().get(instance_id, {})
    llm_calls = inst_usage.get("api_calls", 0)
    t_inst.llm_calls = llm_calls

    delta = compute_attempt_delta(attempts_log)
    if delta:
        print(f"  [attempt_delta] files_changed={delta['files_changed']}  "
              f"size_delta={delta['size_delta_lines']:+d}  "
              f"same_reason={delta['same_admission_reason']}  "
              f"{delta['a1_admission']} → {delta['a2_admission']}")

    # ── p178.1 / p179: flush strategy log entries with retry-level reward ───
    # Primary reward: tests_delta (p179) — how many more tests passed in attempt N vs N-1
    # Secondary reward: next_attempt_admitted (did hint help attempt N+1 get admitted?)
    # Auxiliary: instance_final_admitted (did any attempt succeed?)
    if STRATEGY_LOG_PATH and _strategy_entries:
        _inst_final_admitted = bool(candidates)
        # Build a lookup: attempt number → admission result from attempts_log
        _admit_by_attempt = {
            a["attempt"]: a["admission_reason"] not in ("no_patch", "gate_reject_parse_failed",
                "gate_reject_apply_failed", "gate_reject_empty_patch",
                "gate_reject_too_many_files", "gate_reject_other", "gate_error")
            for a in attempts_log
        }
        _has_patch_by_attempt = {
            a["attempt"]: a["admission_reason"] != "no_patch"
            for a in attempts_log
        }
        for _se in _strategy_entries:
            _next_att = _se["attempt"] + 1
            _next_admitted = _admit_by_attempt.get(_next_att, False)
            _next_has_patch = _has_patch_by_attempt.get(_next_att, False)
            try:
                log_strategy_entry(
                    make_strategy_entry(
                        instance_id=instance_id,
                        attempt_id=_se["attempt"],
                        failure_class=_se["failure_class"],
                        control_action=_se["control_action"],
                        steps_since_last_signal=_se["steps_since_signal"],
                        enforced_violation_codes=_se["enforced_violations"],
                        hint_used=_se["hint_used"],
                        next_attempt_admitted=_next_admitted,
                        next_attempt_has_patch=_next_has_patch,
                        instance_final_admitted=_inst_final_admitted,
                        outcome="solved" if _inst_final_admitted else "unsolved",
                        tests_delta=_se.get("tests_delta", None),
                        tests_passed_before=_se.get("tests_passed_prev", -1),
                        tests_passed_after=_se.get("tests_passed_count", -1),
                        files_written_paths=_se.get("files_written_paths", []),
                        failure_class_v2=_se.get("failure_class_v2", "signal_missing"),
                    ),
                    STRATEGY_LOG_PATH,
                )
            except Exception as _log_err:
                print(f"    [strategy-log] WARNING: failed to write entry: {_log_err}")

    if not candidates:
        return {
            "instance_id": instance_id,
            "accepted": False,
            "patch": "",
            "attempts": max_attempts,
            "elapsed_s": t_inst.elapsed,
            "model_usage": inst_usage,
            "attempts_log": attempts_log,
            "attempt_delta": delta,
        }

    best = max(candidates, key=lambda c: c["score"])
    gate_code = best.get("gate_code", "ADMITTED")
    best_admission = next(
        (a["admission_reason"] for a in attempts_log if a["attempt"] == best["attempt"]),
        gate_code.lower(),
    )
    print(f"  [result] ACCEPTED  best_attempt={best['attempt']}  score={best['score']:.0f}  "
          f"gate={gate_code}  admission={best_admission}  elapsed={t_inst.elapsed:.1f}s  "
          f"bedrock_calls={llm_calls}  cost=${inst_usage.get('cost_usd', 0):.4f}")
    return {
        "instance_id": instance_id,
        "accepted": True,
        "patch": best["patch"],
        "attempts": max_attempts,
        "best_attempt": best["attempt"],
        "score": best["score"],
        "gate_code": gate_code,
        "gate_reason_codes": best.get("gate_reason_codes", []),
        "admission_reason": best_admission,
        "elapsed_s": t_inst.elapsed,
        "model_usage": inst_usage,
        "attempts_log": attempts_log,
        "attempt_delta": delta,
    }

def write_predictions(results: list, output_path: Path, mode: str = "jingu"):
    """Write predictions JSONL. Includes all instances (empty patch for unaccepted)."""
    model_name = "baseline-2shot" if mode == "baseline" else "mini-swe-agent+jingu"
    # Rewrite the file completely (deduplicates any incremental writes)
    with open(output_path, "w") as f:
        for r in results:
            if not r:
                continue
            # Always write an entry — empty patch counts as "no submission" in harness
            f.write(json.dumps({
                "instance_id": r["instance_id"],
                "model_patch": r["patch"] if r.get("accepted") else "",
                "model_name_or_path": model_name,
            }) + "\n")
    print(f"\n[predictions] written: {output_path}  model={model_name}")
    accepted = sum(1 for r in results if r and r.get("accepted"))
    print(f"[predictions] {accepted}/{len(results)} instances accepted")

def _run_official_evaluation(
    predictions_path: Path,
    instance_ids: list[str],
    run_id: str,
    eval_output_dir: Path,
    max_workers: int = 8,
    dataset: str = "Lite",
) -> dict:
    """
    Run the official SWE-bench harness (swebench.harness.run_evaluation).

    This is the ONLY authoritative resolved-rate source.
    controlled_verify is a mid-run signal only; this is the final verdict.

    Returns a dict with resolved_ids, unresolved_ids, resolved_rate.
    """
    import subprocess as _sp

    eval_output_dir.mkdir(parents=True, exist_ok=True)

    dataset_name = f"SWE-bench/SWE-bench_{dataset}"
    cmd = [
        "python", "-m", "swebench.harness.run_evaluation",
        "--dataset_name", dataset_name,
        "--split", "test",
        "--predictions_path", str(predictions_path),
        "--run_id", run_id,
        "--instance_ids", *instance_ids,
        "--max_workers", str(max_workers),
        "--cache_level", "env",
    ]
    print(f"\n[eval] running official harness: run_id={run_id}")
    print(f"[eval] cmd: {' '.join(cmd)}")

    t0 = time.monotonic()
    proc = _sp.run(cmd, capture_output=True, text=True, timeout=7200)
    elapsed = time.monotonic() - t0

    if proc.returncode != 0:
        print(f"[eval] harness FAILED (exit={proc.returncode}) in {elapsed:.0f}s")
        print(f"[eval] stderr: {proc.stderr[-2000:]}")
        return {"error": proc.stderr[-500:], "elapsed_s": round(elapsed, 1)}

    print(f"[eval] harness completed in {elapsed:.0f}s")
    if proc.stdout:
        print(f"[eval] stdout tail:\n{proc.stdout[-1000:]}")

    # Parse results file produced by run_evaluation
    # run_evaluation writes: logs/<run_id>.<dataset>.<split>.json
    # Search for the result JSON
    result_file = None
    dataset_short = dataset_name.split("/")[-1]  # e.g. SWE-bench_Lite or SWE-bench_Verified
    for candidate in [
        Path(f"logs/{run_id}.SWE-bench_{dataset_short}.test.json"),
        Path(f"logs/{run_id}.{dataset_short}.test.json"),
        Path(f"logs/{run_id}.SWE-bench_SWE-bench_Lite.test.json"),
        Path(f"logs/{run_id}.SWE-bench_Lite.test.json"),
        Path(f"logs/{run_id}.json"),
    ]:
        if candidate.exists():
            result_file = candidate
            break

    if result_file is None:
        # Also check cwd variants
        import glob as _glob
        matches = _glob.glob(f"logs/{run_id}*.json") + _glob.glob(f"*{run_id}*.json")
        if matches:
            result_file = Path(matches[0])

    if result_file is None:
        print(f"[eval] WARNING: could not find result JSON for run_id={run_id}")
        print(f"[eval] stdout: {proc.stdout[-500:]}")
        return {"error": "result file not found", "elapsed_s": round(elapsed, 1)}

    try:
        raw = json.loads(result_file.read_text())
    except Exception as e:
        return {"error": f"parse error: {e}", "elapsed_s": round(elapsed, 1)}

    resolved_ids = raw.get("resolved_ids", [])
    all_ids = raw.get("submitted_ids", instance_ids)
    unresolved_ids = [i for i in all_ids if i not in resolved_ids]

    result = {
        "resolved_count": len(resolved_ids),
        "total": len(all_ids),
        "resolved_rate": round(len(resolved_ids) / len(all_ids), 4) if all_ids else 0.0,
        "resolved_ids": sorted(resolved_ids),
        "unresolved_ids": sorted(unresolved_ids),
        "elapsed_s": round(elapsed, 1),
        "result_file": str(result_file),
    }

    print(f"\n[eval] RESULT: resolved={result['resolved_count']}/{result['total']} "
          f"({result['resolved_rate']:.1%})")
    print(f"[eval] resolved: {result['resolved_ids']}")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance-ids", nargs="+", required=True)
    parser.add_argument("--max-attempts", type=int, default=1)
    parser.add_argument("--output", default="results/mini-swe-agent")
    parser.add_argument("--workers", type=int, default=4,
                        help="Parallel instances to run (default: 4)")
    parser.add_argument("--stagger", type=float, default=15.0,
                        help="Seconds between sandbox starts to avoid image-pull contention (default: 15)")
    parser.add_argument("--mode", choices=["jingu", "baseline"], default="jingu",
                        help="jingu=full pipeline (gate+retry); baseline=no gate, no hint (control condition)")
    parser.add_argument("--run-eval", action="store_true", default=False,
                        help="Run official SWE-bench harness after inference (requires Docker)")
    parser.add_argument("--run-id", default=None,
                        help="Run ID for eval results (default: auto-generated from mode+timestamp)")
    parser.add_argument("--dataset", choices=["Lite", "Verified"], default="Lite",
                        help="SWE-bench dataset variant: Lite (300) or Verified (500) (default: Lite)")
    args = parser.parse_args()

    global _timing_root
    _timing_root = Timer("total run")

    # RT4: print activation proof at startup so logs confirm what is live
    _identity = get_execution_identity()
    print_activation_proof(_identity)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Pre-load all instances in a single dataset pass (avoids N redundant downloads)
    print(f"[jingu] loading {len(args.instance_ids)} instances from dataset SWE-bench_{args.dataset}...")
    t_ds = Timer("dataset prefetch", parent=_timing_root)
    _load_instances(args.instance_ids, dataset=args.dataset)
    t_ds.stop()
    print(f"[jingu] loaded in {t_ds.elapsed:.1f}s. launching {args.workers} parallel workers...")

    t_parallel = Timer(f"parallel workers (×{min(args.workers, len(args.instance_ids))})", parent=_timing_root)
    results = [None] * len(args.instance_ids)

    # Auto run-id: mode + timestamp
    run_id = args.run_id or f"{args.mode}-{int(time.time())}"
    preds_filename = f"{args.mode}-predictions.jsonl"

    def _run(idx: int, iid: str):
        delay = idx * args.stagger
        if delay > 0:
            print(f"[jingu] {iid} waiting {delay:.0f}s before start (stagger)")
            time.sleep(delay)
        print(f"\n[jingu] START {iid}  mode={args.mode}")
        r = run_with_jingu(iid, output_dir, max_attempts=args.max_attempts, mode=args.mode)
        status = "ACCEPTED" if r["accepted"] else "FAILED"
        print(f"\n[jingu] {status} {iid}  ({r.get('elapsed_s', 0):.1f}s)")
        return idx, r

    preds_path = output_dir / preds_filename
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_run, i, iid): iid
                   for i, iid in enumerate(args.instance_ids)}
        done = 0
        for fut in as_completed(futures):
            done += 1
            iid = futures[fut]
            try:
                idx, r = fut.result()
                results[idx] = r
            except Exception as e:
                print(f"\n[jingu] ERROR {iid}: {e}")
                idx = args.instance_ids.index(iid)
                results[idx] = {"instance_id": iid, "accepted": False, "patch": "",
                                 "attempts": args.max_attempts, "elapsed_s": 0}
                r = results[idx]
            # Write incrementally: append accepted prediction immediately
            if r and r.get("accepted"):
                _model_name = "baseline-2shot" if args.mode == "baseline" else "mini-swe-agent+jingu"
                with open(preds_path, "a") as pf:
                    pf.write(json.dumps({
                        "instance_id": r["instance_id"],
                        "model_patch": r["patch"],
                        "model_name_or_path": _model_name,
                    }) + "\n")
                print(f"[predictions] saved {r['instance_id']} (incremental)")
            print(f"[progress] {done}/{len(args.instance_ids)} done")

    t_parallel.stop()

    t_write = Timer("write predictions", parent=_timing_root)
    write_predictions(results, preds_path, mode=args.mode)
    t_write.stop()

    _timing_root.stop()

    # ── Run Report ─────────────────────────────────────────────────────────────
    total     = _timing_root.elapsed
    totals    = _usage_tracker.totals()
    per_inst  = _usage_tracker.per_instance()
    max_elapsed = max((r.get("elapsed_s", 0) for r in results if r), default=1)
    seq_total = sum(r.get("elapsed_s", 0) for r in results if r)
    speedup   = seq_total / t_parallel.elapsed if t_parallel.elapsed > 0 else 1

    # ── Attempt-level metrics ───────────────────────────────────────────────────
    # attempt1_accepted: instances where best_attempt == 1
    # attempt2_rescued: accepted instances where best_attempt == 2 (failed attempt1)
    attempt1_accepted = sum(1 for r in results if r and r.get("accepted") and r.get("best_attempt", 1) == 1)
    attempt2_rescued  = sum(1 for r in results if r and r.get("accepted") and r.get("best_attempt", 1) == 2)
    # For baseline mode best_attempt may not be set (all accepted on first available attempt)
    # Fall back: count accepted with no best_attempt field as attempt1
    total_accepted = sum(1 for r in results if r and r.get("accepted"))

    # ── Failure breakdown (jingu mode only) ────────────────────────────────────
    # Collect failure_class_v2 from strategy log entries stored in results
    # These are attached to results that have a "strategy_entries" field if we add it.
    # For now, load from STRATEGY_LOG_PATH if available.
    failure_breakdown: dict[str, int] = {}
    if STRATEGY_LOG_PATH and Path(STRATEGY_LOG_PATH).exists() and args.mode == "jingu":
        try:
            from strategy_logger import load_strategy_log
            _log_entries = load_strategy_log(STRATEGY_LOG_PATH)
            # Only count entries from this batch (matching instance_ids)
            _batch_ids = set(args.instance_ids)
            for _e in _log_entries:
                if _e.instance_id in _batch_ids:
                    fc = getattr(_e, "failure_class_v2", "signal_missing") or "signal_missing"
                    failure_breakdown[fc] = failure_breakdown.get(fc, 0) + 1
        except Exception:
            pass

    report = {
        "mode":             args.mode,
        "run_id":           run_id,
        "instances":        len(args.instance_ids),
        "workers":          args.workers,
        "step_limit":       BASE_CONFIG["agent"].get("step_limit", None),
        "wall_time_s":      round(total, 1),
        "status":           "completed",
        "patches_generated": total_accepted,
        "attempt_stats": {
            "attempt1_accepted":  attempt1_accepted,
            "attempt2_rescued":   attempt2_rescued,
            "total_accepted":     total_accepted,
            # rescued_rate: of instances that had a 2nd attempt, how many were rescued
            "rescued_rate": round(attempt2_rescued / max(1, len(args.instance_ids) - attempt1_accepted), 4),
        },
        "failure_breakdown": failure_breakdown,  # jingu mode only; empty for baseline
        "execution_identity": _identity,
        "model_usage": {
            "total_api_calls":    totals["api_calls"],
            "total_input_tokens": totals["input_tokens"],
            "total_output_tokens":totals["output_tokens"],
            "total_cost_usd":     totals["cost_usd"],
            "avg_calls_per_instance": round(totals["api_calls"] / len(args.instance_ids), 1) if args.instance_ids else 0,
            "avg_cost_per_instance":  round(totals["cost_usd"] / len(args.instance_ids), 4) if args.instance_ids else 0,
            "per_instance": per_inst,
        },
        "parallelism": {
            "sequential_would_be_s": round(seq_total, 1),
            "actual_wall_s":         round(t_parallel.elapsed, 1),
            "speedup_x":             round(speedup, 1),
        },
        "eval_results": None,  # filled in below if --run-eval
    }

    # Save machine-readable report (initial write before eval)
    report_path = output_dir / "run_report.json"
    report_path.write_text(json.dumps(report, indent=2))

    # Print human-readable
    print(f"\n{'='*62}")
    print(f"  RUN REPORT")
    print(f"{'='*62}")
    print(f"  instances={report['instances']}  workers={report['workers']}  "
          f"step_limit={report['step_limit']}  wall={total:.1f}s")
    print()
    print(f"  ── MODEL USAGE (primary) ──")
    print(f"    total_api_calls    : {totals['api_calls']}")
    print(f"    total_input_tokens : {totals['input_tokens']:,}")
    print(f"    total_output_tokens: {totals['output_tokens']:,}")
    print(f"    total_cost_usd     : ${totals['cost_usd']:.4f}")
    print(f"    avg calls/instance : {report['model_usage']['avg_calls_per_instance']}")
    print(f"    avg cost/instance  : ${report['model_usage']['avg_cost_per_instance']:.4f}")
    print()
    print(f"  ── PER-INSTANCE ──")
    for r in results:
        if r is None:
            continue
        iid     = r["instance_id"]
        status  = "✓" if r["accepted"] else "✗"
        elapsed = r.get("elapsed_s", 0)
        u       = per_inst.get(iid, {})
        calls   = u.get("api_calls", 0)
        cost    = u.get("cost_usd", 0)
        avg_c   = elapsed / calls if calls else 0
        bar_w   = int(elapsed / max_elapsed * 20) if max_elapsed > 0 else 0
        print(f"    {status} {iid:35s}  calls={calls:3d}  cost=${cost:.3f}  "
              f"{elapsed:5.1f}s  avg={avg_c:.1f}s/call  {'█'*bar_w}")
    print()
    print(f"  ── ATTEMPT STATS ──")
    print(f"    attempt1 accepted  : {attempt1_accepted}/{len(args.instance_ids)}")
    print(f"    attempt2 rescued   : {attempt2_rescued}/{max(1, len(args.instance_ids) - attempt1_accepted)}")
    if failure_breakdown:
        print(f"  ── FAILURE BREAKDOWN (jingu) ──")
        for fc, cnt in sorted(failure_breakdown.items(), key=lambda x: -x[1]):
            print(f"    {fc:30s}: {cnt}")
    print()
    print(f"  ── TIMING ──")
    print(f"    dataset prefetch   : {t_ds.elapsed:.1f}s")
    print(f"    parallel workers   : {t_parallel.elapsed:.1f}s  ({t_parallel.elapsed/total:.0%} of total)")
    print(f"    parallelism gain   : {seq_total:.1f}s → {t_parallel.elapsed:.1f}s  (×{speedup:.1f})")
    print(f"    write predictions  : {t_write.elapsed:.1f}s")
    print()
    print(f"  report saved → {report_path}")

    # ── Official evaluation (optional) ─────────────────────────────────────────
    if args.run_eval:
        print(f"\n{'='*62}")
        print(f"  OFFICIAL EVALUATION  mode={args.mode}  run_id={run_id}")
        print(f"{'='*62}")
        eval_result = _run_official_evaluation(
            predictions_path=preds_path,
            instance_ids=args.instance_ids,
            run_id=run_id,
            eval_output_dir=output_dir / "eval_results",
            dataset=args.dataset,
        )
        report["eval_results"] = eval_result
        # Update run_report.json with eval results
        report_path.write_text(json.dumps(report, indent=2))
        print(f"  run_report updated with eval_results → {report_path}")

    print(f"{'='*62}\n")

if __name__ == "__main__":
    main()
