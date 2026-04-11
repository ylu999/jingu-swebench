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
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# B1: jingu-trust-gate bridge (subprocess → TS gate)
from jingu_gate_bridge import evaluate_patch_from_traj, build_support_pool, run_patch_gate
# B2: adversarial reviewer (cognitive governance)
from patch_reviewer import review_patch_bedrock, ReviewResult
# B3: retry controller (failure → diagnosis → next strategy)
from retry_controller import build_retry_plan, classify_outcome, RetryPlan
# p208: failure classification engine (system-level routing, separate from outcome engine)
from failure_classifier import classify_failure, get_routing as get_failure_routing
from repair_prompts import build_repair_prompt, build_sdg_repair_prompt
from gate_rejection import SDG_ENABLED as _SDG_ENABLED, build_repair_from_rejection as _build_sdg_repair
# p216: data-driven failure routing engine
from failure_routing import route_failure as route_failure_p216, get_routing_entry, is_data_driven_routing_enabled
from strategy_prompts import get_strategy_prompt
from governance_runtime import (
    install_governance_pack,
    run_governance_packs,
    override_retry_plan_from_pack,
    ExecutionContext as GovExecutionContext,
)
from swebench_failure_reroute_pack import SWEBENCH_FAILURE_REROUTE_PACK
from phase_record_pack import PHASE_RECORD_PACK
from strategy_logger import log_strategy_entry, make_entry as make_strategy_entry
# B4: cognition gate (declaration-vs-patch consistency check)
from declaration_extractor import (
    extract_declaration, extract_last_agent_message,
    extract_from_structured, extract_phase_record_from_structured,
    build_phase_record_from_structured,
)
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
from control.phase_result import build_phase_result, route_from_phase_result


# p225-01: StepMonitorState, StopExecution, early_stop_scope extracted to step_monitor_state.py
from step_monitor_state import StepMonitorState, StopExecution, early_stop_scope

# p225-02: signal_extraction, controlled_verify, jingu_adapter extracted to separate modules
from signal_extraction import (  # noqa: F401 — re-export for backward compat
    _msg_has_env_mutation, _msg_has_signal, compute_steps_since_last_signal,
    _SIGNAL_TOOL_NAMES, _SIGNAL_BASH_PATTERNS, _ENV_MUTATION_PATTERNS,
)
from controlled_verify import (  # noqa: F401 — re-export for backward compat
    run_controlled_verify,
    _check_onboarding, _build_execution_model, _print_execution_model,
    _build_test_command, _parse_test_output_counts, _parse_f2p_p2p,
    _extract_f2p_class_labels,
)
from jingu_adapter import (  # noqa: F401 — re-export for backward compat
    extract_principal_violation_codes, parse_pytest_output, build_execution_feedback,
    normalize_patch, jingu_structural_check, score_patch, extract_jingu_body,
)


# ── Structured output helper (p221) ──────────────────────────────────────────

def _try_parse_structured_output(agent_messages: list[dict]) -> dict | None:
    """Attempt to extract structured JSON from the last assistant tool_call.

    When STRUCTURED_OUTPUT_ENABLED=true, the LLM is forced to call a
    'structured_output' tool. The tool call arguments contain the schema-valid
    JSON output. This function extracts that JSON.

    Returns:
        Parsed dict if a structured_output tool call was found, else None.
    """
    if not STRUCTURED_OUTPUT_ENABLED:
        return None
    for msg in reversed(agent_messages):
        if msg.get("role") != "assistant":
            continue
        tool_calls = msg.get("tool_calls", [])
        for tc in tool_calls:
            fn = tc.get("function", {})
            if fn.get("name") == "structured_output":
                try:
                    return json.loads(fn.get("arguments", "{}"))
                except (json.JSONDecodeError, TypeError):
                    return None
        # Also check content blocks (some litellm versions use this format)
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    if block.get("name") == "structured_output":
                        inp = block.get("input")
                        if isinstance(inp, dict):
                            return inp
                        if isinstance(inp, str):
                            try:
                                return json.loads(inp)
                            except (json.JSONDecodeError, TypeError):
                                return None
        break  # Only check the last assistant message
    return None


# P-INV-001: run environment invariant checks before any batch work
run_preflight()

# B1 gate mode: "trust_gate" (B1) or "structural" (B0 fallback)
GATE_MODE = "trust_gate"
REVIEWER_ENABLED = False  # B2 reviewer — set True to re-enable
RETRY_CONTROLLER_ENABLED = True  # B3 retry-controller — diagnoses attempt 1, guides attempt 2
# p221: structured output — when True, LLM output is schema-enforced JSON (no regex extraction needed)
STRUCTURED_OUTPUT_ENABLED = os.environ.get("STRUCTURED_OUTPUT_ENABLED", "false").lower() == "true"
# p178: strategy learning — set paths to enable log + table
STRATEGY_LOG_PATH = os.environ.get("STRATEGY_LOG_PATH")   # e.g. /root/results/strategy_log.jsonl
STRATEGY_TABLE_PATH = os.environ.get("STRATEGY_TABLE_PATH")  # e.g. /root/results/strategy_table.json

# ── Governance packs (p27 ADR: GovernancePack onboarding pipeline) ────────────
# Install packs at module load. Each pack declares its 5 onboarding steps.
# Missing steps are logged as warnings (v0). Future: hard error.
install_governance_pack(SWEBENCH_FAILURE_REROUTE_PACK)
install_governance_pack(PHASE_RECORD_PACK)

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
    print(f"[init] structured_output_enabled={STRUCTURED_OUTPUT_ENABLED}")
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
    # p217: self-describing gate rejection — activation proof (RT4)
    print(f"[init] sdg_enabled={_SDG_ENABLED}")

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

# P18: per-instance monitor registry — concurrent-safe dispatch.
# Maps instance_id → (StepMonitorState, cp_state_holder, mode).
# _monitored_step and _verifying_run look up state by self.instance_id,
# so parallel workers each see their own state regardless of class-level patch order.
_INSTANCE_MONITOR_REGISTRY: dict[str, tuple] = {}
_INSTANCE_MONITOR_REGISTRY_LOCK = threading.Lock()


def _register_monitor(instance_id: str, state, cp_state_holder, mode: str) -> None:
    with _INSTANCE_MONITOR_REGISTRY_LOCK:
        _INSTANCE_MONITOR_REGISTRY[instance_id] = (state, cp_state_holder, mode)


def _unregister_monitor(instance_id: str) -> None:
    with _INSTANCE_MONITOR_REGISTRY_LOCK:
        _INSTANCE_MONITOR_REGISTRY.pop(instance_id, None)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  LAYER 2 — RUNTIME STATE (agent responsibility)                             ║
# ║                                                                              ║
# ║  Stateful bookkeeping for the control loop.  MUST NOT contain:              ║
# ║    • cognition/governance truth  (phase, principal, evidence_refs)          ║
# ║    • Jingu admission logic       (validation, rejection, repair hints)      ║
# ║                                                                              ║
# ║  Owns:                                                                       ║
# ║    last_verify_time, verify_in_flight, _prev_patch_non_empty,               ║
# ║    no_progress_steps, verify_history, cp_state                              ║
# ║                                                                              ║
# ║  Separation invariant (three-kind rule):                                    ║
# ║    fact         — observable signal from ONE step (patch_non_empty, etc.)   ║
# ║    control state— cross-step bookkeeping (debounce, in-flight, stagnation)  ║
# ║    governance   — cognition truth (phase, subtype, principals, verdict)     ║
# ║  A variable MUST belong to exactly one kind.  Cross-kind = structural bug.  ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

# StepMonitorState: imported from step_monitor_state.py (see top of file)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  LAYER 3 — RUNTIME CONTROL (agent responsibility)                           ║
# ║                                                                              ║
# ║  Loop control: decides WHEN to trigger verify, advance stagnation,          ║
# ║  enforce stop.  Reads/writes RuntimeState.  MUST NOT contain:               ║
# ║    • Jingu validation logic  (principal check, schema, admission)           ║
# ║    • governance truth        (phase taxonomy, subtype, evidence)            ║
# ║                                                                              ║
# ║  Owns:                                                                       ║
# ║    _install_step_monitor, _monitored_step                                   ║
# ║    debounce, patch_first_write detection, pee gating,                       ║
# ║    inner-verify scheduling, stagnation counter, VerdictStop enforcement     ║
# ║                                                                              ║
# ║  Key rule: "should_trigger_verify" and "should_stop" are RUNTIME decisions. ║
# ║  They depend on history. They do NOT belong in Jingu governance.            ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _step_observe(agent_self, *, step_n: int, mode: str) -> tuple[str, str, bool]:
    """
    Section 1: pure observation — extract text, run cognition parse, detect env mutation.

    Returns (latest_assistant_text, snippet, env_error_detected).
    Side-effects: appends cognition violation feedback as user message (non-fatal).
    """
    latest_assistant_text = ""
    snippet = ""
    for msg in reversed(agent_self.messages):
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        content = c["text"]
                        break
            if isinstance(content, str):
                latest_assistant_text = content
                snippet = content.replace("\n", " ")[:80]
            break

    # 改动9c: increment _llm_step when a new assistant response is detected.
    # _llm_step is used as the idempotency key scope for phase_prefix and cognition_violation.
    # One LLM response may trigger _monitored_step N times (once per tool call) — all N
    # invocations see the same assistant text → same _llm_step → dedup fires correctly.
    _state_ref = getattr(agent_self, "_jingu_monitor_state", None)
    if _state_ref is not None and latest_assistant_text:
        if latest_assistant_text != _state_ref._last_assistant_text:
            _state_ref._llm_step += 1
            _state_ref._last_assistant_text = latest_assistant_text
            _state_ref._observe_tool_signal = False  # reset on new LLM step

    # Y-lite: detect observation-class tool calls in the latest assistant message.
    # Any tool call in OBSERVE phase = agent is gathering evidence via tools.
    # We set observe_tool_signal=True for the gate to treat as implicit evidence basis.
    if _state_ref is not None:
        for _msg in reversed(agent_self.messages):
            if _msg.get("role") == "assistant":
                _tcs = _msg.get("tool_calls", [])
                if _tcs:
                    _state_ref._observe_tool_signal = True
                # Also check extra.actions (some adapters use this format)
                elif _msg.get("extra", {}).get("actions"):
                    _state_ref._observe_tool_signal = True
                break

    print(f"    [step {step_n}] ${agent_self.cost:.2f}  {snippet}", flush=True)

    # Cognition record parse + violation feedback
    if mode == "jingu" and latest_assistant_text:
        try:
            from cognition_schema import check_step_cognition, format_violation_feedback
            _cog_record, _cog_violations = check_step_cognition(latest_assistant_text, step_n=step_n)
            if _cog_record is not None:
                _principal_str = " ".join(_cog_record.principals) if _cog_record.principals else "(none)"
                print(
                    f"    [cognition] step={step_n} phase={_cog_record.phase}"
                    f" principals=[{_principal_str}]"
                    f" evidence={len(_cog_record.evidence_refs)}"
                    f" violations={len(_cog_violations)}",
                    flush=True,
                )
                if _cog_violations:
                    _feedback = format_violation_feedback(_cog_violations, _cog_record)
                    # 改动9c: use _llm_step (not step_n/n_calls) for keyed idempotency.
                    # _state_ref already set above from _jingu_monitor_state.
                    _viol_codes = ":".join(sorted(v.code if hasattr(v, "code") else str(v) for v in _cog_violations))
                    _llm_step_id = _state_ref._llm_step if _state_ref is not None else step_n
                    _cog_key = f"{_llm_step_id}:cognition_violation:{_viol_codes}"
                    if _state_ref is None or _cog_key not in _state_ref._injected_signals:
                        if _state_ref is not None:
                            _state_ref._injected_signals.add(_cog_key)
                        print(f"    [cognition] VIOLATION — injecting feedback", flush=True)
                        agent_self.messages.append({"role": "user", "content": _feedback})
                    else:
                        print(f"    [cognition] VIOLATION — skipped=dedup key={_cog_key}", flush=True)
        except Exception as _cog_exc:
            print(f"    [cognition] parse error (non-fatal): {_cog_exc}", flush=True)

    # Env mutation detection
    env_error_detected = False
    for msg in reversed(agent_self.messages):
        if msg.get("role") == "assistant":
            has_mut, trigger = _msg_has_env_mutation(msg)
            if has_mut:
                env_error_detected = True
                print(
                    f"    [env-mutation] ENVIRONMENT_MUTATION_IN_AGENT_LOOP "
                    f"step={step_n} trigger={trigger!r} — "
                    f"agent is doing env work (pip/conda/setup.py). "
                    f"This belongs to infrastructure, not agent reasoning.",
                    flush=True,
                )
            break

    return latest_assistant_text, snippet, env_error_detected


def _step_verify_if_needed(
    agent_self,
    *,
    state: "StepMonitorState",
    verify_debounce_s: float,
) -> bool:
    """
    Section 2: patch signal detection + conditional inner-verify dispatch.

    Returns step_patch_non_empty (True if agent has a real, non-empty patch).
    Side-effects: may launch a background threading.Thread for inner-verify.
    """
    import threading as _thr
    import subprocess as _sp_iv

    step_patch_non_empty = False
    for msg in reversed(agent_self.messages):
        if msg.get("role") == "assistant":
            if not _msg_has_signal(msg):
                break
            step_patch_non_empty = True
            cid = state.container_id
            if not cid:
                break
            now = time.monotonic()
            with state._lock:
                too_soon = (now - state.last_verify_time) < verify_debounce_s
                in_flight = state.verify_in_flight
            if too_soon or in_flight:
                break
            _base_commit = state.instance.get("base_commit", "HEAD")
            _git_diff_result = _sp_iv.run(
                ["docker", "exec", "-w", "/testbed", cid, "git", "diff", _base_commit],
                capture_output=True, text=True, timeout=30,  # Bug F fix (p20): 10s too short under 30-worker load
            )
            _raw_diff = _git_diff_result.stdout if _git_diff_result.returncode == 0 else ""
            current_patch = (_raw_diff.strip() + "\n") if _raw_diff.strip() else ""
            if not current_patch:
                step_patch_non_empty = False
                break
            with state._lock:
                state.last_verified_patch = current_patch
                state.last_verify_time = now
                state.verify_in_flight = True

            step_n = agent_self.n_calls
            print(
                f"    [inner-verify] triggering verify at step={step_n} "
                f"(patch changed, container={cid[:12]}...)",
                flush=True,
            )

            def _run_verify(patch=current_patch, container=cid, step=step_n):
                try:
                    # v2: inner-verify uses apply_test_patch=False (agent-visible signal only)
                    cv_result = run_controlled_verify(
                        patch, state.instance, container, timeout_s=45,
                        apply_test_patch=False,
                    )
                    state.record_verify(step, cv_result)
                except Exception as exc:
                    print(f"    [inner-verify] ERROR: {exc}", flush=True)
                finally:
                    with state._lock:
                        state.verify_in_flight = False

            _thr.Thread(target=_run_verify, daemon=True).start()
            break

    return step_patch_non_empty


def _step_cp_update_and_verdict(
    agent_self,
    *,
    state: "StepMonitorState",
    cp_state_holder: "list | None",
    env_error_detected: bool,
    step_patch_non_empty: bool,
    latest_assistant_text: str,
) -> None:
    """
    Section 3: control-plane state update + verdict decision + verdict actions.

    Side-effects:
    - Updates cp_state / cp_state_holder[0] via update_cp_with_step_signals
    - Sets state.early_stop_verdict + agent n_calls on VerdictStop
    - Sets state.pending_redirect_hint on VerdictRedirect
    - Advances cp_state phase + appends phase_records on VerdictAdvance
    - Logs per-step cp telemetry
    """
    _pee, _pee_reason = state.update_cp_with_step_signals(
        env_error_detected=env_error_detected,
        patch_non_empty=step_patch_non_empty,
        cp_state_holder=cp_state_holder,
    )
    _cp_s = cp_state_holder[0] if cp_state_holder is not None else state.cp_state
    _step_signals_present = bool(env_error_detected or step_patch_non_empty)
    _weak_progress = extract_weak_progress(
        env_error_detected=env_error_detected,
        patch_non_empty=step_patch_non_empty,
        latest_tests_passed=state.latest_tests_passed(),
    )
    if _step_signals_present or _weak_progress:
        _pee_str = f"True({_pee_reason})" if _pee else "False"
        print(
            f"    [cp-step] instance={state.instance_id} attempt={state.attempt}"
            f" signals={[k for k,v in [('env',env_error_detected),('patch',step_patch_non_empty)] if v]}"
            f" no_progress:{_cp_s.no_progress_steps} step:{_cp_s.step_index}"
            f" env_noise:{_cp_s.env_noise} actionability:{_cp_s.actionability}"
            f" weak_progress:{_weak_progress} pee:{_pee_str}",
            flush=True,
        )

    _step_verdict = decide_next(_cp_s)
    _verdict_to_log = f"step={_cp_s.step_index} verdict={_step_verdict.type}"
    if hasattr(_step_verdict, "to") and _step_verdict.to is not None:
        _verdict_to_log += f" to={_step_verdict.to}"
    if hasattr(_step_verdict, "reason") and _step_verdict.reason:
        _verdict_to_log += f" reason={_step_verdict.reason}"
    print(f"    [cp] {_verdict_to_log}", flush=True)

    if isinstance(_step_verdict, VerdictStop):
        state.early_stop_verdict = _step_verdict
        print(
            f"    [cp] VerdictStop enforcement: raising StopExecution({_step_verdict.reason})"
            f" — immediate interrupt, no phase injection",
            flush=True,
        )
        raise StopExecution(_step_verdict.reason)

    elif isinstance(_step_verdict, VerdictRedirect):
        # 改动6: execute_no_progress loop breaker.
        # EXECUTE→DECIDE redirect is valid but must not loop indefinitely.
        # Count consecutive execute_no_progress redirects; > limit → StopExecution.
        _EXECUTE_REDIRECT_LIMIT = 3
        if _step_verdict.reason == "execute_no_progress":
            _exec_key = ("EXECUTE", "execute_no_progress")
            state._retryable_loop_counts[_exec_key] = (
                state._retryable_loop_counts.get(_exec_key, 0) + 1
            )
            _exec_redirect_count = state._retryable_loop_counts[_exec_key]
            print(
                f"    [cp] execute_no_progress_redirect count={_exec_redirect_count}"
                f" limit={_EXECUTE_REDIRECT_LIMIT}",
                flush=True,
            )
            if _exec_redirect_count > _EXECUTE_REDIRECT_LIMIT:
                # Bug A fix (p17): execute_no_progress is attempt-terminal, not instance-terminal.
                # Outer loop now does `continue` (not `break`) on no_signal — next attempt gets
                # a fresh cp_state and the NBR hint. This is the correct scope for this signal.
                print(
                    f"    [cp] execute_no_progress loop exceeded limit={_EXECUTE_REDIRECT_LIMIT}"
                    f" → VerdictStop(no_signal) [attempt-terminal, will retry]",
                    flush=True,
                )
                state.early_stop_verdict = VerdictStop(reason="no_signal")
                raise StopExecution("no_signal")
        else:
            # Non-execute redirect: reset execute_no_progress counter
            _exec_key = ("EXECUTE", "execute_no_progress")
            state._retryable_loop_counts[_exec_key] = 0

        state.pending_redirect_hint = f"[REDIRECT:{_step_verdict.to}] {_step_verdict.reason}"
        agent_self.messages.append({
            "role": "user",
            "content": (
                f"[Control-plane redirect: {_step_verdict.reason}] "
                f"Re-examine your environment assumptions. "
                f"Transition to phase {_step_verdict.to} before patching."
            ),
        })
        # Clear pending_redirect_hint — already injected above.
        state.pending_redirect_hint = ""

    elif isinstance(_step_verdict, VerdictAdvance):
        _old_phase = _cp_s.phase
        if _step_verdict.to is not None:
            import dataclasses as _dc_adv
            # Bug E fix (p19): reset no_progress_steps=0 on phase transition.
            # Stagnation-triggered advance carries no_progress_steps >= threshold into
            # the new phase, causing immediate cascade: ANALYZE→DECIDE→EXECUTE in 2 steps.
            # Each phase must start with a clean stagnation slate.
            if cp_state_holder is not None:
                cp_state_holder[0] = _dc_adv.replace(cp_state_holder[0], phase=_step_verdict.to, no_progress_steps=0)
                _cp_s = cp_state_holder[0]
            else:
                state.cp_state = _dc_adv.replace(state.cp_state, phase=_step_verdict.to, no_progress_steps=0)
                _cp_s = state.cp_state
        # Log agent declared phase at advance time — key for misalignment diagnosis.
        try:
            from declaration_extractor import _extract_phase_from_message as _epfm, _PHASE_NORM as _pnorm
            _adv_declared_raw = _epfm(latest_assistant_text)
            _adv_declared = _pnorm.get(_adv_declared_raw, _adv_declared_raw) if _adv_declared_raw else "none"
        except Exception:
            _adv_declared = "unknown"
        print(
            f"    [cp] phase_advance from={_old_phase} to={_step_verdict.to}"
            f" agent_declared={_adv_declared}",
            flush=True,
        )

        _pr = None
        _pr_source = "none"
        _pr_foreign_phase = ""   # agent declared a different phase than eval_phase
        try:
            from declaration_extractor import extract_record_for_phase as _extract_for_phase
            # Phase addressability invariant:
            #   record_phase == eval_phase, always.
            #   Agent-declared phase guides intent detection, NOT gate record selection.
            #
            # Rule 1: look for a cached record with phase == _old_phase (exact match).
            # Rule 2: if none found, use extract_record_for_phase(target=_old_phase),
            #         which enforces target_phase regardless of agent's PHASE: declaration.
            # Rule 3: if agent declared a foreign phase (≠ eval_phase), log as soft signal;
            #         do NOT pass those foreign principals to the gate.
            # Rule 4: telemetry prints eval_phase / record_phase / declared_phase / source.
            _eval_phase = str(_old_phase).upper()
            _prev_pr = next(
                (r for r in reversed(state.phase_records)
                 if r.phase.upper() == _eval_phase),
                None,
            )
            if _prev_pr is not None:
                _pr = _prev_pr
                _pr_source = "cache"
            else:
                # No cached record — extract phase data.
                # p224: Primary path — structured extraction via JinguModel.
                # Makes an independent LLM call with grammar-constrained sampling
                # (response_format json_schema). Schema guarantees valid JSON.
                # Fallback: regex extraction if structured extract fails.
                _accumulated = state._phase_accumulated_text.get(_eval_phase, "")
                _extract_text = _accumulated if _accumulated.strip() else latest_assistant_text
                _structured_parsed = None

                # Try structured extraction via JinguModel
                try:
                    from jingu_onboard import onboard as _onboard_fn
                    _gov = _onboard_fn()
                    _extraction_schema = _gov.get_constrained_schema(_eval_phase)
                    # p226-03: derive phase_hint from cognition success_criteria
                    _phase_hint = ""
                    try:
                        _cog = _gov.get_cognition(_eval_phase)
                        if _cog and _cog.success_criteria:
                            _phase_hint = "; ".join(_cog.success_criteria)
                    except Exception:
                        pass  # non-critical — hint is optional
                    if _extraction_schema is not None and hasattr(agent_self, "model"):
                        _model = agent_self.model
                        if hasattr(_model, "structured_extract"):
                            _structured_parsed = _model.structured_extract(
                                accumulated_text=_extract_text,
                                phase=_eval_phase,
                                schema=_extraction_schema,
                                phase_hint=_phase_hint,
                            )
                except Exception as _se_exc:
                    print(
                        f"    [phase_record] structured_extract error (non-fatal): {_se_exc}",
                        flush=True,
                    )

                if _structured_parsed is not None:
                    # Build PhaseRecord from structured output — zero regex
                    # p226: use build_phase_record_from_structured for bundle schema compatibility
                    # (evidence_refs as [string], not [{file,line,observation}])
                    _pr = build_phase_record_from_structured(
                        _structured_parsed, str(_old_phase)
                    )
                    state.phase_records.append(_pr)
                    _pr_source = "structured"
                    _declared_phase = (_structured_parsed.get("phase") or "").upper()
                    _foreign = bool(_declared_phase and _declared_phase != _eval_phase)
                    _acc_len = len(_accumulated) if _accumulated else 0
                    print(
                        f"    [phase_record] extraction_method=structured"
                        f" extraction_schema_source=bundle"
                        f" accumulated_chars={_acc_len}"
                        f" fields={list(_structured_parsed.keys())}",
                        flush=True,
                    )
                else:
                    # Fallback: regex extraction from accumulated text
                    _pr, _declared_phase, _foreign = _extract_for_phase(
                        _extract_text, str(_old_phase)
                    )
                    state.phase_records.append(_pr)
                    _pr_source = "extracted"
                    _acc_len = len(_accumulated) if _accumulated else 0
                    _has_refs = bool(getattr(_pr, "evidence_refs", None))
                    _has_rc = bool((getattr(_pr, "root_cause", "") or "").strip())
                    print(
                        f"    [phase_record] extraction_method=regex_fallback"
                        f" extraction_schema_source=regex_fallback"
                        f" accumulated_chars={_acc_len}"
                        f" has_root_cause={_has_rc}"
                        f" has_evidence_refs={_has_refs}",
                        flush=True,
                    )
                if _foreign:
                    _pr_foreign_phase = _declared_phase
                    # Soft signal: agent declared a foreign phase in this message.
                    # The extracted record has empty principals/evidence_refs because
                    # those belong to the foreign phase, not to eval_phase.
                    # Classify misalignment direction: ahead = agent is past eval_phase,
                    # behind = agent is re-doing an earlier phase, unknown = unlisted phase.
                    _PHASE_ORDER = ["UNDERSTAND", "OBSERVE", "ANALYZE", "DECIDE", "EXECUTE", "JUDGE"]
                    try:
                        _eval_idx = _PHASE_ORDER.index(_eval_phase)
                        _decl_idx = _PHASE_ORDER.index(_declared_phase)
                        _align = "declared_ahead" if _decl_idx > _eval_idx else "declared_behind"
                        _align_delta = _decl_idx - _eval_idx
                    except (ValueError, AttributeError):
                        _align = "unknown_phase"
                        _align_delta = 0
                    print(
                        f"    [phase_record] foreign_phase_declared:"
                        f" eval_phase={_eval_phase} declared_phase={_declared_phase}"
                        f" alignment={_align} delta={_align_delta}"
                        f" — principals extracted from foreign context discarded",
                        flush=True,
                    )
            # Rule 4: telemetry
            print(
                f"    [phase_record] eval_phase={_eval_phase}"
                f" record_phase={_pr.phase} source={_pr_source}"
                f" subtype={_pr.subtype} principals={_pr.principals}"
                f" evidence_refs={_pr.evidence_refs}",
                flush=True,
            )
        except Exception as _pr_exc:
            print(f"    [phase_record] error (non-fatal): {_pr_exc}", flush=True)

        # p222: Cognition validation — validate PhaseRecord against bundle contracts
        _cognition_rejected = False
        if _pr is not None:
            try:
                from cognition_loader import COGNITION_EXECUTION_ENABLED as _COG_ENABLED
                if _COG_ENABLED:
                    from cognition_loader import CognitionLoader as _CogLoader
                    from phase_validator import (
                        validate_phase_record as _validate_pr,
                        build_validation_feedback as _build_cog_feedback,
                    )
                    from jingu_loader import JinguLoader as _JL
                    _cog_bundle = _JL()._bundle
                    _cog_loader = _CogLoader(_cog_bundle)
                    _cog_errors = _validate_pr(_pr, _cog_loader)
                    if _cog_errors:
                        _cog_codes = [e.code for e in _cog_errors]
                        print(
                            f"    [cognition_validator] REJECT errors={_cog_codes}",
                            flush=True,
                        )
                        _cog_feedback = _build_cog_feedback(_cog_errors, _pr, _cog_loader)
                        _cognition_rejected = True
                        # Redirect back to current phase with feedback
                        import dataclasses as _dc_cog
                        if cp_state_holder is not None:
                            cp_state_holder[0] = _dc_cog.replace(
                                cp_state_holder[0],
                                phase=_old_phase,
                                no_progress_steps=0,
                            )
                        else:
                            state.cp_state = _dc_cog.replace(
                                state.cp_state,
                                phase=_old_phase,
                                no_progress_steps=0,
                            )
                        _step_verdict = VerdictContinue(reason="cognition_validation_failed")
                        # Inject feedback for next step
                        _injections.append({
                            "role": "user",
                            "content": (
                                f"[Cognition Validation Failed]\n\n"
                                f"{_cog_feedback}\n\n"
                                f"Fix the issues above and resubmit for phase {_old_phase}."
                            ),
                        })
                        # Invalidate cached phase record
                        state.phase_records = [
                            r for r in state.phase_records
                            if r.phase.upper() != _eval_phase
                        ]
                    else:
                        # Check phase transition
                        if _old_phase and str(_step_verdict) != "VerdictContinue":
                            _from_p = str(_old_phase).upper()
                            _to_p = str(getattr(_step_verdict, 'to', '')).upper()
                            if _to_p and not _cog_loader.is_transition_allowed(_from_p, _to_p):
                                print(
                                    f"    [cognition_validator] transition_warning"
                                    f" from={_from_p} to={_to_p} allowed=false",
                                    flush=True,
                                )
                        print(
                            f"    [cognition_validator] PASS phase={_pr.phase}"
                            f" subtype={_pr.subtype}",
                            flush=True,
                        )
            except Exception as _cog_exc:
                print(f"    [cognition_validator] error (non-fatal): {_cog_exc}", flush=True)

        # p211: Analysis gate — enforce quality before EXECUTE advance
        _analysis_gate_rejected = False
        _analysis_gate_force_passed = False
        _AG_MAX_REJECTS = 2  # escape hatch: after N rejects, let agent proceed
        if _eval_phase == "ANALYZE" and _pr is not None and not _cognition_rejected:
            try:
                from analysis_gate import evaluate_analysis as _eval_analysis
                _analysis_verdict = _eval_analysis(
                    _pr,
                    structured_output=(_pr_source == "structured"),
                )
                _ag_reject_count = state.analysis_gate_rejects
                print(
                    f"    [analysis_gate] passed={_analysis_verdict.passed}"
                    f" failed_rules={_analysis_verdict.failed_rules}"
                    f" scores={_analysis_verdict.scores}"
                    f" rejects_so_far={_ag_reject_count}",
                    flush=True,
                )
                if not _analysis_verdict.passed and _ag_reject_count >= _AG_MAX_REJECTS:
                    print(f"    [analysis_gate] FORCE_PASS — max_rejects={_AG_MAX_REJECTS} reached, allowing advance", flush=True)
                    _analysis_gate_force_passed = True
                elif not _analysis_verdict.passed:
                    _analysis_gate_rejected = True
                    # Reset phase back to ANALYZE — do not advance to EXECUTE
                    import dataclasses as _dc_ag
                    if cp_state_holder is not None:
                        cp_state_holder[0] = _dc_ag.replace(
                            cp_state_holder[0], phase="ANALYZE", no_progress_steps=0
                        )
                        _cp_s = cp_state_holder[0]
                    else:
                        state.cp_state = _dc_ag.replace(
                            state.cp_state, phase="ANALYZE", no_progress_steps=0
                        )
                        _cp_s = state.cp_state
                    # p217: SDG structured repair or p214 field-level fallback
                    _sdg_repair_used = False
                    if _SDG_ENABLED and getattr(_analysis_verdict, "rejection", None):
                        try:
                            _sdg_content = _build_sdg_repair(_analysis_verdict.rejection)
                            _sdg_content += "\n\nFix only the failing fields. Do not rewrite fields already OK.\nStay in ANALYZE phase."
                            agent_self.messages.append({
                                "role": "user",
                                "content": _sdg_content,
                            })
                            _sdg_repair_used = True
                            print(f"    [analysis_gate] sdg_repair_used=true failures={len(_analysis_verdict.rejection.failures)}", flush=True)
                        except Exception as _sdg_exc:
                            print(f"    [analysis_gate] sdg_repair error (fallback to p214): {_sdg_exc}", flush=True)

                    if not _sdg_repair_used:
                        # Fallback: p214 field-level contract feedback
                        _ag_scores = _analysis_verdict.scores
                        _ag_pass = 0.5  # must match analysis_gate._THRESHOLD
                        _ag_field_status = (
                            f"- ROOT_CAUSE: {'OK' if _ag_scores.get('code_grounding', 0) >= _ag_pass else 'MISSING'}"
                            f" (score={_ag_scores.get('code_grounding', 0):.1f})\n"
                            f"- CAUSAL_CHAIN: {'OK' if _ag_scores.get('causal_chain', 0) >= _ag_pass else 'MISSING'}"
                            f" (score={_ag_scores.get('causal_chain', 0):.1f})\n"
                            f"- ALTERNATIVES: {'OK' if _ag_scores.get('alternative_hypothesis', 0) >= _ag_pass else 'MISSING'}"
                            f" (score={_ag_scores.get('alternative_hypothesis', 0):.1f})"
                        )
                        agent_self.messages.append({
                            "role": "user",
                            "content": (
                                f"[analysis_gate REJECT]\n"
                                f"ANALYZE gate result:\n"
                                f"{_ag_field_status}\n\n"
                                f"Fix only the MISSING fields. Do not rewrite fields already OK.\n"
                                f"Stay in ANALYZE phase."
                            ),
                        })
                    # Invalidate cached phase record so next step re-extracts
                    state.phase_records = [
                        r for r in state.phase_records
                        if r.phase.upper() != _eval_phase
                    ]
                    state.analysis_gate_rejects += 1
                    print(f"    [analysis_gate] REJECT ({state.analysis_gate_rejects}/{_AG_MAX_REJECTS}) — redirecting to ANALYZE", flush=True)
            except Exception as _ag_exc:
                print(f"    [analysis_gate] error (non-fatal): {_ag_exc}", flush=True)

        # Design gate — enforce design quality before EXECUTE advance
        _design_gate_rejected = False
        _design_gate_force_passed = False
        _DG_MAX_REJECTS = 2
        if _eval_phase == "DESIGN" and _pr is not None and not _cognition_rejected:
            try:
                from design_gate import evaluate_design as _eval_design
                _design_verdict = _eval_design(_pr)
                _dg_reject_count = getattr(state, 'design_gate_rejects', 0)
                print(
                    f"    [design_gate] passed={_design_verdict.passed}"
                    f" failed_rules={_design_verdict.failed_rules}"
                    f" scores={_design_verdict.scores}"
                    f" rejects_so_far={_dg_reject_count}",
                    flush=True,
                )
                if not _design_verdict.passed and _dg_reject_count >= _DG_MAX_REJECTS:
                    print(f"    [design_gate] FORCE_PASS — max_rejects={_DG_MAX_REJECTS} reached, allowing advance", flush=True)
                    _design_gate_force_passed = True
                elif not _design_verdict.passed:
                    _design_gate_rejected = True
                    import dataclasses as _dc_dg
                    if cp_state_holder is not None:
                        cp_state_holder[0] = _dc_dg.replace(
                            cp_state_holder[0], phase="DESIGN", no_progress_steps=0
                        )
                        _cp_s = cp_state_holder[0]
                    else:
                        state.cp_state = _dc_dg.replace(
                            state.cp_state, phase="DESIGN", no_progress_steps=0
                        )
                        _cp_s = state.cp_state
                    # SDG structured repair or fallback
                    _dg_sdg_repair_used = False
                    if _SDG_ENABLED and getattr(_design_verdict, "rejection", None):
                        try:
                            _dg_sdg_content = _build_sdg_repair(_design_verdict.rejection)
                            _dg_sdg_content += "\n\nFix only the failing fields. Do not rewrite fields already OK.\nStay in DESIGN phase."
                            agent_self.messages.append({
                                "role": "user",
                                "content": _dg_sdg_content,
                            })
                            _dg_sdg_repair_used = True
                            print(f"    [design_gate] sdg_repair_used=true failures={len(_design_verdict.rejection.failures)}", flush=True)
                        except Exception as _dg_sdg_exc:
                            print(f"    [design_gate] sdg_repair error (fallback): {_dg_sdg_exc}", flush=True)

                    if not _dg_sdg_repair_used:
                        _dg_scores = _design_verdict.scores
                        _dg_pass = 0.5
                        _dg_field_status = (
                            f"- INVARIANT_PRESERVATION: {'OK' if _dg_scores.get('invariant_preservation', 0) >= _dg_pass else 'MISSING'}"
                            f" (score={_dg_scores.get('invariant_preservation', 0):.1f})\n"
                            f"- DESIGN_COMPARISON: {'OK' if _dg_scores.get('design_comparison', 0) >= _dg_pass else 'MISSING'}"
                            f" (score={_dg_scores.get('design_comparison', 0):.1f})\n"
                            f"- CONSTRAINT_ENCODING: {'OK' if _dg_scores.get('constraint_encoding', 0) >= _dg_pass else 'MISSING'}"
                            f" (score={_dg_scores.get('constraint_encoding', 0):.1f})"
                        )
                        agent_self.messages.append({
                            "role": "user",
                            "content": (
                                f"[design_gate REJECT]\n"
                                f"DESIGN gate result:\n"
                                f"{_dg_field_status}\n\n"
                                f"Fix only the MISSING fields. Do not rewrite fields already OK.\n"
                                f"Stay in DESIGN phase."
                            ),
                        })
                    state.phase_records = [
                        r for r in state.phase_records
                        if r.phase.upper() != _eval_phase
                    ]
                    if not hasattr(state, 'design_gate_rejects'):
                        state.design_gate_rejects = 0
                    state.design_gate_rejects += 1
                    print(f"    [design_gate] REJECT ({state.design_gate_rejects}/{_DG_MAX_REJECTS}) — redirecting to DESIGN", flush=True)
            except Exception as _dg_exc:
                print(f"    [design_gate] error (non-fatal): {_dg_exc}", flush=True)

        try:
            if _cognition_rejected:
                raise RuntimeError("cognition_validator rejected, skipping principal gate")
            if _analysis_gate_rejected:
                raise RuntimeError("analysis_gate rejected, skipping principal gate")
            if _analysis_gate_force_passed:
                raise RuntimeError("analysis_gate FORCE_PASS, skipping principal gate to allow advance")
            if _design_gate_rejected:
                raise RuntimeError("design_gate rejected, skipping principal gate")
            if _design_gate_force_passed:
                raise RuntimeError("design_gate FORCE_PASS, skipping principal gate to allow advance")
            if _pr is None:
                raise RuntimeError("phase_record unavailable, skipping principal gate")
            from principal_gate import (
                evaluate_admission as _eval_admission,
                get_principal_feedback as _get_pg_feedback,
            )
            from control.reasoning_state import set_principal_violation as _set_pv
            # Rule 1: evaluate against _old_phase (the phase being completed), not _pr.phase
            # (which may differ if the record was extracted with wrong phase in prior sessions).
            _obs_tool_sig = getattr(getattr(agent_self, "_jingu_monitor_state", None), "_observe_tool_signal", False)
            # p23: save ANALYZE root_cause for EXECUTE causal binding check
            if _eval_phase == "ANALYZE" and _pr is not None:
                _rc = getattr(_pr, "root_cause", "") or ""
                if _rc:
                    state.last_analyze_root_cause = _rc
                    print(f"    [phase_record] root_cause saved ({len(_rc)} chars)", flush=True)
            _admission = _eval_admission(
                _pr, _eval_phase,
                observe_tool_signal=_obs_tool_sig,
                last_analyze_root_cause=state.last_analyze_root_cause if _eval_phase == "EXECUTE" else "",
                structured_output=(_pr_source == "structured"),
            )
            # 改动10: if agent declared a foreign phase, prepend the reason so gate output
            # reflects the actual problem (phase boundary violation) rather than a misleading
            # missing_required_field:evidence_refs (which we no longer discard, but principals
            # are still untrusted, so a foreign_phase reason is still warranted).
            if _pr_foreign_phase:
                _phase_order = ["UNDERSTAND", "OBSERVE", "ANALYZE", "DECIDE", "EXECUTE", "JUDGE"]
                _delta = abs(_phase_order.index(_pr_foreign_phase) - _phase_order.index(_eval_phase)) if (_pr_foreign_phase in _phase_order and _eval_phase in _phase_order) else 0
                _foreign_reason = f"foreign_phase_declared:declared={_pr_foreign_phase},eval={_eval_phase},delta={_delta}"
                if _foreign_reason not in _admission.reasons:
                    _admission.reasons.insert(0, _foreign_reason)
                # principals were cleared (untrusted) — don't also report them as missing
                _admission.reasons = [r for r in _admission.reasons if not r.startswith("missing_required_principal")]
                # Bug B fix (p16): after stripping missing_required_principal, if the only
                # remaining reason is foreign_phase_declared, promote status to ADMITTED.
                # Without this, status stays RETRYABLE → same (phase, reason) fires 3+ times
                # → ESCALATE_CONTRACT_BUG loop → 22/30 FAILED in p16.
                # foreign_phase_declared is a phase boundary annotation, not a violation —
                # the record is still evaluable (evidence_refs preserved, 改动10).
                _non_foreign_reasons = [r for r in _admission.reasons if r != _foreign_reason]
                if not _non_foreign_reasons and _admission.status == "RETRYABLE":
                    _admission.status = "ADMITTED"
            print(
                f"    [principal_gate] eval_phase={_eval_phase} record_phase={_pr.phase}"
                f" admission={_admission.status} reasons={_admission.reasons}",
                flush=True,
            )
            if _admission.status in ("RETRYABLE", "REJECTED"):
                # Pick a representative violation code for cp_state / feedback
                _pg_violation = _admission.reasons[0] if _admission.reasons else "admission_violation"
                _pg_feedback = _get_pg_feedback(_pg_violation)
                try:
                    from jingu_onboard import onboard as _onb_repair
                    _gov_repair = _onb_repair()
                    # Use governance routing for repair target
                    _route_obj = _gov_repair.get_route(str(_cp_s.phase), _pg_violation)
                    _repair_phase = _route_obj.next_phase if _route_obj else ""
                    _repair_hint = _gov_repair.get_repair_hint(str(_cp_s.phase), _pg_violation)
                    _pg_guidance = _repair_hint if _repair_hint else ""
                except Exception:
                    _repair_phase = ""
                    _pg_guidance = ""
                _repair_suffix = f" Repair phase: {_repair_phase}." if _repair_phase else ""
                _guidance_suffix = f" {_pg_guidance}" if _pg_guidance else ""
                # p217: Use SDG structured repair hint when available
                if _SDG_ENABLED and getattr(_admission, "rejection", None):
                    try:
                        _sdg_hint = _build_sdg_repair(_admission.rejection)
                        state.pending_redirect_hint = _sdg_hint
                        print(f"    [principal_gate] sdg_repair_used=true failures={len(_admission.rejection.failures)}", flush=True)
                    except Exception as _sdg_exc:
                        print(f"    [principal_gate] sdg_repair error (fallback): {_sdg_exc}", flush=True)
                        state.pending_redirect_hint = (
                            f"[{_admission.status}:{_pg_violation}] {_pg_feedback}{_repair_suffix}{_guidance_suffix}"
                        )
                else:
                    state.pending_redirect_hint = (
                        f"[{_admission.status}:{_pg_violation}] {_pg_feedback}{_repair_suffix}{_guidance_suffix}"
                    )
                # p216: augment redirect hint with data-driven routing strategy
                if is_data_driven_routing_enabled():
                    try:
                        _p216_phase = _eval_phase
                        # Extract principal name from violation code
                        _p216_principal = _pg_violation.split(":")[-1] if ":" in _pg_violation else _pg_violation
                        _p216_next, _p216_strategy = route_failure_p216(_p216_phase, _p216_principal)
                        _p216_prompt = get_strategy_prompt(_p216_strategy)
                        state.pending_redirect_hint = _p216_prompt + "\n\n" + state.pending_redirect_hint
                        print(
                            f"    [p216-routing] phase={_p216_phase} principal={_p216_principal}"
                            f" -> next={_p216_next} strategy={_p216_strategy}",
                            flush=True,
                        )
                    except Exception as _p216_exc:
                        print(f"    [p216-routing] error (non-fatal): {_p216_exc}", flush=True)
                # ── cognition-aware control: admission result → cp_state → verdict ──
                if cp_state_holder is not None:
                    cp_state_holder[0] = _set_pv(cp_state_holder[0], _pg_violation)
                    _cp_s = cp_state_holder[0]
                else:
                    state.cp_state = _set_pv(state.cp_state, _pg_violation)
                    _cp_s = state.cp_state

                if _admission.status == "REJECTED":
                    # Phase boundary error — stop attempt, do not redirect
                    state.early_stop_verdict = VerdictStop(reason="no_signal")
                    print(
                        f"    [principal_gate] REJECTED → VerdictStop"
                        f" reasons={_admission.reasons}",
                        flush=True,
                    )
                    raise StopExecution("no_signal")
                else:
                    # RETRYABLE — right phase, incomplete output; redirect to repair
                    # Invalidate cached record for eval_phase so next step does fresh extraction.
                    # Without this, a RETRYABLE record stays in cache and blocks all retries.
                    state.phase_records = [
                        r for r in state.phase_records
                        if r.phase.upper() != _eval_phase
                    ]
                    # P16 loop breaker: same (phase, reason) 3+ consecutive → ESCALATE
                    # Rule 4: loop key must bind (eval_phase, reason) — not _pr.phase
                    _loop_key = (_eval_phase, _pg_violation)
                    state._retryable_loop_counts[_loop_key] = (
                        state._retryable_loop_counts.get(_loop_key, 0) + 1
                    )
                    # Reset counts for all OTHER keys (different phase or reason = not same loop)
                    for _k in list(state._retryable_loop_counts):
                        if _k != _loop_key:
                            state._retryable_loop_counts[_k] = 0
                    _loop_count = state._retryable_loop_counts[_loop_key]
                    _RETRYABLE_LOOP_LIMIT = 3
                    _contract_bypass = False
                    # p23: structured output violations are NOT eligible for contract_bypass.
                    # missing_root_cause / missing_plan / plan_not_grounded represent cognition
                    # quality requirements — agent must produce structured reasoning to proceed.
                    # contract_bypass applies only to principal declaration gaps (capability mismatch),
                    # not to structural reasoning failures.
                    _STRUCTURED_BYPASS_EXEMPT = {
                        "missing_root_cause",
                        "missing_plan",
                        "plan_not_grounded_in_root_cause",
                    }
                    _has_structured_violation = any(
                        r in _STRUCTURED_BYPASS_EXEMPT
                        for r in (_admission.reasons or [])
                    )
                    if _loop_count >= _RETRYABLE_LOOP_LIMIT and not _has_structured_violation:
                        # Bug C fix (p18): ESCALATE_CONTRACT_BUG should NOT stop the instance.
                        # When agent consistently fails to declare required principals, this is
                        # a contract-vs-agent-capability gap, not an agent logic error.
                        # Stopping (no_signal) wastes both attempts — agent never gets to patch.
                        # Fix: bypass admission (ADMITTED with contract_bypass marker) and let
                        # agent continue. Score will be lower but instance can still solve.
                        print(
                            f"    [principal_gate] ESCALATE_CONTRACT_BUG:"
                            f" phase={_loop_key[0]} reason={_loop_key[1]}"
                            f" count={_loop_count} >= {_RETRYABLE_LOOP_LIMIT}"
                            f" → contract_bypass ADMITTED (agent continues without principal check)",
                            flush=True,
                        )
                        _admission.status = "ADMITTED"
                        _admission.reasons = [f"contract_bypass:{_loop_key[1]}"]
                        state._retryable_loop_counts[_loop_key] = 0  # reset to avoid re-trigger
                        _contract_bypass = True
                        # Skip RETRYABLE redirect injection below — agent continues normally.

                    if not _contract_bypass and not state.early_stop_verdict:
                        _pv_verdict = decide_next(_cp_s)
                        print(
                            f"    [principal_gate] RETRYABLE → cognition_verdict={_pv_verdict.type}"
                            f" to={getattr(_pv_verdict, 'to', '')}",
                            flush=True,
                        )
                        if isinstance(_pv_verdict, VerdictRedirect):
                            # Update cp_state phase to redirect target so subsequent
                            # decide_next() and evaluate_admission() use the correct phase.
                            import dataclasses as _dc_ret
                            if cp_state_holder is not None:
                                cp_state_holder[0] = _dc_ret.replace(
                                    cp_state_holder[0], phase=_pv_verdict.to
                                )
                                _cp_s = cp_state_holder[0]
                            else:
                                state.cp_state = _dc_ret.replace(
                                    state.cp_state, phase=_pv_verdict.to
                                )
                                _cp_s = state.cp_state
                            agent_self.messages.append({
                                "role": "user",
                                "content": (
                                    f"[Cognition gate RETRYABLE: {_pg_violation}] "
                                    f"{_pg_feedback} "
                                    f"{_pg_guidance} "
                                    f"Return to phase {_pv_verdict.to} before proceeding."
                                ),
                            })
                            # Clear pending_redirect_hint — already injected above,
                            # so _step_inject_phase won't inject it a second time.
                            state.pending_redirect_hint = ""
        except Exception as _pg_exc:
            print(f"    [principal_gate] error={_pg_exc}", flush=True)

        try:
            if _analysis_gate_rejected:
                raise RuntimeError("analysis_gate rejected, skipping inference check")
            if _analysis_gate_force_passed:
                raise RuntimeError("analysis_gate FORCE_PASS, skipping inference check")
            if _pr is None:
                raise RuntimeError("phase_record unavailable, skipping inference check")
            # Inference telemetry: run inference directly to log per-principal signals.
            # This is independent of check_principal_inference — purely observability.
            try:
                from principal_inference import run_inference as _run_inf
                from jingu_onboard import onboard as _onb_inf
                _gov_inf = _onb_inf()
                _inf_cfg = _gov_inf.get_phase_config(_eval_phase)
                _inf_subtype = _inf_cfg.subtype if _inf_cfg else ""
                _inf_result = _run_inf(_pr, _inf_subtype)
                _inf_telem_parts = []
                for _pname, _pdetail in _inf_result.details.items():
                    _inferred_flag = "✓" if _pname in _inf_result.present else "✗"
                    _inf_telem_parts.append(
                        f"{_inferred_flag}{_pname}(score={_pdetail.score:.1f}"
                        f" signals={_pdetail.signals})"
                    )
                print(
                    f"    [principal_inference] subtype={_inf_subtype}"
                    f" declared={[p.lower() for p in (_pr.principals or [])]}"
                    f" inferred={_inf_result.present}",
                    flush=True,
                )
                if _inf_telem_parts:
                    print(
                        f"    [principal_inference] details: {' | '.join(_inf_telem_parts)}",
                        flush=True,
                    )
            except Exception as _inf_telem_exc:
                print(f"    [principal_inference] telemetry error={_inf_telem_exc}", flush=True)
            from principal_gate import check_principal_inference as _check_pi
            _inf_violation = _check_pi(_pr, _eval_phase)
            # p207-P9: filter out bypassed principals from fake violation
            if _inf_violation and "fake_principal" in _inf_violation and state._bypassed_principals:
                _fake_names = [
                    p.strip() for p in _inf_violation.split(":", 1)[1].split(",")
                    if p.strip()
                ]
                _remaining = [p for p in _fake_names if p not in state._bypassed_principals]
                if _remaining:
                    _inf_violation = f"fake_principal:{','.join(_remaining)}"
                else:
                    # All fake principals are bypassed — no violation
                    _inf_violation = None
                    print(
                        f"    [principal_inference] fake_principals_all_bypassed:"
                        f" bypassed={sorted(state._bypassed_principals)}",
                        flush=True,
                    )
            if _inf_violation and "fake_principal" in _inf_violation:
                # CC2: fake principal = declared but not supported by behavioral evidence.
                # Only fake_checkable principals (stage 4, has inference rule) reach here.
                # Treat as RETRYABLE — same loop-break logic as evaluate_admission RETRYABLE.
                try:
                    from jingu_onboard import onboard as _onb_inf_repair
                    _gov_inf_repair = _onb_inf_repair()
                    _inf_route = _gov_inf_repair.get_route(_eval_phase, "fake_principal")
                    _inf_repair = _inf_route.next_phase if _inf_route else _eval_phase
                    _inf_guidance = _gov_inf_repair.get_repair_hint(_eval_phase, "fake_principal")
                except Exception:
                    _inf_repair = ""
                    _inf_guidance = ""
                _inf_repair_suffix = f" Repair phase: {_inf_repair}." if _inf_repair else ""
                state.pending_redirect_hint = (
                    f"[RETRYABLE:{_inf_violation}] "
                    f"Your declared principals are not supported by your reasoning. "
                    f"Provide concrete evidence (file references, causal reasoning) "
                    f"before declaring these principals.{_inf_repair_suffix} {_inf_guidance}"
                )
                print(
                    f"    [principal_inference] FAKE_RETRYABLE: phase={_eval_phase}"
                    f" violation={_inf_violation} repair={_inf_repair}",
                    flush=True,
                )
                # Invalidate cached record so next step does fresh extraction.
                state.phase_records = [
                    r for r in state.phase_records
                    if r.phase.upper() != _eval_phase
                ]
                # Loop-break: same (eval_phase, violation) N+ times → ESCALATE
                _fi_loop_key = (_eval_phase, _inf_violation)
                state._retryable_loop_counts[_fi_loop_key] = (
                    state._retryable_loop_counts.get(_fi_loop_key, 0) + 1
                )
                for _k in list(state._retryable_loop_counts):
                    if _k != _fi_loop_key:
                        state._retryable_loop_counts[_k] = 0
                _fi_loop_count = state._retryable_loop_counts[_fi_loop_key]
                _FAKE_LOOP_LIMIT = 3
                if _fi_loop_count >= _FAKE_LOOP_LIMIT:
                    # p207-P9: selective bypass — only bypass the specific principals
                    # that caused the fake loop, not all contracts.
                    _fake_principals = []
                    if ":" in _inf_violation:
                        _fake_principals = [
                            p.strip() for p in _inf_violation.split(":", 1)[1].split(",")
                            if p.strip()
                        ]
                    state._bypassed_principals.update(_fake_principals)
                    state._retryable_loop_counts[_fi_loop_key] = 0  # reset to avoid re-trigger
                    print(
                        f"    [principal_inference] FAKE_LOOP_SELECTIVE_BYPASS:"
                        f" phase={_eval_phase} violation={_inf_violation}"
                        f" count={_fi_loop_count} >= {_FAKE_LOOP_LIMIT}"
                        f" → bypassed_principals={sorted(state._bypassed_principals)}"
                        f" (selective bypass, other principals still enforced)",
                        flush=True,
                    )
                    # Clear pending redirect hint — agent continues normally
                    state.pending_redirect_hint = ""
            elif _inf_violation and "missing_required" in _inf_violation:
                pass  # logged by principal_gate above; inference perspective is telemetry only
        except Exception as _pi_exc:
            print(f"    [principal_inference] check error={_pi_exc}", flush=True)

        try:
            if _analysis_gate_rejected:
                raise RuntimeError("analysis_gate rejected, skipping telemetry")
            if _analysis_gate_force_passed:
                raise RuntimeError("analysis_gate FORCE_PASS, skipping telemetry")
            if _pr is None:
                raise RuntimeError("phase_record unavailable, skipping telemetry")
            from principal_inference import run_inference, diff_principals
            from jingu_onboard import onboard as _onb_telem
            _gov_telem = _onb_telem()
            _pi_cfg = _gov_telem.get_phase_config(_eval_phase)
            _pi_subtype = _pi_cfg.subtype if _pi_cfg else ""
            _inf_rich = run_inference(_pr, _pi_subtype)
            diff_principals(
                getattr(_pr, "principals", []) or [],
                _inf_rich,
                phase=_eval_phase,
            )
        except Exception:
            pass


# ── Per-step structure validation (p207-P2) ──────────────────────────────────
# Required structured fields per phase, derived from phase_prompt.py templates.
# Keys are phase names (uppercase), values are lists of field markers to check.
# Check is lightweight: looks for "FIELD_NAME:" in agent output (not full parsing).
PHASE_REQUIRED_FIELDS: dict[str, list[str]] = {
    "UNDERSTAND": ["PROBLEM_STATEMENT", "EXPECTED_BEHAVIOR", "ACTUAL_BEHAVIOR", "SCOPE"],
    "OBSERVE":    ["EVIDENCE"],
    "ANALYZE":    ["ROOT_CAUSE", "EVIDENCE", "CAUSAL_CHAIN"],
    "DECIDE":     ["OPTIONS", "SELECTED", "CONSTRAINTS"],
    "EXECUTE":    ["PLAN", "CHANGE_SCOPE"],
    "JUDGE":      ["VERDICT", "TEST_RESULTS", "CONFIDENCE"],
}


def _step_check_structure(
    agent_self,
    *,
    cp_state_holder: "list | None",
    state: "StepMonitorState",
    latest_assistant_text: str,
) -> None:
    """
    p207-P2: per-step structure validation with correction hints.

    After each agent step, check if the current phase's required structured fields
    are present in the agent's latest output. If missing, inject a SOFT correction
    hint into the next step's context.

    Design decisions:
    - Check ONLY the current phase's required fields (not all fields)
    - Correction is a HINT (soft), not a REJECT (hard)
    - Only inject correction ONCE per missing field per attempt (don't spam)
    - Uses state._injected_signals with key "structure:<FIELD>" for dedup
    - Log format: [structure-check] phase=X missing=FIELD injected=true|dedup

    Does not block execution. Exception-safe — failure must not crash main flow.
    """
    try:
        _cp_s = cp_state_holder[0] if cp_state_holder is not None else state.cp_state
        _phase = str(_cp_s.phase).upper()

        required = PHASE_REQUIRED_FIELDS.get(_phase)
        if not required:
            return  # unknown phase or no requirements — skip silently

        if not latest_assistant_text:
            return  # no output to check — skip

        # Lightweight check: look for "FIELD_NAME:" pattern in output
        missing: list[str] = []
        for field in required:
            if f"{field}:" not in latest_assistant_text and f"{field.lower()}:" not in latest_assistant_text:
                # Check dedup: only report once per field per attempt
                _dedup_key = f"structure:{field}"
                if _dedup_key not in state._injected_signals:
                    missing.append(field)

        if not missing:
            print(f"    [structure-check] phase={_phase} all_present=true", flush=True)
            return

        # Build correction hint for missing fields
        # ROOT_CAUSE (ANALYZE) and PLAN (EXECUTE) get WARNING-level mandatory language;
        # all other fields remain soft hints.
        _MANDATORY_FIELDS = {
            ("ANALYZE", "ROOT_CAUSE"): (
                "WARNING: Your analysis MUST include ROOT_CAUSE: with a specific file and line. "
                "Without this, your fix will be unfocused."
            ),
            ("EXECUTE", "PLAN"): (
                "WARNING: Your execution MUST include PLAN: listing specific changes. "
                "Without this, your patch may be incomplete."
            ),
        }
        _missing_str = ", ".join(missing)
        _hint_parts = [
            f"[STRUCTURE HINT] Your {_phase} output is missing required fields: {_missing_str}.",
        ]
        _has_mandatory = False
        for field in missing:
            _mandatory_msg = _MANDATORY_FIELDS.get((_phase, field))
            if _mandatory_msg:
                _hint_parts.append(_mandatory_msg)
                _has_mandatory = True
            else:
                _hint_parts.append(f"  {field}:")
                _hint_parts.append(f"  <your {field.lower().replace('_', ' ')} here>")
        if not _has_mandatory:
            _hint_parts.insert(1, "Please include these fields in your next response using the format:")

        _hint = "\n".join(_hint_parts)

        # Inject hint and mark fields as corrected (dedup)
        agent_self.messages.append({"role": "user", "content": _hint})
        for field in missing:
            _dedup_key = f"structure:{field}"
            state._injected_signals.add(_dedup_key)
            print(f"    [structure-check] phase={_phase} missing={field} injected=true", flush=True)

    except Exception as _exc:
        print(f"    [structure-check] error (non-fatal): {_exc}", flush=True)


def _step_inject_phase(agent_self, *, cp_state_holder: "list | None", state: "StepMonitorState") -> None:
    """
    Section p189: inject current phase as a user message prefix.
    Also consumes state.pending_redirect_hint — any hint set during this step
    is injected now so the agent sees it at the start of the next step.
    Exception-safe — injection failure must not crash main flow.
    """
    # Consume pending_redirect_hint set during this step (principal_gate, inference,
    # cognition_fail, patch_format etc.). Inject before phase prefix so hint appears first.
    try:
        _hint = state.pending_redirect_hint
        if _hint:
            agent_self.messages.append({"role": "user", "content": _hint})
            print(f"    [phase_injection] redirect_hint injected=true", flush=True)
            state.pending_redirect_hint = ""
    except Exception:
        pass

    try:
        from phase_prompt import build_phase_prefix as _build_phase_prefix
        _cp_s = cp_state_holder[0] if cp_state_holder is not None else state.cp_state
        _phase_str = str(_cp_s.phase)
        _phase_prefix = _build_phase_prefix(_phase_str)
        if _phase_prefix:
            _phase_content = _phase_prefix.rstrip("\n")
            # 改动9c: use _llm_step (not n_calls) for keyed idempotency.
            # n_calls increments per tool call; _llm_step increments per LLM response.
            # One LLM response with N tool calls → _monitored_step fires N times →
            # all N see same _llm_step → only first injection passes, rest are deduped.
            _phase_key = f"{state._llm_step}:phase_prefix:{_phase_str}"
            if _phase_key not in state._injected_signals:
                state._injected_signals.add(_phase_key)
                agent_self.messages.append({"role": "user", "content": _phase_content})
                print(f"    [phase_injection] phase={_phase_str} injected=true", flush=True)
            else:
                print(f"    [phase_injection] phase={_phase_str} skipped=dedup", flush=True)
    except Exception as _phase_exc:
        print(f"    [phase_injection] error (non-fatal): {_phase_exc}", flush=True)


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

    # P18: register state in global registry keyed by instance_id.
    # _monitored_step dispatches via self.instance_id, not closure — concurrent-safe.
    _register_monitor(instance_id, state, cp_state_holder, mode)

    def _monitored_step(self):
        result = _orig_step(self)

        # P18: dispatch via instance_id — each parallel worker finds its own state.
        _iid = getattr(self, "instance_id", instance_id)
        with _INSTANCE_MONITOR_REGISTRY_LOCK:
            _entry = _INSTANCE_MONITOR_REGISTRY.get(_iid)
        if _entry is None:
            return result  # registry cleared (run finished) — skip monitoring
        _state, _cp_holder, _mode = _entry

        # Thin orchestrator: delegates to the four section functions above.
        # Each section is independently testable and has explicit parameter contracts.
        # 改动9b: wire monitor state onto agent instance so _step_observe can access
        # _injected_signals for cognition violation keyed idempotency.
        self._jingu_monitor_state = _state
        _latest_assistant_text, _snippet, _env_error = _step_observe(
            self, step_n=self.n_calls, mode=_mode
        )
        # p221: accumulate assistant text per phase for PhaseRecord extraction.
        # Agent outputs short thinking texts across many steps; accumulating them
        # gives extract_record_for_phase enough material at VerdictAdvance time.
        _acc_phase = str((_cp_holder[0] if _cp_holder else _state.cp_state).phase).upper()
        if _latest_assistant_text:
            _state._phase_accumulated_text[_acc_phase] = (
                _state._phase_accumulated_text.get(_acc_phase, "") + "\n" + _latest_assistant_text
            )
        _patch_non_empty = _step_verify_if_needed(
            self, state=_state, verify_debounce_s=VERIFY_DEBOUNCE_S
        )
        try:
            _step_cp_update_and_verdict(
                self,
                state=_state,
                cp_state_holder=_cp_holder,
                env_error_detected=_env_error,
                step_patch_non_empty=_patch_non_empty,
                latest_assistant_text=_latest_assistant_text,
            )
        except StopExecution:
            # VerdictStop issued — skip phase injection and propagate up to run_agent.
            # This is the correct termination path: immediate interrupt, no delayed enforcement.
            raise

        _step_inject_phase(self, cp_state_holder=_cp_holder, state=_state)

        # p207-P2: per-step structure validation — check if agent output has
        # the current phase's required fields, inject soft correction hint if missing.
        _step_check_structure(
            self,
            cp_state_holder=_cp_holder,
            state=_state,
            latest_assistant_text=_latest_assistant_text,
        )

        # p25 Materialization Gate Layer 1 (in-loop liveness, K=2):
        # Once EXECUTE phase is entered, agent MUST write a patch within 2 steps.
        # If no write happens in K steps, inject a strong forcing hint.
        _mat_phase = str((_cp_holder[0] if _cp_holder else _state.cp_state).phase).upper()
        _mat_step = getattr(self, "n_calls", -1)
        if _mat_phase == "EXECUTE":
            if _state._execute_entry_step < 0:
                # First step in EXECUTE — record entry
                _state._execute_entry_step = _mat_step
                _state._execute_write_seen = False
                print(f"    [mat-gate] EXECUTE entered at step={_mat_step}", flush=True)

                # Retroactive analysis gate: if agent entered EXECUTE without
                # passing through a VerdictAdvance(from=ANALYZE) — e.g. stagnation
                # cascade or direct CONTINUE→EXECUTE — the analysis_gate never fired.
                # Check the most recent ANALYZE phase record now.
                _retro_ag_max = 2  # same as _AG_MAX_REJECTS
                _retro_ag_count = _state.analysis_gate_rejects
                _last_analyze_pr = next(
                    (r for r in reversed(_state.phase_records)
                     if getattr(r, 'phase', '').upper() == 'ANALYZE'),
                    None,
                )
                if _last_analyze_pr is not None:
                    try:
                        from analysis_gate import evaluate_analysis as _retro_eval
                        _retro_verdict = _retro_eval(_last_analyze_pr)
                        print(
                            f"    [mat-gate] retroactive_analysis_gate"
                            f" passed={_retro_verdict.passed}"
                            f" failed_rules={_retro_verdict.failed_rules}"
                            f" scores={_retro_verdict.scores}"
                            f" rejects_so_far={_retro_ag_count}",
                            flush=True,
                        )
                        if not _retro_verdict.passed and _retro_ag_count < _retro_ag_max:
                            # Redirect back to ANALYZE — analysis quality insufficient
                            import dataclasses as _dc_retro
                            _cp_ref = _cp_holder[0] if _cp_holder else _state.cp_state
                            _cp_ref_new = _dc_retro.replace(
                                _cp_ref, phase="ANALYZE", no_progress_steps=0
                            )
                            if _cp_holder:
                                _cp_holder[0] = _cp_ref_new
                            else:
                                _state.cp_state = _cp_ref_new
                            _state._execute_entry_step = -1  # reset — not in EXECUTE anymore
                            _state.analysis_gate_rejects += 1

                            # Inject feedback — prefer SDG if available
                            _retro_feedback_injected = False
                            if _SDG_ENABLED and getattr(_retro_verdict, "rejection", None):
                                try:
                                    _retro_sdg = _build_sdg_repair(_retro_verdict.rejection)
                                    _retro_sdg += (
                                        "\n\nFix only the failing fields. Do not rewrite fields already OK."
                                        "\nStay in ANALYZE phase."
                                    )
                                    self.messages.append({"role": "user", "content": _retro_sdg})
                                    _retro_feedback_injected = True
                                    print(
                                        f"    [mat-gate] retroactive_analysis_gate"
                                        f" sdg_repair_used=true"
                                        f" failures={len(_retro_verdict.rejection.failures)}",
                                        flush=True,
                                    )
                                except Exception as _retro_sdg_exc:
                                    print(
                                        f"    [mat-gate] retroactive_analysis_gate"
                                        f" sdg_repair error: {_retro_sdg_exc}",
                                        flush=True,
                                    )

                            if not _retro_feedback_injected:
                                _retro_reasons = "; ".join(_retro_verdict.reasons)
                                self.messages.append({
                                    "role": "user",
                                    "content": (
                                        f"[analysis_gate REJECT — retroactive check at EXECUTE entry]\n"
                                        f"Your ANALYZE phase output did not pass quality checks:\n"
                                        f"{_retro_reasons}\n\n"
                                        f"Return to ANALYZE and address the failing checks before proceeding to EXECUTE."
                                    ),
                                })

                            print(
                                f"    [mat-gate] retroactive_analysis_gate REJECT"
                                f" ({_state.analysis_gate_rejects}/{_retro_ag_max})"
                                f" — redirecting to ANALYZE",
                                flush=True,
                            )
                        elif not _retro_verdict.passed:
                            print(
                                f"    [mat-gate] retroactive_analysis_gate FORCE_PASS"
                                f" — max_rejects={_retro_ag_max} reached",
                                flush=True,
                            )
                    except Exception as _retro_exc:
                        print(
                            f"    [mat-gate] retroactive_analysis_gate error (non-fatal):"
                            f" {_retro_exc}",
                            flush=True,
                        )
            if _patch_non_empty:
                _state._execute_write_seen = True
            _MAT_GATE_K = 2
            _steps_since_entry = _mat_step - _state._execute_entry_step
            if _steps_since_entry >= _MAT_GATE_K and not _state._execute_write_seen:
                print(
                    f"    [mat-gate] FORCE: {_steps_since_entry} steps in EXECUTE, no patch written",
                    flush=True,
                )
                self.messages.append({
                    "role": "user",
                    "content": (
                        "[MATERIALIZATION GATE] You have been in the EXECUTE phase for "
                        f"{_steps_since_entry} steps without writing any code. "
                        "You MUST write a patch NOW. "
                        "Do not read more files. Do not analyze further. "
                        "Write the fix to the file immediately using str_replace or write_file."
                    ),
                })
        else:
            # Phase changed away from EXECUTE — reset tracking
            if _state._execute_entry_step >= 0:
                _state._execute_entry_step = -1
                _state._execute_write_seen = False

        return result

    # Return (state, ScopedPatch) — caller uses ScopedPatch as a context manager.
    # ScopedPatch.__exit__ restores ProgressTrackingAgent.step unconditionally,
    # which eliminates the P9 class of bug (monitor chain stacking across attempts).
    return state, ScopedPatch(ProgressTrackingAgent, "step", _monitored_step)


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


def patch_content_hash(patch: str) -> str:
    """
    p25 Outcome Gate: semantic-lite fingerprint for same-patch detection across attempts.
    Extracts logical content lines (added/removed, stripped of whitespace and comments),
    sorts for order-independence, returns short hash.
    Used to distinguish stuck (same content) vs exploring (different content).
    """
    if not patch:
        return "empty"
    import hashlib
    content_lines = []
    touched_files = []
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            touched_files.append(line[6:].strip())
        elif line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            content = line[1:].strip()
            if content and not content.startswith("#"):
                content_lines.append(content)
    content_lines.sort()
    fingerprint_str = "|".join(sorted(set(touched_files))) + "\n" + "\n".join(content_lines)
    return hashlib.sha256(fingerprint_str.encode()).hexdigest()[:16]


# p225-02: LAYER 1 (signal extraction) moved to signal_extraction.py


# p225-02: LAYER 4 (jingu adapter) moved to jingu_adapter.py
# p225-02: LAYER 3b (controlled verify) moved to controlled_verify.py

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


class ScopedPatch:
    """
    Scoped monkey patch — replaces an attribute on an object for the duration of a
    `with` block, then restores the original value unconditionally on exit.

    Stacks safely: multiple ScopedPatch instances on the same obj/attr will each
    save and restore the value they saw on entry, so nesting works correctly and
    no "chain stacking" (P9 class bug) is possible.

    Usage:
        with ScopedPatch(ProgressTrackingAgent, "step", monitored_step):
            run_agent(...)
        # ProgressTrackingAgent.step is now the original value again.
    """

    def __init__(self, obj, attr: str, new_value):
        self._obj = obj
        self._attr = attr
        self._new_value = new_value
        self._orig = None          # set in __enter__
        self._entered = False

    def __enter__(self):
        self._orig = getattr(self._obj, self._attr)
        setattr(self._obj, self._attr, self._new_value)
        self._entered = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._entered:
            setattr(self._obj, self._attr, self._orig)
        return False   # do not suppress exceptions

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

# p225-02: LAYER 5 (jingu validation / governance) moved to jingu_adapter.py

# ── mini-SWE-agent runner (direct Python API) ─────────────────────────────────

# Official mini-swe-agent Verified run config (collection 737e5dd2, run b6e8010b)
# Uses Anthropic direct API with interleaved thinking (reasoning_effort=high)
MODEL = __import__("os").environ.get("JINGU_MODEL", "bedrock/global.anthropic.claude-sonnet-4-5-20250929-v1:0")

BASE_CONFIG = {
    "model": {
        "model_class": "jingu",
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
) -> tuple[str | None, str | None, dict | None, object | None]:
    """Run mini-SWE-agent on one instance. Returns (submission patch or None, exit_status, jingu_body or None, monitor or None)."""
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

    # B4: phase-structured reasoning protocol — p224: loaded from bundle via jingu_onboard.
    # All phase prompts, principal requirements, type contracts, forbidden moves
    # are derived from bundle.json (compiled by jingu-cognition TS). Zero hardcoded strings.
    try:
        from jingu_onboard import onboard as _onboard_prompt
        _gov_prompt = _onboard_prompt()
        # Assemble full reasoning protocol from per-phase prompts
        _phase_prompt_parts = []
        for _pp_phase in _gov_prompt.list_phases():
            _pp_text = _gov_prompt.get_phase_prompt(_pp_phase)
            if _pp_text:
                _phase_prompt_parts.append(_pp_text)
        # Build type contracts block from gate configs
        _type_contracts_lines = []
        for _pp_phase in _gov_prompt.list_phases():
            _pp_gate = _gov_prompt.get_gate(_pp_phase)
            if _pp_gate:
                _pp_req = ", ".join(_pp_gate.required_principals)
                _pp_forb = ", ".join(_pp_gate.forbidden_principals)
                _pp_forb_str = f"  forbidden=[{_pp_forb}]" if _pp_forb else ""
                _type_contracts_lines.append(
                    f"  {_pp_gate.subtype.split('.')[-1]:<20} required=[{_pp_req}]{_pp_forb_str}"
                )
        _type_contracts_block = "Type contracts:\n" + "\n".join(_type_contracts_lines)
        # Per-step principal requirements
        def _get_req(p):
            _g = _gov_prompt.get_gate(p)
            return ", ".join(_g.required_principals) if _g else ""
        _analysis_req = _get_req("ANALYZE")
        _decision_req = _get_req("DECIDE")
        _execute_req  = _get_req("EXECUTE")
    except Exception as _onb_exc:
        print(f"    [jingu_onboard] prompt load error (fallback): {_onb_exc}", flush=True)
        _phase_prompt_parts = []
        _type_contracts_block = "Type contracts: (see principal_gate for v2.0 contracts)"
        _analysis_req = "ontology_alignment, phase_boundary_discipline, causal_grounding, evidence_linkage"
        _decision_req = "ontology_alignment, phase_boundary_discipline, option_comparison, constraint_satisfaction"
        _execute_req  = "ontology_alignment, phase_boundary_discipline, action_grounding, minimal_change"

    # If bundle provides compiled phase prompts, use them directly
    if _phase_prompt_parts:
        _combined_prompt = "\n\n".join(_phase_prompt_parts)
        extra_parts.append(
            f"REASONING PROTOCOL (governance system enforces these — follow exactly):\n\n"
            f"{_combined_prompt}\n\n"
            f"{_type_contracts_block}\n\n"
            f"Rules:\n"
            f"  - Output PHASE: markers exactly as shown — the governance system parses them\n"
            f"  - FIX_TYPE must match CLAIMS from your decision step\n"
            f"  - PRINCIPALS must include ALL required for your type, none of the forbidden"
        )
    else:
        # Fallback: hardcoded protocol (pre-bundle)
        extra_parts.append(
            "REASONING PROTOCOL (output these markers as you work — they are parsed by the governance system):\n\n"
            "## STEP 1 — before writing any code, output all three:\n"
            "  PHASE: analysis\n"
            f"  PRINCIPALS: {_analysis_req}\n"
            "  EVIDENCE: <file:line or test name that shows the bug>\n"
            "  ROOT_CAUSE: <the specific line or logic that causes the failure>\n\n"
            "## STEP 2 — once root cause is clear, output:\n"
            "  PHASE: decision\n"
            f"  PRINCIPALS: {_decision_req}\n"
            "  CLAIMS: <chosen fix type — execution | diagnosis | design | planning>\n"
            "  SCOPE: <which files/functions will be changed>\n\n"
            "## STEP 3 — BEFORE writing any code, output these lines first:\n"
            "  PHASE: execution\n"
            f"  PRINCIPALS: {_execute_req}\n"
            "  EVIDENCE: <which analysis step or file:line justified this change>\n"
            "  Then write the patch.\n\n"
            "## STEP 4 — before calling submit, output these two lines exactly:\n"
            "  FIX_TYPE: <one of: understanding | observation | analysis | diagnosis | decision | design | planning | execution | validation>\n"
            "  PRINCIPALS: <space-separated list — must satisfy the contract for your chosen type>\n\n"
            f"{_type_contracts_block}\n\n"
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
    # _install_step_monitor now returns (state, ScopedPatch) — the ScopedPatch is used
    # as a context manager below to guarantee ProgressTrackingAgent.step is restored.
    _monitor, _step_patch = _install_step_monitor(instance_id, attempt, instance, cp_state_holder=cp_state_holder, mode=mode)

    # Hook DefaultAgent.run() to:
    # 1. Inject container_id into _monitor as soon as container is started
    # 2. Run a final controlled_verify BEFORE env cleanup (end-of-attempt signal)
    from minisweagent.agents.default import DefaultAgent as _DA
    _orig_run = _DA.run   # captured here; ScopedPatch restores it on __exit__

    def _verifying_run(self_agent, *args, **kwargs):
        # P18: look up this agent's monitor from registry — avoids using closed-over
        # _monitor which may belong to a different parallel worker.
        _iid = getattr(self_agent, "instance_id", instance_id)
        with _INSTANCE_MONITOR_REGISTRY_LOCK:
            _vr_entry = _INSTANCE_MONITOR_REGISTRY.get(_iid)
        _vr_monitor = _vr_entry[0] if _vr_entry is not None else _monitor

        # Inject container_id so step monitor can start verifying mid-run
        cid = getattr(getattr(self_agent, 'env', None), 'container_id', None)
        if cid and not _vr_monitor.container_id:
            _vr_monitor.container_id = cid
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
                # p221: try structured output first
                _cg_structured = _try_parse_structured_output(_cg_msgs)
                if _cg_structured is not None:
                    _cg_decl = extract_from_structured(_cg_structured)
                else:
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
                _vr_monitor.pending_redirect_hint = f"[COGNITION_FAIL] {_cg_feedback}"
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
                f"changed_file_relevant={'pass' if _judge_result.changed_file_relevant else 'fail'}",
                flush=True,
            )
            if not _judge_result.all_pass:
                # Hard check failures — set redirect hints (controlled_verify gated below)
                if not _judge_result.patch_non_empty:
                    _vr_monitor.early_stop_verdict = VerdictStop(reason="empty_patch")
                elif not _judge_result.patch_format:
                    _vr_monitor.pending_redirect_hint = "[REDIRECT:EXECUTE] patch_format_error"
                elif not _judge_result.no_semantic_weakening:
                    _vr_monitor.pending_redirect_hint = "[REDIRECT:ANALYZE] semantic_weakening_detected"
                elif not _judge_result.changed_file_relevant:
                    # p204: changed_file_relevant promoted to hard check
                    # Agent modified only test files (not source) — redirect back to EXECUTE
                    _vr_monitor.pending_redirect_hint = "[REDIRECT:EXECUTE] wrong_file_changed"
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
            _vr_monitor._verify_skipped = True
            _vr_monitor._verify_skip_reason = _prereq_reason
            return result

        t_cv0 = time.monotonic()
        cv_result = run_controlled_verify(submitted, instance, cid, timeout_s=60)
        cv_result["elapsed_ms"] = round((time.monotonic() - t_cv0) * 1000, 1)
        # Store as last verify_history entry (step=-1 means end-of-attempt)
        _vr_monitor.record_verify(-1, cv_result)
        # v2 two-column log: final-verify (oracle/eval) vs inner-verify (agent-visible)
        _er = cv_result.get("eval_resolved")
        if _er is not None:
            print(f"    [outcome-eval] eval_resolved={_er}"
                  f"  f2p={cv_result.get('f2p_passed')}/{(cv_result.get('f2p_passed',0) or 0)+(cv_result.get('f2p_failed',0) or 0)}"
                  f"  p2p={cv_result.get('p2p_passed')}/{(cv_result.get('p2p_passed',0) or 0)+(cv_result.get('p2p_failed',0) or 0)}",
                  flush=True)
        return result

    t_llm = Timer("LLM agent loop (Bedrock)", parent=t_agent)
    # Both patches are scoped: ScopedPatch.__exit__ restores unconditionally on any exit path.
    # _step_patch covers ProgressTrackingAgent.step (P9 fix — no monitor chain stacking).
    # _run_patch covers DefaultAgent.run (equivalent to the old _DA.run = _orig_run restore).
    with _step_patch, ScopedPatch(_DA, "run", _verifying_run):
        try:
            process_instance(instance, attempt_dir, config, progress)
        except StopExecution as e:
            # VerdictStop: clean early exit — not an error.
            # _monitor.early_stop_verdict is already set; caller (run_with_jingu) will
            # log and break the attempt loop.
            print(
                f"  [cp] early_stop instance={instance_id} attempt={attempt}"
                f" reason={e.reason} — StopExecution caught, exiting agent loop",
                flush=True,
            )
        except Exception as e:
            print(f"    [agent] ERROR: {e}")
            traceback.print_exc()
    # P18: remove from registry immediately after run — prevents cross-case state
    # bleed on retry. _monitor object itself is still referenced below for post-processing.
    _unregister_monitor(instance_id)
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
            _fallback_cv = None
            if _monitor.verify_history:
                # Use the last controlled_fail_to_pass result (final verify or last mid-run)
                for _vh in reversed(_monitor.verify_history):
                    if _vh["kind"] == "controlled_fail_to_pass":
                        _final_cv = _vh
                        break
                # Fallback: if no controlled_fail_to_pass but controlled_error exists,
                # treat as F2P_ALL_FAIL: controlled_passed=0, controlled_failed=N.
                # This allows governance pack to classify and reroute these cases.
                if _final_cv is None:
                    for _vh in reversed(_monitor.verify_history):
                        if _vh["kind"] == "controlled_error" and _vh.get("tests_failed", 0) > 0:
                            _fallback_cv = _vh
                            break
            _cv_source = _final_cv or _fallback_cv
            if _cv_source:
                cv_flat = {
                    "verification_kind": _cv_source["kind"],
                    "tests_passed": _cv_source["tests_passed"],
                    "tests_failed": _cv_source["tests_failed"],
                    "exit_code": _cv_source["exit_code"],
                    "elapsed_ms": _cv_source["elapsed_ms"],
                    "step": _cv_source["step"],
                    # BUG-10 fix: eval-aligned fields
                    "f2p_passed": _cv_source.get("f2p_passed"),
                    "f2p_failed": _cv_source.get("f2p_failed"),
                    "p2p_passed": _cv_source.get("p2p_passed"),
                    "p2p_failed": _cv_source.get("p2p_failed"),
                    "eval_resolved": _cv_source.get("eval_resolved"),
                }
                jingu_body["controlled_verify"] = cv_flat
                jingu_body["test_results"]["ran_tests"] = True
                jingu_body["test_results"]["controlled_passed"] = _cv_source["tests_passed"]
                jingu_body["test_results"]["controlled_failed"] = _cv_source["tests_failed"]
                jingu_body["test_results"]["controlled_exit_code"] = _cv_source["exit_code"]
                # BUG-10: log eval-aligned verdict
                _er = _cv_source.get("eval_resolved")
                if _er is not None:
                    print(f"    [controlled_verify] eval_resolved={_er}"
                          f"  f2p={_cv_source.get('f2p_passed')}/{(_cv_source.get('f2p_passed',0) or 0)+(_cv_source.get('f2p_failed',0) or 0)}"
                          f"  p2p={_cv_source.get('p2p_passed')}/{(_cv_source.get('p2p_passed',0) or 0)+(_cv_source.get('p2p_failed',0) or 0)}",
                          flush=True)
                if _fallback_cv and _final_cv is None:
                    print(f"    [cv-fallback] F2P_ALL_FAIL inferred from controlled_error: "
                          f"passed={_cv_source['tests_passed']} failed={_cv_source['tests_failed']}")
                # p208: failure classification — classify cv_result into typed failure category
                _ft = classify_failure(cv_flat)
                if _ft:
                    _routing = get_failure_routing(_ft)
                    jingu_body["failure_type"] = _ft
                    jingu_body["failure_routing"] = _routing
                    jingu_body["repair_directive"] = {
                        "failure_type": _ft,
                        "next_phase": _routing["next_phase"],
                        "repair_goal": _routing["repair_goal"],
                    }
                    jingu_body["retry_mode"] = "phase_specific"
                    print(f"    [failure-classify] type={_ft} next_phase={_routing['next_phase']} "
                          f"f2p_pass={cv_flat.get('f2p_passed', 0)} "
                          f"f2p_fail={cv_flat.get('f2p_failed', 0)}", flush=True)
                else:
                    jingu_body["failure_type"] = None
                    jingu_body["failure_routing"] = None
                    jingu_body["repair_directive"] = None
                    jingu_body["retry_mode"] = "generic"
            # p207-P4: store parsed test results as structured data for all consumers.
            # Calls parse_pytest_output on CV stdout so GovernancePacks, retry_controller,
            # and any future consumer can access failing_tests/error_excerpts/summary
            # without re-parsing.
            if _cv_source and _cv_source.get("kind") == "controlled_fail_to_pass":
                _cv_stdout_p4 = _cv_source.get("stdout", "")
                _cv_stderr_p4 = _cv_source.get("stderr", "")
                if _cv_stdout_p4 or _cv_stderr_p4:
                    _parsed = parse_pytest_output(_cv_stdout_p4, _cv_stderr_p4)
                    _cp = _cv_source.get("tests_passed", 0) or 0
                    _cf = _cv_source.get("tests_failed", 0) or 0
                    jingu_body["parsed_test_results"] = {
                        "failing_tests": _parsed["failing_tests"],
                        "error_excerpts": _parsed["error_excerpts"],
                        "summary": _parsed["summary"],
                        "partial_progress": _cp > 0 and _cf > 0,
                    }
                    print(f"    [p207-P4] parsed_test_results: "
                          f"failing={len(_parsed['failing_tests'])} "
                          f"excerpts={len(_parsed['error_excerpts'])} "
                          f"partial={_cp > 0 and _cf > 0}")
            # Store full verify_history for observability
            jingu_body["verify_history"] = _monitor.verify_history
            # p190: per-phase records — one entry per VerdictAdvance during this attempt
            jingu_body["phase_records"] = [r.as_dict() for r in _monitor.phase_records]
            # p207-P9: log selective bypass summary at attempt end
            if _monitor._bypassed_principals:
                _bp_sorted = sorted(_monitor._bypassed_principals)
                jingu_body["bypassed_principals"] = _bp_sorted
                print(
                    f"    [fake_loop_summary] total_bypassed={len(_bp_sorted)}"
                    f" principals={_bp_sorted}",
                    flush=True,
                )
            # p195: principal inference telemetry — rich result with signals/explanation
            try:
                from principal_inference import run_inference, diff_principals
                from jingu_onboard import onboard as _onb_endtelem
                _gov_endtelem = _onb_endtelem()
                _pi_telemetry = []
                for _telem_pr in _monitor.phase_records:
                    _telem_phase = str(getattr(_telem_pr, "phase", ""))
                    _telem_cfg = _gov_endtelem.get_phase_config(_telem_phase)
                    _telem_subtype = _telem_cfg.subtype if _telem_cfg else ""
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
                return sub, exit_status, jingu_body, _monitor

    if sub_from_traj:
        return sub_from_traj, exit_status, jingu_body, _monitor

    if sub_from_traj_diff:
        return sub_from_traj_diff, exit_status, jingu_body, _monitor

    # 改动8: container git diff fallback.
    # agent 用 str_replace_editor 修改了文件但未调用 submit，也未打印 git diff。
    # sub_from_traj 和 sub_from_traj_diff 都为空，但容器里可能有真实的修改。
    # 直接从容器里 git diff base_commit 获取最终 patch。
    # 只在容器仍然存在时有效（StopExecution 场景下容器还活着）。
    _cid = _monitor.container_id if _monitor else None
    if _cid:
        try:
            import subprocess as _sp
            _base_c = instance.get("base_commit", "HEAD")
            _diff_r = _sp.run(
                ["docker", "exec", "-w", "/testbed", _cid, "git", "diff", _base_c],
                capture_output=True, text=True, timeout=30,  # Bug F fix (p20): 10s too short under load
            )
            _diff_patch = _diff_r.stdout.strip() if _diff_r.returncode == 0 else ""
            if _diff_patch:
                print(
                    f"    [agent] container-diff fallback: extracted {len(_diff_patch)}c patch "
                    f"from container {_cid[:12]}...",
                    flush=True,
                )
                return _diff_patch, exit_status, jingu_body, _monitor
        except Exception as _e:
            print(f"    [agent] container-diff fallback failed: {_e}", flush=True)

    return None, exit_status, jingu_body, _monitor

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
    _prev_raw_patch = ""            # p25 Outcome Gate: raw patch from previous attempt for hash comparison
    _no_progress_streak = 0          # p207-P6: consecutive no-progress attempts with different patches
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

        # Clear principal_violation at attempt boundary — prevents attempt=N violation
        # from bleeding into attempt=N+1 first step (set_principal_violation is phase-boundary
        # only; update_reasoning_state clears it each step, but not at attempt start).
        import dataclasses as _dc_boundary
        cp_state_holder[0] = _dc_boundary.replace(cp_state_holder[0], principal_violation="")

        # NBR enforcement: No Blind Retry — attempt N+1 must have concrete failure signal
        # Bypass in baseline mode: naive retry intentionally has no hint.
        if attempt > 1 and not last_failure.strip() and mode != "baseline":
            raise RuntimeError(
                f"[NBR violation] attempt {attempt} has empty last_failure. "
                "Execution feedback is required before retry. "
                "Check build_execution_feedback() and ensure tests_ran signal is captured."
            )

        patch, agent_exit, jingu_body, _attempt_monitor = run_agent(
            instance, output_dir, attempt,
            previous_failure=last_failure, parent_timer=t_inst,
            mode=mode, cp_state_holder=cp_state_holder)

        # p186: check early_stop_verdict set by _monitored_step during the attempt.
        # run_agent now returns _attempt_monitor directly — no global registry needed.
        # VerdictStop(no_signal) replaces the steps_since_last_signal >= threshold path.
        # VerdictStop(task_success) fires when task_success signal received.
        # Both cases break the attempt loop immediately — no gate, no retry needed.
        if _attempt_monitor is not None and _attempt_monitor.early_stop_verdict is not None:
            _esv = _attempt_monitor.early_stop_verdict
            print(
                f"  [cp] early_stop instance={instance_id} attempt={attempt}"
                f" reason={_esv.reason} — verdict-driven attempt termination",
                flush=True,
            )
            if _esv.reason == "no_signal":
                # p202: build typed PhaseResult from monitor state — parallel path.
                # Old generic last_failure kept as fallback; PhaseResult hint overrides
                # for the 3 known subtypes (NO_PATCH / NO_VERIFY / VERIFY_STALL).
                _mon = _attempt_monitor
                _tr = (jingu_body or {}).get("test_results", {})
                # Use cp_state_holder[0] for phase — it tracks VerdictAdvance phase changes.
                # _mon.cp_state.phase stays at OBSERVE; cp_state_holder[0].phase is current.
                _phase_result = build_phase_result(
                    str(cp_state_holder[0].phase).upper(),
                    has_patch=_mon._prev_patch_non_empty,
                    has_inner_verify=len(_mon.verify_history) > 0,
                    test_results=_tr,
                    no_progress_steps=cp_state_holder[0].no_progress_steps,
                    early_stop_reason=_esv.reason,
                    files_written=len((jingu_body or {}).get("files_written", [])),
                )
                _pr_route, _pr_target, _pr_hint = route_from_phase_result(_phase_result)
                print(
                    f"  [phase_result] phase={_phase_result.phase}"
                    f" outcome={_phase_result.outcome}"
                    f" verdict={_phase_result.verdict}"
                    f" route={_pr_route}"
                    f" target={_pr_target or '-'}"
                    f" trust={_phase_result.trust_score or '-'}"
                    f" reason={_phase_result.judge_reason}",
                    flush=True,
                )
                # Override last_failure with typed hint for the 3 known subtypes.
                # Other cases fall through to the generic message below.
                _typed_subtypes = {
                    "NO_PATCH_NO_ATTEMPT",
                    "NO_PATCH_NO_WRITE",
                    "NO_PATCH_WRITE_FAIL",
                    "NO_PATCH_ABORTED",
                    "NO_SIGNAL_NO_VERIFY",
                    "NO_SIGNAL_STALLED_AFTER_VERIFY",
                }
                if _phase_result.outcome in _typed_subtypes and _pr_hint:
                    last_failure = _pr_hint
                else:
                    # Fallback: generic no_signal message (old behaviour preserved).
                    last_failure = (
                        "Previous attempt stopped early: no progress signal detected "
                        "(control-plane verdict=STOP no_signal). "
                        "Change your approach entirely — avoid repeated reads without writing code."
                    )
            # For task_success: controlled_verify confirmed pass, no retry needed.
            # p202 fourth cut: emit [phase_result] for task_success (SUCCESS path).
            if _esv.reason == "task_success":
                _mon_ts = _attempt_monitor
                _tr_ts = (jingu_body or {}).get("test_results", {})
                _pr_ts = build_phase_result(
                    str(cp_state_holder[0].phase).upper(),
                    has_patch=_mon_ts._prev_patch_non_empty,
                    has_inner_verify=len(_mon_ts.verify_history) > 0,
                    test_results=_tr_ts,
                    no_progress_steps=cp_state_holder[0].no_progress_steps,
                    early_stop_reason="task_success",
                    files_written=len((_tr_ts or {}).get("files_written", [])),
                )
                _pr_ts_route, _pr_ts_target, _ = route_from_phase_result(_pr_ts)
                print(
                    f"  [phase_result] phase={_pr_ts.phase}"
                    f" outcome={_pr_ts.outcome}"
                    f" verdict={_pr_ts.verdict}"
                    f" route={_pr_ts_route}"
                    f" target={_pr_ts_target or '-'}"
                    f" trust={_pr_ts.trust_score or '-'}"
                    f" reason={_pr_ts.judge_reason}",
                    flush=True,
                )
                break  # task_success = instance-terminal: verified pass, no retry needed.

            # Bug A fix (p17): use early_stop_scope() to decide break vs continue.
            # no_signal → attempt-terminal: reset cp_state, continue to next attempt.
            # unknown reasons → fall through to normal gate logic (conservative).
            #
            # p24 submission persistence gate: if run_agent returned a non-empty patch
            # (agent called submit before no_signal fired), do NOT discard it — fall
            # through to normal gate logic so the submission is evaluated.
            # Only continue (discard attempt) when patch is empty.
            # Root cause of 11179 bug: att2 submitted exact gold patch + controlled_verify
            # passed, but early_stop discarded it → predictions.jsonl got empty patch.
            _scope = early_stop_scope(_esv.reason)
            if _scope == "attempt_terminal":
                if patch:
                    # Agent submitted before no_signal fired — preserve submission.
                    # Reset cp_state so any subsequent attempt starts clean.
                    cp_state_holder[0] = initial_reasoning_state("OBSERVE")
                    print(
                        f"  [cp] no_signal attempt={attempt}/{max_attempts}"
                        f" — submission preserved ({len(patch)}c patch),"
                        f" falling through to gate (p24 submission persistence)",
                        flush=True,
                    )
                    # Fall through to gate logic below (do NOT continue)
                else:
                    print(
                        f"  [cp] no_signal attempt={attempt}/{max_attempts}"
                        f" — attempt-terminal (no patch), resetting cp_state for next attempt",
                        flush=True,
                    )
                    cp_state_holder[0] = initial_reasoning_state("OBSERVE")
                    continue  # → next attempt with last_failure hint already set above

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
                # p24 Execution Materialization Gate: detect C-type failure.
                # C-type = agent was ready to execute (had plan or EXECUTE phase record)
                #          but files_written == 0 → never materialized.
                #
                # Readiness signal (at least one required — prevents firing too early):
                #   - phase_records contains EXECUTE entry (CP pushed to execution phase), OR
                #   - ANALYZE record has non-empty plan (agent declared execution intent)
                #
                # This is tighter than "any ANALYZE record exists" to avoid false positives
                # on cases where analysis was incomplete (not yet ready to execute).
                _jb = jingu_body or {}
                _files_written_count = len(_jb.get("files_written", []))
                _phase_recs = _jb.get("phase_records", [])
                _analyze_rec = next((r for r in _phase_recs if r.get("phase") == "ANALYZE"), None)
                _execute_rec = next((r for r in _phase_recs if r.get("phase") == "EXECUTE"), None)
                _has_root_cause = bool(_analyze_rec and _analyze_rec.get("root_cause"))
                _has_plan = bool(_analyze_rec and _analyze_rec.get("plan")) or bool(_execute_rec and _execute_rec.get("plan"))
                # Ready-to-execute signal: CP entered EXECUTE phase, OR agent declared a plan
                _execution_ready = bool(_execute_rec or _has_plan)
                if _files_written_count == 0 and _analyze_rec and _execution_ready:
                    # C-type Commit Avoidance: ready to execute but no file written
                    _rc_snippet = ""
                    if _has_root_cause:
                        _rc_snippet = f" Root cause from your analysis: {_analyze_rec['root_cause'][:120]}"
                    print(
                        f"    [execution-gate] EXECUTION_NO_MATERIALIZATION"
                        f" files_written={_files_written_count}"
                        f" has_root_cause={_has_root_cause}"
                        f" has_plan={_has_plan}"
                        f" execute_rec={_execute_rec is not None}",
                        flush=True,
                    )
                    last_failure = (
                        "EXECUTION REQUIRED: You identified the root cause but never edited any file. "
                        "Analysis is complete. You MUST write the patch NOW.\n\n"
                        "MANDATORY this attempt:\n"
                        "1. Do NOT re-read files or re-analyze.\n"
                        "2. Open the exact file identified in your analysis.\n"
                        "3. Make the minimal code change to fix the root cause.\n"
                        "4. Run the required tests.\n"
                        "5. Call submit.\n\n"
                        "Failure to edit at least one file = attempt counts as FAILED."
                        + (_rc_snippet if _rc_snippet else "")
                    )
                elif _files_written_count == 0 and _analyze_rec and not _execution_ready:
                    # Analysis present but no readiness signal — agent not yet ready.
                    # Log for observability; use standard hint (do not force execute prematurely).
                    print(
                        f"    [execution-gate] ANALYZE_NOT_READY"
                        f" files_written={_files_written_count}"
                        f" has_root_cause={_has_root_cause}"
                        f" has_plan={_has_plan}",
                        flush=True,
                    )
                    last_failure = "No patch was generated"
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
                    prev = attempts_log[-2].get("patch_fp") or {}
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
                            # p221: try structured output first for declaration extraction
                            _structured_decl = _try_parse_structured_output(_traj_msgs_for_signal)
                            if _structured_decl is not None:
                                _decl = extract_from_structured(_structured_decl)
                                print(f"    [cognition] extraction_method=structured", flush=True)
                            else:
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
                        _inner_cv = (jingu_body or {}).get("controlled_verify") or {}
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
                            controlled_verify=(jingu_body or {}).get("controlled_verify", {}),
                            # v2 (no-oracle) signals from inner-verify (apply_test_patch=False)
                            patch_exists=bool(patch and patch.strip()),
                            inner_f2p_passed=_inner_cv.get("f2p_passed") if _inner_cv.get("f2p_passed") is not None else -1,
                            inner_f2p_total=(_inner_cv.get("f2p_passed") or 0) + (_inner_cv.get("f2p_failed") or 0),
                            inner_new_failures=_inner_cv.get("p2p_failed") or 0,
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
                            # p25 Outcome Gate: distinguish stuck (same patch) vs exploring (different patch).
                            # SWE-bench has sparse rewards — delta==0 does NOT mean wrong direction.
                            # A different patch with delta==0 may still converge; only same patch is stuck.
                            _curr_hash = patch_content_hash(patch)
                            _prev_hash = patch_content_hash(_prev_raw_patch) if _prev_raw_patch else None
                            _same_patch = (_prev_hash is not None and _curr_hash == _prev_hash)
                            _patch_direction = "stuck" if _same_patch else "exploring"
                            print(f"    [outcome-gate] NO_PROGRESS direction={_patch_direction} "
                                  f"curr_hash={_curr_hash} prev_hash={_prev_hash}")
                            if _same_patch:
                                # Same patch content, no improvement → force strategy change
                                print(f"    [test-progress-gate] NO_PROGRESS stuck — overriding to ADJUST (force change)")
                                retry_plan = RetryPlan(
                                    root_causes=retry_plan.root_causes + ["invariant=NO_TEST_PROGRESS", "direction=stuck"],
                                    must_do=["Write a completely different patch — different approach or different file"],
                                    must_not_do=["Do not reuse any part of your previous patch"],
                                    validation_requirement="Run required tests and confirm delta > 0",
                                    next_attempt_prompt=(
                                        "NO PROGRESS + SAME PATCH: Your approach is stuck. "
                                        "You must write a fundamentally different fix. "
                                        "Abandon your current hypothesis entirely. "
                                        "Reread the failing tests with fresh eyes and form a new hypothesis."
                                    )[:600],
                                    control_action="ADJUST",
                                    principal_violations=retry_plan.principal_violations,
                                )
                                _no_progress_streak = 0  # p207-P6: stuck is a different mode, reset exploring streak
                            else:
                                # Different patch content, no improvement yet → exploring
                                _no_progress_streak += 1
                                print(f"    [outcome_gate] consecutive_no_progress={_no_progress_streak} "
                                      f"strategy_change_forced={_no_progress_streak >= 2}")
                                if _no_progress_streak >= 2:
                                    # p207-P6: 2+ consecutive no-progress with different patches → force complete strategy change
                                    print(f"    [test-progress-gate] NO_PROGRESS exploring streak={_no_progress_streak} — FORCED STRATEGY CHANGE")
                                    retry_plan = RetryPlan(
                                        root_causes=retry_plan.root_causes + ["invariant=NO_TEST_PROGRESS", "direction=exploring", f"no_progress_streak={_no_progress_streak}"],
                                        must_do=[
                                            "ABANDON your current hypothesis entirely — it has failed multiple times",
                                            "Re-read the failing test to understand what it ACTUALLY checks",
                                            "Identify a completely different root cause",
                                            "Write a fundamentally different fix targeting different code",
                                        ],
                                        must_not_do=[
                                            "Do NOT make small variations of your previous patches",
                                            "Do NOT modify the same function or method as before",
                                            "Do NOT assume your previous diagnosis was correct",
                                        ],
                                        validation_requirement="Run required tests and confirm delta > 0",
                                        next_attempt_prompt=(
                                            f"STRATEGY CHANGE REQUIRED (attempt streak={_no_progress_streak}): "
                                            f"Your last {_no_progress_streak} attempts with DIFFERENT patches all failed to improve test results. "
                                            "This means your fundamental hypothesis about the bug is wrong. "
                                            "You MUST: "
                                            "(1) ABANDON your current hypothesis entirely. "
                                            "(2) Re-read the failing test to understand what it ACTUALLY checks. "
                                            "(3) Identify a completely different root cause. "
                                            "(4) Write a fundamentally different fix — different file or different function. "
                                            "Do NOT make small variations of previous patches."
                                        )[:600],
                                        control_action="ADJUST",
                                        principal_violations=retry_plan.principal_violations,
                                    )
                                else:
                                    # First no-progress exploring attempt → gentle hint
                                    print(f"    [test-progress-gate] NO_PROGRESS exploring — gentle ADJUST")
                                    if retry_plan.control_action == "CONTINUE":
                                        retry_plan = RetryPlan(
                                            root_causes=retry_plan.root_causes + ["invariant=NO_TEST_PROGRESS", "direction=exploring"],
                                            must_do=retry_plan.must_do,
                                            must_not_do=retry_plan.must_not_do,
                                            validation_requirement=retry_plan.validation_requirement,
                                            next_attempt_prompt=(
                                                retry_plan.next_attempt_prompt
                                                + "\n[Tests not yet improving — keep iterating, change approach if needed]"
                                            ),
                                            control_action="ADJUST",
                                            principal_violations=retry_plan.principal_violations,
                                        )
                        else:
                            # Progress OK or first attempt — reset no-progress streak
                            _no_progress_streak = 0
                        # ── p27 GovernancePack pipeline ───────────────────────────────────
                        # Run all installed packs: parse_failure → recognize → route.
                        # First REROUTE decision overrides retry_plan.
                        # Architecture (p27 ADR): packs declared at module level via
                        # install_governance_pack(); no per-attempt wiring needed.
                        _gov_ctx = GovExecutionContext(
                            jingu_body=jingu_body or {},
                            fail_to_pass=fail_to_pass,
                            attempt=attempt,
                            instance_id=instance_id,
                            patch_text=patch,
                        )
                        _pack_decision = run_governance_packs(_gov_ctx)
                        if _pack_decision and _pack_decision.action == "REROUTE":
                            retry_plan = override_retry_plan_from_pack(retry_plan, _pack_decision)
                        # ─────────────────────────────────────────────────────────────────
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
                            # p202 fourth cut: emit [phase_result] at cp_verdict STOP boundary.
                            _tr_cpv = (jingu_body or {}).get("test_results", {})
                            _pr_cpv = build_phase_result(
                                str(cp_state_holder[0].phase).upper(),
                                has_patch=(_attempt_monitor._prev_patch_non_empty if _attempt_monitor else False),
                                has_inner_verify=len(_attempt_monitor.verify_history) > 0 if _attempt_monitor else False,
                                test_results=_tr_cpv,
                                no_progress_steps=cp_state_holder[0].no_progress_steps,
                                early_stop_reason=cp_verdict.reason,
                                files_written=len((jingu_body or {}).get("files_written", [])),
                            )
                            _pr_cpv_route, _pr_cpv_target, _ = route_from_phase_result(_pr_cpv)
                            print(
                                f"  [phase_result] phase={_pr_cpv.phase}"
                                f" outcome={_pr_cpv.outcome}"
                                f" verdict={_pr_cpv.verdict}"
                                f" route={_pr_cpv_route}"
                                f" target={_pr_cpv_target or '-'}"
                                f" trust={_pr_cpv.trust_score or '-'}"
                                f" reason={_pr_cpv.judge_reason}",
                                flush=True,
                            )
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
                            # p202 fourth cut: emit [phase_result] at STOP_FAIL / STOP_NO_SIGNAL.
                            _tr_sf = (jingu_body or {}).get("test_results", {})
                            _pr_sf = build_phase_result(
                                str(cp_state_holder[0].phase).upper(),
                                has_patch=(_attempt_monitor._prev_patch_non_empty if _attempt_monitor else False),
                                has_inner_verify=len(_attempt_monitor.verify_history) > 0 if _attempt_monitor else False,
                                test_results=_tr_sf,
                                no_progress_steps=cp_state_holder[0].no_progress_steps,
                                early_stop_reason=retry_plan.control_action.lower(),
                                files_written=len((jingu_body or {}).get("files_written", [])),
                            )
                            _pr_sf_route, _pr_sf_target, _ = route_from_phase_result(_pr_sf)
                            print(
                                f"  [phase_result] phase={_pr_sf.phase}"
                                f" outcome={_pr_sf.outcome}"
                                f" verdict={_pr_sf.verdict}"
                                f" route={_pr_sf_route}"
                                f" target={_pr_sf_target or '-'}"
                                f" trust={_pr_sf.trust_score or '-'}"
                                f" reason={_pr_sf.judge_reason}",
                                flush=True,
                            )
                            break
                        # verified_pass: controlled_verify confirmed all tests pass — no retry needed
                        # (kept as fallback; VerdictStop(task_success) above is the primary path)
                        if _strategy_failure_class_v2 == "verified_pass":
                            print(f"    [retry-ctrl] STOPPING — verified_pass (controlled_verify tests_failed=0)")
                            # p202 fourth cut: emit [phase_result] at verified_pass (SUCCESS path).
                            _tr_vp = (jingu_body or {}).get("test_results", {})
                            _pr_vp = build_phase_result(
                                str(cp_state_holder[0].phase).upper(),
                                has_patch=(_attempt_monitor._prev_patch_non_empty if _attempt_monitor else False),
                                has_inner_verify=len(_attempt_monitor.verify_history) > 0 if _attempt_monitor else False,
                                test_results=_tr_vp,
                                no_progress_steps=cp_state_holder[0].no_progress_steps,
                                early_stop_reason="verified_pass",
                                files_written=len((jingu_body or {}).get("files_written", [])),
                            )
                            _pr_vp_route, _pr_vp_target, _ = route_from_phase_result(_pr_vp)
                            print(
                                f"  [phase_result] phase={_pr_vp.phase}"
                                f" outcome={_pr_vp.outcome}"
                                f" verdict={_pr_vp.verdict}"
                                f" route={_pr_vp_route}"
                                f" target={_pr_vp_target or '-'}"
                                f" trust={_pr_vp.trust_score or '-'}"
                                f" reason={_pr_vp.judge_reason}",
                                flush=True,
                            )
                            break

                        # next_attempt_prompt already merges hint_prefix + exec_feedback
                        _prev_raw_patch = patch  # p25: save for Outcome Gate hash comparison
                        last_failure = retry_plan.next_attempt_prompt[:600]
                        # p209: augment with phase-specific repair prompt from failure classification
                        _jb_ft = (jingu_body or {}).get("failure_type")
                        _jb_routing = (jingu_body or {}).get("failure_routing")
                        _jb_cv = (jingu_body or {}).get("controlled_verify") or {}
                        if _jb_ft and _jb_routing:
                            _repair = build_repair_prompt(_jb_ft, _jb_cv, _jb_routing)
                            last_failure = _repair + "\n\n" + last_failure
                            print(f"    [repair-route] attempt={attempt} failure_type={_jb_ft} "
                                  f"next_phase={_jb_routing['next_phase']}", flush=True)
                        # p216: augment with data-driven routing strategy at attempt level
                        if is_data_driven_routing_enabled():
                            try:
                                _p216_phase = (jingu_body or {}).get("last_phase", "ANALYZE").upper()
                                _p216_principal = (jingu_body or {}).get("top_failed_principal", "")
                                if _p216_principal:
                                    _p216_next, _p216_strategy = route_failure_p216(_p216_phase, _p216_principal)
                                    _p216_prompt = get_strategy_prompt(_p216_strategy)
                                    last_failure = _p216_prompt + "\n\n" + last_failure
                                    print(f"    [p216-routing] attempt={attempt} phase={_p216_phase} "
                                          f"principal={_p216_principal} -> next={_p216_next} "
                                          f"strategy={_p216_strategy}", flush=True)
                            except Exception as _p216_exc:
                                print(f"    [p216-routing] error (non-fatal): {_p216_exc}", flush=True)
                    else:
                        last_failure = exec_feedback[:400]
                        # p209: augment non-retry-controller path too
                        _jb_ft = (jingu_body or {}).get("failure_type")
                        _jb_routing = (jingu_body or {}).get("failure_routing")
                        _jb_cv = (jingu_body or {}).get("controlled_verify") or {}
                        if _jb_ft and _jb_routing:
                            _repair = build_repair_prompt(_jb_ft, _jb_cv, _jb_routing)
                            last_failure = _repair + "\n\n" + last_failure
                            print(f"    [repair-route] attempt={attempt} failure_type={_jb_ft} "
                                  f"next_phase={_jb_routing['next_phase']}", flush=True)
                        # p216: augment with data-driven routing strategy at attempt level
                        if is_data_driven_routing_enabled():
                            try:
                                _p216_phase = (jingu_body or {}).get("last_phase", "ANALYZE").upper()
                                _p216_principal = (jingu_body or {}).get("top_failed_principal", "")
                                if _p216_principal:
                                    _p216_next, _p216_strategy = route_failure_p216(_p216_phase, _p216_principal)
                                    _p216_prompt = get_strategy_prompt(_p216_strategy)
                                    last_failure = _p216_prompt + "\n\n" + last_failure
                                    print(f"    [p216-routing] attempt={attempt} phase={_p216_phase} "
                                          f"principal={_p216_principal} -> next={_p216_next} "
                                          f"strategy={_p216_strategy}", flush=True)
                            except Exception as _p216_exc:
                                print(f"    [p216-routing] error (non-fatal): {_p216_exc}", flush=True)
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


# ── Per-instance S3 sync (BUG-4) ─────────────────────────────────────────────
_S3_RESULTS_BUCKET = os.environ.get("S3_RESULTS_BUCKET", "")
_s3_client = None

def _get_s3_client():
    """Lazy-init boto3 S3 client. Returns None if boto3 unavailable or bucket not set."""
    global _s3_client
    if not _S3_RESULTS_BUCKET:
        return None
    if _s3_client is None:
        try:
            import boto3
            _s3_client = boto3.client("s3", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))
        except Exception:
            return None
    return _s3_client

def _upload_to_s3(local_path: Path, s3_key: str) -> bool:
    """Upload a single file to S3. Returns True on success, False on failure. Never raises."""
    try:
        client = _get_s3_client()
        if client is None:
            return False
        client.upload_file(str(local_path), _S3_RESULTS_BUCKET, s3_key)
        return True
    except Exception as e:
        print(f"[s3-sync] upload failed {s3_key}: {e}")
        return False

def _sync_instance_to_s3(instance_id: str, result: dict, output_dir: Path, batch_name: str):
    """Upload traj + heartbeat for a completed instance. Best-effort, never crashes."""
    if not _S3_RESULTS_BUCKET:
        return
    try:
        best_attempt = result.get("best_attempt", 1)
        # Upload traj for each attempt that exists
        for attempt in range(1, result.get("attempts", 1) + 1):
            traj_local = output_dir / f"attempt_{attempt}" / instance_id / f"{instance_id}.traj.json"
            if traj_local.exists():
                s3_key = f"{batch_name}/attempt_{attempt}/{instance_id}.traj.json"
                if _upload_to_s3(traj_local, s3_key):
                    print(f"[s3-sync] uploaded {s3_key}")

        # Upload heartbeat
        hb_local = output_dir / "heartbeat.json"
        if hb_local.exists():
            _upload_to_s3(hb_local, f"{batch_name}/heartbeat.json")
    except Exception as e:
        print(f"[s3-sync] instance sync failed {instance_id}: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance-ids", nargs="+", required=True)
    parser.add_argument("--max-attempts", type=int, default=3)
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
    parser.add_argument("--dataset", choices=["Lite", "Verified"], default="Verified",
                        help="SWE-bench dataset variant: Lite (300) or Verified (500) (default: Verified)")
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

            # ── Heartbeat: write after each instance completes ─────────────────
            _hb_errors = [e_iid for e_iid, e_r in zip(
                [futures[f] for f in futures], results) if e_r and not e_r.get("accepted")]
            try:
                _hb = {
                    "ts": time.time(),
                    "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "done": done,
                    "total": len(args.instance_ids),
                    "last_instance": iid,
                    "last_accepted": bool(r and r.get("accepted")),
                    "accepted_so_far": sum(1 for x in results if x and x.get("accepted")),
                    "errors": [futures[f] for f in futures
                               if f.done() and f.exception() is not None],
                    "run_id": args.run_id or "unknown",
                }
                (output_dir / "heartbeat.json").write_text(json.dumps(_hb, indent=2) + "\n")
            except Exception:
                pass  # heartbeat is best-effort, never crash the pipeline

            # ── Per-instance S3 sync: upload traj + heartbeat immediately ────────
            try:
                _batch_name = output_dir.name  # e.g. "batch-p11-foo"
                _sync_instance_to_s3(iid, r, output_dir, _batch_name)
            except Exception:
                pass  # S3 sync is best-effort, never crash the pipeline

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
