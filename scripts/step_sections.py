"""step_sections — extracted step-level section functions.

p225-05: These functions were extracted verbatim from run_with_jingu_gate.py
to enable independent testing and gradual migration to JinguAgent hooks.

Functions:
    _step_observe          — Section 1: pure observation
    _step_verify_if_needed — Section 2: patch signal + inner-verify
    _step_cp_update_and_verdict — Section 3: control-plane update + verdict
    _step_check_structure  — Section 4: per-step structure validation
    _step_inject_phase     — Section 5: phase prefix injection
    _check_materialization_gate — Section 6: EXECUTE liveness gate
    PHASE_REQUIRED_FIELDS  — per-phase required field definitions
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from step_monitor_state import StepMonitorState

# ---------------------------------------------------------------------------
# Imports shared across sections — lazy-imported in original, kept lazy here.
# Only import what is needed at module level; heavy deps stay inside functions.
# ---------------------------------------------------------------------------
from signal_extraction import _msg_has_env_mutation, _msg_has_signal
from control.reasoning_state import (
    decide_next,
    VerdictStop, VerdictRedirect, VerdictAdvance, VerdictContinue,
)
from control.swe_signal_adapter import extract_weak_progress
from gate_rejection import SDG_ENABLED as _SDG_ENABLED, build_repair_from_rejection as _build_sdg_repair
from failure_routing import route_failure as route_failure_p216, is_data_driven_routing_enabled
from strategy_prompts import get_strategy_prompt
from step_monitor_state import StopExecution
from declaration_extractor import build_phase_record_from_structured


# ── PR3: limit event unification helper ──────────────────────────────────────

def _emit_limit_triggered(
    state: "StepMonitorState",
    *,
    step_n: int,
    limit_name: str,
    configured_value: int | float,
    actual_value: int | float,
    action_taken: str,
    source_file: str,
    source_line: int,
    reason: str = "",
) -> None:
    """Emit to BOTH stdout ([limit-triggered] prefix) and decisions.jsonl."""
    print(
        f"    [limit-triggered] {limit_name}: configured={configured_value}"
        f" actual={actual_value} action={action_taken}"
        f" source={source_file}:{source_line}"
        f" reason={reason}",
        flush=True,
    )
    _emit_decision(
        state,
        decision_type="limit_triggered",
        step_n=step_n,
        verdict=action_taken,
        reason=f"{limit_name}: configured={configured_value} actual={actual_value} -- {reason}",
        signals={
            "limit_name": limit_name,
            "configured_value": configured_value,
            "actual_value": actual_value,
            "action_taken": action_taken,
            "source_file": source_file,
            "source_line": source_line,
        },
    )


# ── p230: decision provenance emission helper ────────────────────────────────

def _emit_decision(
    state: "StepMonitorState",
    *,
    decision_type: str,
    step_n: int,
    verdict: str,
    reason: str = "",
    rule_violated: str | None = None,
    signals: dict | None = None,
    phase_from: str | None = None,
    phase_to: str | None = None,
) -> None:
    """Emit a DecisionEvent to the attempt's decisions.jsonl. Never raises."""
    logger = getattr(state, "_decision_logger", None)
    if not logger:
        return
    try:
        from decision_logger import DecisionEvent
        logger.log(DecisionEvent(
            decision_type=decision_type,
            step_n=step_n,
            timestamp_ms=time.time() * 1000,
            verdict=verdict,
            rule_violated=rule_violated,
            signals_evaluated=signals,
            reason_text=reason,
            phase_from=phase_from,
            phase_to=phase_to,
        ))
    except Exception:
        pass  # logging must not crash the run


# ── Per-step structure validation (p207-P2) ──────────────────────────────────
# Required structured fields per phase, derived from phase_prompt.py templates.
PHASE_REQUIRED_FIELDS: dict[str, list[str]] = {
    "UNDERSTAND": ["PROBLEM_STATEMENT", "EXPECTED_BEHAVIOR", "ACTUAL_BEHAVIOR", "SCOPE"],
    "OBSERVE":    ["EVIDENCE"],
    # ANALYZE: removed — agent submits via submit_phase_record tool call, not text
    # sections. The analysis_gate checks the tool call fields (root_cause,
    # causal_chain). Checking for text markers here actively conflicts with the
    # tool-call path by injecting "write ROOT_CAUSE:" hints.
    "DECIDE":     ["OPTIONS", "SELECTED", "CONSTRAINTS"],
    "EXECUTE":    ["PLAN", "CHANGE_SCOPE"],
    "JUDGE":      ["VERDICT", "TEST_RESULTS", "CONFIDENCE"],
}


# ═══════════════════════════════════════════════════════════════════════════════
# Section 1: _step_observe
# ═══════════════════════════════════════════════════════════════════════════════

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
            # Plan-C: skip structured_extract traj entries
            if msg.get("extra", {}).get("type", "").startswith("structured_extract_"):
                continue
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

    _state_ref = getattr(agent_self, "_jingu_monitor_state", None)
    if _state_ref is not None and latest_assistant_text:
        if latest_assistant_text != _state_ref._last_assistant_text:
            _state_ref._llm_step += 1
            _state_ref._last_assistant_text = latest_assistant_text
            _state_ref._observe_tool_signal = False

    if _state_ref is not None:
        for _msg in reversed(agent_self.messages):
            if _msg.get("role") == "assistant":
                # Plan-C: skip structured_extract traj entries
                if _msg.get("extra", {}).get("type", "").startswith("structured_extract_"):
                    continue
                _tcs = _msg.get("tool_calls", [])
                if _tcs:
                    _state_ref._observe_tool_signal = True
                elif _msg.get("extra", {}).get("actions"):
                    _state_ref._observe_tool_signal = True
                break

    print(f"    [step {step_n}] ${agent_self.cost:.2f}  {snippet}", flush=True)

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

    env_error_detected = False
    for msg in reversed(agent_self.messages):
        if msg.get("role") == "assistant":
            # Plan-C: skip structured_extract traj entries
            if msg.get("extra", {}).get("type", "").startswith("structured_extract_"):
                continue
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


# ═══════════════════════════════════════════════════════════════════════════════
# Section 2: _step_verify_if_needed
# ═══════════════════════════════════════════════════════════════════════════════

def _step_verify_if_needed(
    agent_self,
    *,
    state: "StepMonitorState",
    verify_debounce_s: float,
    cp_state_holder: list | None = None,
) -> bool:
    """
    Section 2: patch signal detection + conditional quick judge dispatch.

    Returns step_patch_non_empty (True if agent has a real, non-empty patch).
    Side-effects: may run a synchronous quick judge (max 30s) and set
    state._pending_quick_judge_message for the caller to inject.
    """
    import hashlib as _hl
    import subprocess as _sp_iv

    step_patch_non_empty = False
    for msg in reversed(agent_self.messages):
        if msg.get("role") == "assistant":
            # Plan-C: skip structured_extract traj entries
            if msg.get("extra", {}).get("type", "").startswith("structured_extract_"):
                continue
            if not _msg_has_signal(msg):
                break
            step_patch_non_empty = True
            cid = state.container_id
            if not cid:
                break
            _base_commit = state.instance.get("base_commit", "HEAD")
            _git_diff_result = _sp_iv.run(
                ["docker", "exec", "-w", "/testbed", cid, "git", "diff", _base_commit],
                capture_output=True, text=True, timeout=30,
            )
            _raw_diff = _git_diff_result.stdout if _git_diff_result.returncode == 0 else ""
            current_patch = (_raw_diff.strip() + "\n") if _raw_diff.strip() else ""
            if not current_patch:
                step_patch_non_empty = False
                break

            # E1: Quick Judge — target-aware corrective signal
            patch_hash = _hl.md5(current_patch.encode()).hexdigest()[:16]
            # Pass real phase from cp_state_holder (state.cp_state may be stale)
            _real_phase = None
            if cp_state_holder:
                _real_phase = getattr(cp_state_holder[0], 'phase', None)
            if state.should_trigger_quick_judge(patch_hash, current_phase=_real_phase):
                try:
                    from quick_judge import run_quick_judge, format_agent_message, QuickJudgeResult

                    # Extract changed files from patch
                    changed_files = [
                        line[6:].strip()
                        for line in current_patch.splitlines()
                        if line.startswith("+++ b/")
                    ]

                    # Reconstruct previous result for direction comparison
                    prev_result = None
                    if state.quick_judge_history:
                        prev = state.quick_judge_history[-1]
                        prev_result = QuickJudgeResult(
                            step=prev.get("step", 0),
                            target_test_id=prev.get("target_test_id", ""),
                            target_status=prev.get("target_status", "unknown"),
                            tests_targeted=prev.get("tests_targeted", 0),
                            tests_passed=prev.get("tests_passed", 0),
                            tests_failed=prev.get("tests_failed", 0),
                            tests_error=prev.get("tests_error", 0),
                            failing_test_names=prev.get("failing_test_names", []),
                            elapsed_ms=prev.get("elapsed_ms", 0),
                            direction=prev.get("direction", "first_signal"),
                        )

                    # Canonical step identity: use state._llm_step (monotonic per step)
                    _canonical_step = state._llm_step
                    print(
                        f"    [quick-judge] triggering at step={_canonical_step} "
                        f"(patch changed, container={cid[:12]}...)",
                        flush=True,
                    )

                    qj_result = run_quick_judge(
                        patch=current_patch,
                        instance=state.instance,
                        container_id=cid,
                        changed_files=changed_files,
                        previous_result=prev_result,
                        step=_canonical_step,
                    )

                    # Record in telemetry (target-aware + sentinel fields)
                    state.record_quick_judge(_canonical_step, {
                        "step": _canonical_step,
                        "tier": "quick",
                        "trigger_source": "automatic_patch_detected",
                        "target_test_id": qj_result.target_test_id,
                        "target_status": qj_result.target_status,
                        "signal_kind": qj_result.signal_kind,
                        "corrective": qj_result.corrective,
                        "command_scope": qj_result.command_scope,
                        "tests_targeted": qj_result.tests_targeted,
                        "tests_passed": qj_result.tests_passed,
                        "tests_failed": qj_result.tests_failed,
                        "tests_error": qj_result.tests_error,
                        "failing_test_names": qj_result.failing_test_names,
                        "elapsed_ms": qj_result.elapsed_ms,
                        "direction": qj_result.direction,
                        "patch_hash": patch_hash,
                        "invoked": True,
                        "acknowledged": None,
                        "effective": None,
                        "sentinel_tests_run": qj_result.sentinel_tests_run,
                        "sentinel_tests_passed": qj_result.sentinel_tests_passed,
                        "sentinel_tests_failed": qj_result.sentinel_tests_failed,
                        "regression_detected": qj_result.regression_detected,
                        "regression_test_names": qj_result.regression_test_names,
                        "f2p_targeted": qj_result.f2p_targeted,
                        "f2p_passed": qj_result.f2p_passed,
                        "f2p_failed": qj_result.f2p_failed,
                        "f2p_coverage": qj_result.f2p_coverage,
                    })

                    # Format message for agent injection (consumed by jingu_agent.py)
                    state._pending_quick_judge_message = format_agent_message(qj_result)

                except Exception as _qj_exc:
                    print(f"    [quick-judge] ERROR (non-fatal): {_qj_exc}", flush=True)

            break

    return step_patch_non_empty


# ═══════════════════════════════════════════════════════════════════════════════
# Section 3: _step_cp_update_and_verdict
# ═══════════════════════════════════════════════════════════════════════════════

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

    # ══════════════════════════════════════════════════════════════════
    # Phase Submission Enforcement — Checkpoint Escalation
    #
    # Three-level escalation for agents that don't call submit_phase_record:
    #   Level 1 (soft, step >= 8):  reminder message
    #   Level 2 (hard, step >= 15): warning message + force armed
    #   Level 3 (terminal):        handled by protocol_violation_stop (existing)
    #
    # Only applies to phases with schemas (OBSERVE, ANALYZE, DECIDE, DESIGN, EXECUTE, JUDGE).
    # Resets when agent submits a phase record or phase changes.
    # ══════════════════════════════════════════════════════════════════
    _current_phase_str = str(_cp_s.phase).upper()
    _CHECKPOINT_SOFT = 8    # steps before soft reminder
    _CHECKPOINT_HARD = 15   # steps before hard warning + force

    # Detect phase change → reset counter
    if _current_phase_str != state._last_submission_phase:
        state._steps_without_submission = 0
        state._submission_escalation_level = 0
        state._last_submission_phase = _current_phase_str

    # Check if agent submitted a phase record this step (peek, don't consume)
    _model_peek = getattr(agent_self, "model", None)
    _has_pending_submission = (
        _model_peek is not None
        and hasattr(_model_peek, "_submitted_phase_record")
        and _model_peek._submitted_phase_record is not None
    )
    if _has_pending_submission:
        state._steps_without_submission = 0
        state._submission_escalation_level = 0
    else:
        state._steps_without_submission += 1

    # Escalation logic
    if (
        state._steps_without_submission >= _CHECKPOINT_HARD
        and state._submission_escalation_level < 2
        and _current_phase_str not in ("UNDERSTAND",)
    ):
        state._submission_escalation_level = 2
        agent_self.messages.append({
            "role": "user",
            "content": (
                f"[PHASE CHECKPOINT — HARD WARNING]\n"
                f"You have been in {_current_phase_str} for {state._steps_without_submission} steps "
                f"without submitting a phase record.\n"
                f"The system REQUIRES you to call submit_phase_record to complete this phase.\n"
                f"Your next response MUST include a submit_phase_record call with your "
                f"{_current_phase_str} findings. You will not be able to use other tools "
                f"until you submit."
            ),
        })
        if _model_peek is not None and hasattr(_model_peek, "set_force_phase_record"):
            _model_peek.set_force_phase_record(True)
            state._phase_record_force_total += 1
        print(
            f"    [phase_submission_enforcement] CHECKPOINT HARD:"
            f" phase={_current_phase_str} steps={state._steps_without_submission}"
            f" → force armed + warning injected",
            flush=True,
        )
    elif (
        state._steps_without_submission >= _CHECKPOINT_SOFT
        and state._submission_escalation_level < 1
        and _current_phase_str not in ("UNDERSTAND",)
    ):
        state._submission_escalation_level = 1
        agent_self.messages.append({
            "role": "user",
            "content": (
                f"[PHASE CHECKPOINT — REMINDER]\n"
                f"You have been in {_current_phase_str} for {state._steps_without_submission} steps. "
                f"Remember to call submit_phase_record when you have enough findings. "
                f"Phase completion requires a submitted record."
            ),
        })
        print(
            f"    [phase_submission_enforcement] CHECKPOINT SOFT:"
            f" phase={_current_phase_str} steps={state._steps_without_submission}"
            f" → reminder injected",
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
        _emit_decision(
            state, decision_type="gate_verdict", step_n=_cp_s.step_index,
            verdict="stop", reason=getattr(_step_verdict, "reason", ""),
        )
        print(
            f"    [cp] VerdictStop enforcement: raising StopExecution({_step_verdict.reason})"
            f" — immediate interrupt, no phase injection",
            flush=True,
        )
        raise StopExecution(_step_verdict.reason)

    elif isinstance(_step_verdict, VerdictRedirect):
        _emit_decision(
            state, decision_type="gate_verdict", step_n=_cp_s.step_index,
            verdict="redirect", reason=getattr(_step_verdict, "reason", ""),
            phase_to=getattr(_step_verdict, "to", None),
        )
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
                _emit_limit_triggered(
                    state, step_n=_cp_s.step_index,
                    limit_name="execute_redirect_limit",
                    configured_value=_EXECUTE_REDIRECT_LIMIT, actual_value=_exec_redirect_count,
                    action_taken="stop", source_file="step_sections.py", source_line=379,
                    reason="execute_no_progress_loop_exceeded",
                )
                _emit_decision(
                    state, decision_type="gate_verdict", step_n=_cp_s.step_index,
                    verdict="stop", reason="execute_no_progress_loop_exceeded",
                    signals={"redirect_count": _exec_redirect_count, "limit": _EXECUTE_REDIRECT_LIMIT},
                )
                print(
                    f"    [cp] execute_no_progress loop exceeded limit={_EXECUTE_REDIRECT_LIMIT}"
                    f" → VerdictStop(no_signal) [attempt-terminal, will retry]",
                    flush=True,
                )
                state.early_stop_verdict = VerdictStop(reason="no_signal")
                raise StopExecution("no_signal")
        else:
            _exec_key = ("EXECUTE", "execute_no_progress")
            state._retryable_loop_counts[_exec_key] = 0

        state.pending_redirect_hint = f"[REDIRECT:{_step_verdict.to}] {_step_verdict.reason}"
        # Phase Submission Enforcement: enriched redirect with cognition context
        _redirect_content = ""
        if _step_verdict.reason == "execute_no_progress":
            _last_rc = state.last_analyze_root_cause or ""
            _rc_hint = ""
            if _last_rc:
                _rc_hint = (
                    f" Your analysis identified: \"{_last_rc[:200]}\". "
                    f"Edit the specific file/function from your analysis directly. "
                    f"If your analysis was wrong, return to ANALYZE with new evidence."
                )
            _exec_steps = state._steps_without_submission
            _redirect_content = (
                f"[EXECUTE STALL — NO PROGRESS]\n"
                f"You have spent {_exec_steps} steps in EXECUTE without writing any file changes.\n"
                f"You must either:\n"
                f"1. Write a code change NOW (use the bash tool to edit a file)\n"
                f"2. If you don't know what to change, return to ANALYZE{_rc_hint}"
            )
        else:
            _redirect_content = (
                f"[Control-plane redirect: {_step_verdict.reason}] "
                f"Re-examine your environment assumptions. "
                f"Transition to phase {_step_verdict.to} before patching."
            )
        agent_self.messages.append({
            "role": "user",
            "content": _redirect_content,
        })
        state.pending_redirect_hint = ""

    elif isinstance(_step_verdict, VerdictAdvance):
        _emit_decision(
            state, decision_type="gate_verdict", step_n=_cp_s.step_index,
            verdict="advance", reason=getattr(_step_verdict, "reason", ""),
            phase_to=getattr(_step_verdict, "to", None),
        )
        _old_phase = _cp_s.phase
        # Plan-A: defer phase advance until after all gates pass.
        # _new_phase is computed but NOT applied yet. Applied at bottom of handler.
        _new_phase = _step_verdict.to
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
        _emit_decision(
            state, decision_type="phase_advance", step_n=_cp_s.step_index,
            verdict="advance", phase_from=str(_old_phase), phase_to=str(_step_verdict.to),
        )

        _eval_phase = str(_old_phase).upper()

        # ══════════════════════════════════════════════════════════════════
        # Plan-B STRONG: Phase Record Acquisition
        #
        # Contract:
        #   1. Phase completion = admitted PhaseRecord exists (tool_submitted ONLY)
        #   2. No transition without admitted record
        #   3. Fallback = diagnostic only, never admission
        #
        # Admitted sources: tool_submitted (only)
        # Diagnostic sources: structured_extract, regex (separate storage)
        # ══════════════════════════════════════════════════════════════════

        _pr = None  # admitted phase record — ONLY from tool submission
        _pr_source = "none"
        _pr_foreign_phase = ""
        _diagnostic_pr = None  # diagnostic-only, never used for admission/gate/routing
        try:
            # ── Step 1: Check for tool-submitted phase record (ONLY admitted path) ──
            _model = getattr(agent_self, "model", None)
            _tool_submitted = None
            if _model is not None and hasattr(_model, "pop_submitted_phase_record"):
                _tool_submitted = _model.pop_submitted_phase_record()

            if _tool_submitted is not None:
                _pr = build_phase_record_from_structured(
                    _tool_submitted, str(_old_phase)
                )
                state.phase_records.append(_pr)
                _pr_source = "tool_submitted"
                if hasattr(state, "_extraction_tool_submitted"):
                    state._extraction_tool_submitted += 1
                _declared_phase = (_tool_submitted.get("phase") or "").upper()
                _foreign = bool(_declared_phase and _declared_phase != _eval_phase)
                print(
                    f"    [phase_record] extraction_method=tool_submitted"
                    f" fields={list(_tool_submitted.keys())}"
                    f" admitted=true",
                    flush=True,
                )
                if _foreign:
                    _pr_foreign_phase = _declared_phase
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
                        f" alignment={_align} delta={_align_delta}",
                        flush=True,
                    )
            # _pr is None here = agent did NOT call submit_phase_record.
            # No cache, no fallback extraction can produce an admitted record.

            # ── Step 2: Diagnostic extraction (telemetry ONLY, never admission) ──
            # Runs regardless of tool submission — for drift analysis.
            # Stored separately in state.diagnostic_phase_records, NOT in phase_records.
            _accumulated = state._phase_accumulated_text.get(_eval_phase, "")
            _extract_text = _accumulated if _accumulated.strip() else latest_assistant_text
            _structured_parsed = None
            _extraction_schema = None
            try:
                from jingu_onboard import onboard as _onboard_fn
                _gov = _onboard_fn()
                _extraction_schema = _gov.get_constrained_schema(_eval_phase)
                _phase_hint = ""
                try:
                    _cog = _gov.get_cognition(_eval_phase)
                    if _cog and _cog.success_criteria:
                        _phase_hint = "; ".join(_cog.success_criteria)
                except Exception:
                    pass
                if _extraction_schema is not None and _model is not None:
                    if hasattr(_model, "structured_extract"):
                        _structured_parsed = _model.structured_extract(
                            accumulated_text=_extract_text,
                            phase=_eval_phase,
                            schema=_extraction_schema,
                            phase_hint=_phase_hint,
                        )
            except Exception as _se_exc:
                print(
                    f"    [diagnostic] structured_extract error (non-fatal): {_se_exc}",
                    flush=True,
                )

            # Plan-C: record structured_extract call in traj for observability
            _extract_rec = getattr(_model, "_last_extract_record", None) if _model else None
            if _extract_rec is not None:
                try:
                    agent_self.messages.append({
                        "role": "user",
                        "content": _extract_rec.extraction_prompt,
                        "extra": {
                            "type": "structured_extract_request",
                            "phase": _eval_phase,
                            "schema_name": _extract_rec.schema_name,
                            "accumulated_text_chars": len(_accumulated) if _accumulated else 0,
                            "phase_hint": _extract_rec.phase_hint or "",
                            "timestamp": _extract_rec.timestamp_request,
                        },
                    })
                    agent_self.messages.append({
                        "role": "assistant",
                        "content": _extract_rec.response_raw or "",
                        "extra": {
                            "type": "structured_extract_response",
                            "phase": _eval_phase,
                            "success": _extract_rec.success,
                            "fields": list((_extract_rec.response_parsed or {}).keys()),
                            "cost": _extract_rec.cost,
                            "error": _extract_rec.error,
                            "timestamp": _extract_rec.timestamp_response,
                        },
                    })
                except Exception as _traj_exc:
                    print(f"    [Plan-C] traj recording error (non-fatal): {_traj_exc}", flush=True)

            if _structured_parsed is not None:
                from declaration_extractor import extract_record_for_phase as _extract_for_phase
                _diagnostic_pr = build_phase_record_from_structured(
                    _structured_parsed, str(_old_phase)
                )
                if hasattr(state, "_extraction_structured"):
                    state._extraction_structured += 1
                _acc_len = len(_accumulated) if _accumulated else 0
                print(
                    f"    [diagnostic] method=structured"
                    f" accumulated_chars={_acc_len}"
                    f" fields={list(_structured_parsed.keys())}"
                    f" DIAGNOSTIC_ONLY=true",
                    flush=True,
                )
            else:
                try:
                    from declaration_extractor import extract_record_for_phase as _extract_for_phase
                    _diag_pr, _diag_declared, _diag_foreign = _extract_for_phase(
                        _extract_text, str(_old_phase)
                    )
                    _diagnostic_pr = _diag_pr
                except Exception:
                    pass
                if hasattr(state, "_extraction_no_schema") and _extraction_schema is None:
                    state._extraction_no_schema += 1
                elif hasattr(state, "_extraction_regex_fallback"):
                    state._extraction_regex_fallback += 1
                _acc_len = len(_accumulated) if _accumulated else 0
                print(
                    f"    [diagnostic] method=regex"
                    f" accumulated_chars={_acc_len}"
                    f" DIAGNOSTIC_ONLY=true",
                    flush=True,
                )

            # Store diagnostic record in SEPARATE list (never in phase_records)
            if _diagnostic_pr is not None:
                if not hasattr(state, "diagnostic_phase_records"):
                    state.diagnostic_phase_records = []
                state.diagnostic_phase_records.append(_diagnostic_pr)

            # ── Log summary ──
            if _pr is not None:
                print(
                    f"    [phase_record] eval_phase={_eval_phase}"
                    f" record_phase={_pr.phase} source={_pr_source}"
                    f" subtype={_pr.subtype} principals={_pr.principals}"
                    f" evidence_refs={_pr.evidence_refs}"
                    f" admitted=true",
                    flush=True,
                )
            else:
                print(
                    f"    [phase_record] eval_phase={_eval_phase}"
                    f" source=none admitted=false"
                    f" diagnostic_available={_diagnostic_pr is not None}"
                    f" PROTOCOL_VIOLATION=missing_phase_record",
                    flush=True,
                )

        except Exception as _pr_exc:
            print(f"    [phase_record] error (non-fatal): {_pr_exc}", flush=True)

        # ══════════════════════════════════════════════════════════════════
        # Plan-B STRONG: Phase Completion Gate — HARD BLOCK
        #
        # No admitted PhaseRecord → phase NOT complete → transition BLOCKED.
        # Agent gets retries to call submit_phase_record. After max retries,
        # phase is STILL not complete — protocol violation emitted,
        # transition remains blocked (no FORCE_ADVANCE).
        # ══════════════════════════════════════════════════════════════════
        _MAX_SUBMISSION_RETRIES = 2
        _extraction_gated = False  # True = transition blocked
        if _pr is None:
            _extraction_gated = True  # ALWAYS blocked when no admitted record
            _ext_key = _eval_phase
            _ext_retries = state.extraction_retry_counts.get(_ext_key, 0)
            state.extraction_retry_counts[_ext_key] = _ext_retries + 1

            # Track missing submissions for telemetry
            if hasattr(state, "_missing_submission_count"):
                state._missing_submission_count += 1

            if _ext_retries < _MAX_SUBMISSION_RETRIES:
                _emit_limit_triggered(
                    state, step_n=_cp_s.step_index,
                    limit_name="phase_record_submission_retry",
                    configured_value=_MAX_SUBMISSION_RETRIES,
                    actual_value=_ext_retries + 1,
                    action_taken="block_transition_retry",
                    source_file="step_sections.py",
                    source_line=640,
                    reason=f"protocol_violation: no submit_phase_record for {_eval_phase} ({_ext_retries + 1}/{_MAX_SUBMISSION_RETRIES})",
                )
                agent_self.messages.append({
                    "role": "user",
                    "content": (
                        f"[PROTOCOL VIOLATION: PHASE RECORD REQUIRED]\n"
                        f"Phase {_eval_phase} cannot be completed without calling submit_phase_record.\n"
                        f"This is not optional. The system CANNOT proceed to the next phase.\n"
                        f"Call submit_phase_record now with your {_eval_phase} findings.\n"
                        f"Retry {_ext_retries + 1}/{_MAX_SUBMISSION_RETRIES}."
                    ),
                })
                # Phase Submission Enforcement: force tool_choice on retry
                _model_force = getattr(agent_self, "model", None)
                if _model_force is not None and hasattr(_model_force, "set_force_phase_record"):
                    _model_force.set_force_phase_record(True)
                    print(
                        f"    [phase_submission_enforcement] ARMED:"
                        f" forcing submit_phase_record on next query"
                        f" (retry {_ext_retries + 1}/{_MAX_SUBMISSION_RETRIES})",
                        flush=True,
                    )
                print(
                    f"    [phase_gate] BLOCKED: no admitted record for {_eval_phase}"
                    f" retry={_ext_retries + 1}/{_MAX_SUBMISSION_RETRIES}"
                    f" — transition DENIED, staying in current phase",
                    flush=True,
                )
            else:
                # Max retries exhausted. Phase is STILL blocked — no force advance.
                # Emit protocol violation and terminate this attempt.
                _emit_limit_triggered(
                    state, step_n=_cp_s.step_index,
                    limit_name="phase_record_submission_exhausted",
                    configured_value=_MAX_SUBMISSION_RETRIES,
                    actual_value=_ext_retries + 1,
                    action_taken="protocol_violation_stop",
                    source_file="step_sections.py",
                    source_line=640,
                    reason=f"protocol_violation: agent never called submit_phase_record for {_eval_phase} after {_ext_retries + 1} attempts",
                )
                _emit_decision(
                    state, decision_type="gate_verdict", step_n=_cp_s.step_index,
                    verdict="stop", reason="protocol_violation_missing_phase_record",
                    signals={"phase": _eval_phase, "retries": _ext_retries + 1},
                )
                print(
                    f"    [phase_gate] PROTOCOL VIOLATION: agent never submitted"
                    f" phase record for {_eval_phase} after {_ext_retries + 1} retries"
                    f" — stopping attempt (no force advance)",
                    flush=True,
                )
                state.early_stop_verdict = VerdictStop(
                    reason="protocol_violation_missing_phase_record",
                )
                raise StopExecution("protocol_violation_missing_phase_record")

        # p222: Cognition validation
        _cognition_rejected = False
        if _pr is not None and not _extraction_gated:
            try:
                from cognition_prompts import COGNITION_EXECUTION_ENABLED as _COG_ENABLED
                if _COG_ENABLED:
                    from cognition_prompts import CognitionLoader as _CogLoader
                    from phase_validator import (
                        validate_phase_record as _validate_pr,
                        build_validation_feedback as _build_cog_feedback,
                    )
                    import json as _json_cog
                    import os as _os_cog
                    from pathlib import Path as _Path_cog
                    _bundle_path_cog = _os_cog.environ.get(
                        "JINGU_BUNDLE_PATH",
                        str(_Path_cog(__file__).parent.parent / "bundle.json"),
                    )
                    with open(_bundle_path_cog) as _f_cog:
                        _cog_bundle = _json_cog.load(_f_cog)
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
                        # Plan-A: no rollback needed — phase was never advanced
                        _step_verdict = VerdictContinue(reason="cognition_validation_failed")
                        _emit_decision(
                            state, decision_type="gate_verdict", step_n=_cp_s.step_index,
                            verdict="continue", reason="cognition_validation_failed",
                        )
                        # Inject feedback for next step
                        agent_self.messages.append({
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

        # p211: Analysis gate
        _analysis_gate_rejected = False
        _analysis_gate_force_passed = False
        _AG_MAX_REJECTS = 2
        if _eval_phase == "ANALYZE" and _pr is not None and not _cognition_rejected and not _extraction_gated:
            try:
                from analysis_gate import evaluate_analysis as _eval_analysis
                _analysis_verdict = _eval_analysis(
                    _pr,
                    structured_output=(_pr_source == "tool_submitted"),
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
                    # Phase Submission Enforcement (p14): redirect to OBSERVE
                    # instead of force_pass. Agent must gather more evidence.
                    _AG_OBSERVE_REDIRECT_MAX = 2
                    _ag_obs_redirects = state._analysis_observe_redirects
                    if _ag_obs_redirects < _AG_OBSERVE_REDIRECT_MAX:
                        state._analysis_observe_redirects = _ag_obs_redirects + 1
                        _missing_rules = _analysis_verdict.failed_rules or []
                        _missing_str = ", ".join(_missing_rules) if _missing_rules else "quality checks"
                        agent_self.messages.append({
                            "role": "user",
                            "content": (
                                f"[ANALYSIS INCOMPLETE — REDIRECT TO OBSERVE]\n"
                                f"Your analysis has been rejected {_ag_reject_count} times. "
                                f"Failed checks: {_missing_str}.\n"
                                f"Return to OBSERVE and gather more evidence. Specifically:\n"
                                + "\n".join(f"- Investigate: {r}" for r in _missing_rules)
                                + f"\n\nThen re-enter ANALYZE with stronger evidence."
                            ),
                        })
                        # Reset phase to OBSERVE
                        import dataclasses as _dc_ag
                        _cp_ref_ag = cp_state_holder[0] if cp_state_holder else state.cp_state
                        _cp_new_ag = _dc_ag.replace(
                            _cp_ref_ag, phase="OBSERVE", no_progress_steps=0
                        )
                        if cp_state_holder:
                            cp_state_holder[0] = _cp_new_ag
                        else:
                            state.cp_state = _cp_new_ag
                        state._execute_entry_step = -1
                        state.phase_records = [
                            r for r in state.phase_records
                            if r.phase.upper() != _eval_phase
                        ]
                        _analysis_gate_rejected = True
                        _emit_limit_triggered(
                            state, step_n=_cp_s.step_index,
                            limit_name="analysis_gate_redirect_observe",
                            configured_value=_AG_OBSERVE_REDIRECT_MAX, actual_value=_ag_obs_redirects + 1,
                            action_taken="redirect_observe", source_file="step_sections.py", source_line=653,
                            reason=f"failed_rules={_analysis_verdict.failed_rules} scores={_analysis_verdict.scores}",
                        )
                        print(
                            f"    [analysis_gate] REDIRECT TO OBSERVE"
                            f" ({_ag_obs_redirects + 1}/{_AG_OBSERVE_REDIRECT_MAX})"
                            f" — failed_rules={_missing_rules}",
                            flush=True,
                        )
                    else:
                        # Max observe redirects exhausted — force_pass with warning
                        print(
                            f"    [analysis_gate] FORCE_PASS — max_observe_redirects="
                            f"{_AG_OBSERVE_REDIRECT_MAX} exhausted, allowing advance with warning",
                            flush=True,
                        )
                        _emit_limit_triggered(
                            state, step_n=_cp_s.step_index,
                            limit_name="analysis_gate_force_pass",
                            configured_value=_AG_MAX_REJECTS, actual_value=_ag_reject_count,
                            action_taken="force_pass_after_observe_redirects",
                            source_file="step_sections.py", source_line=653,
                            reason=f"failed_rules={_analysis_verdict.failed_rules} scores={_analysis_verdict.scores} observe_redirects={_ag_obs_redirects}",
                        )
                        _analysis_gate_force_passed = True
                elif not _analysis_verdict.passed:
                    _analysis_gate_rejected = True
                    # Plan-A: no rollback needed — phase was never advanced
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
                        _ag_scores = _analysis_verdict.scores
                        _ag_pass = 0.5
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
                    state.phase_records = [
                        r for r in state.phase_records
                        if r.phase.upper() != _eval_phase
                    ]
                    state.analysis_gate_rejects += 1
                    print(f"    [analysis_gate] REJECT ({state.analysis_gate_rejects}/{_AG_MAX_REJECTS}) — redirecting to ANALYZE", flush=True)
            except Exception as _ag_exc:
                print(f"    [analysis_gate] error (non-fatal): {_ag_exc}", flush=True)

        # Design gate
        _design_gate_rejected = False
        _design_gate_force_passed = False
        _DG_MAX_REJECTS = 2
        if _eval_phase == "DESIGN" and _pr is not None and not _cognition_rejected and not _extraction_gated:
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
                    _emit_limit_triggered(
                        state, step_n=_cp_s.step_index,
                        limit_name="design_gate_force_pass",
                        configured_value=_DG_MAX_REJECTS, actual_value=_dg_reject_count,
                        action_taken="force_pass", source_file="step_sections.py", source_line=729,
                        reason=f"failed_rules={_design_verdict.failed_rules} scores={_design_verdict.scores}",
                    )
                    _design_gate_force_passed = True
                elif not _design_verdict.passed:
                    _design_gate_rejected = True
                    # Plan-A: no rollback needed — phase was never advanced
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

        _pg_retryable_no_bypass = False  # Plan-A: tracks if principal gate RETRYABLE redirected phase
        try:
            if _extraction_gated:
                raise RuntimeError("extraction_gate blocked, skipping principal gate")
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
            _obs_tool_sig = getattr(getattr(agent_self, "_jingu_monitor_state", None), "_observe_tool_signal", False)
            if _eval_phase == "ANALYZE" and _pr is not None:
                _rc = getattr(_pr, "root_cause", "") or ""
                if _rc:
                    state.last_analyze_root_cause = _rc
                    print(f"    [phase_record] root_cause saved ({len(_rc)} chars)", flush=True)
            _admission = _eval_admission(
                _pr, _eval_phase,
                observe_tool_signal=_obs_tool_sig,
                last_analyze_root_cause=state.last_analyze_root_cause if _eval_phase == "EXECUTE" else "",
                structured_output=(_pr_source == "tool_submitted"),
            )
            if _pr_foreign_phase:
                _phase_order = ["UNDERSTAND", "OBSERVE", "ANALYZE", "DECIDE", "EXECUTE", "JUDGE"]
                _delta = abs(_phase_order.index(_pr_foreign_phase) - _phase_order.index(_eval_phase)) if (_pr_foreign_phase in _phase_order and _eval_phase in _phase_order) else 0
                _foreign_reason = f"foreign_phase_declared:declared={_pr_foreign_phase},eval={_eval_phase},delta={_delta}"
                if _foreign_reason not in _admission.reasons:
                    _admission.reasons.insert(0, _foreign_reason)
                _admission.reasons = [r for r in _admission.reasons if not r.startswith("missing_required_principal")]
                _non_foreign_reasons = [r for r in _admission.reasons if r != _foreign_reason]
                if not _non_foreign_reasons and _admission.status == "RETRYABLE":
                    _admission.status = "ADMITTED"
            print(
                f"    [principal_gate] eval_phase={_eval_phase} record_phase={_pr.phase}"
                f" admission={_admission.status} reasons={_admission.reasons}",
                flush=True,
            )
            # Phase Submission Enforcement telemetry
            if _admission.status == "ADMITTED":
                state._phase_record_admit_total += 1
            elif _admission.status in ("RETRYABLE", "REJECTED"):
                state._phase_record_reject_total += 1
            if _admission.status in ("RETRYABLE", "REJECTED"):
                _pg_violation = _admission.reasons[0] if _admission.reasons else "admission_violation"
                _pg_feedback = _get_pg_feedback(_pg_violation)
                try:
                    from jingu_onboard import onboard as _onb_repair
                    _gov_repair = _onb_repair()
                    _route_obj = _gov_repair.get_route(str(_cp_s.phase), _pg_violation)
                    _repair_phase = _route_obj.next_phase if _route_obj else ""
                    _repair_hint = _gov_repair.get_repair_hint(str(_cp_s.phase), _pg_violation)
                    _pg_guidance = _repair_hint if _repair_hint else ""
                except Exception:
                    _repair_phase = ""
                    _pg_guidance = ""
                _repair_suffix = f" Repair phase: {_repair_phase}." if _repair_phase else ""
                _guidance_suffix = f" {_pg_guidance}" if _pg_guidance else ""
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
                if is_data_driven_routing_enabled():
                    try:
                        _p216_phase = _eval_phase
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
                if cp_state_holder is not None:
                    cp_state_holder[0] = _set_pv(cp_state_holder[0], _pg_violation)
                    _cp_s = cp_state_holder[0]
                else:
                    state.cp_state = _set_pv(state.cp_state, _pg_violation)
                    _cp_s = state.cp_state

                if _admission.status == "REJECTED":
                    state.early_stop_verdict = VerdictStop(reason="no_signal")
                    _emit_decision(
                        state, decision_type="gate_verdict", step_n=_cp_s.step_index,
                        verdict="stop", reason="no_signal",
                        rule_violated=_admission.reasons[0] if _admission.reasons else None,
                    )
                    print(
                        f"    [principal_gate] REJECTED → VerdictStop"
                        f" reasons={_admission.reasons}",
                        flush=True,
                    )
                    raise StopExecution("no_signal")
                else:
                    state.phase_records = [
                        r for r in state.phase_records
                        if r.phase.upper() != _eval_phase
                    ]
                    _loop_key = (_eval_phase, _pg_violation)
                    state._retryable_loop_counts[_loop_key] = (
                        state._retryable_loop_counts.get(_loop_key, 0) + 1
                    )
                    for _k in list(state._retryable_loop_counts):
                        if _k != _loop_key:
                            state._retryable_loop_counts[_k] = 0
                    _loop_count = state._retryable_loop_counts[_loop_key]
                    _RETRYABLE_LOOP_LIMIT = 3
                    _contract_bypass = False
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
                        print(
                            f"    [principal_gate] ESCALATE_CONTRACT_BUG:"
                            f" phase={_loop_key[0]} reason={_loop_key[1]}"
                            f" count={_loop_count} >= {_RETRYABLE_LOOP_LIMIT}"
                            f" → contract_bypass ADMITTED (agent continues without principal check)",
                            flush=True,
                        )
                        _emit_limit_triggered(
                            state, step_n=_cp_s.step_index,
                            limit_name="retryable_loop_force_pass",
                            configured_value=_RETRYABLE_LOOP_LIMIT, actual_value=_loop_count,
                            action_taken="bypass", source_file="step_sections.py", source_line=936,
                            reason=f"phase={_loop_key[0]} violation={_loop_key[1]}",
                        )
                        _admission.status = "ADMITTED"
                        _admission.reasons = [f"contract_bypass:{_loop_key[1]}"]
                        state._retryable_loop_counts[_loop_key] = 0
                        _contract_bypass = True

                    if not _contract_bypass and not state.early_stop_verdict:
                        _pg_retryable_no_bypass = True  # Plan-A: principal gate handled phase
                        _pv_verdict = decide_next(_cp_s)
                        print(
                            f"    [principal_gate] RETRYABLE → cognition_verdict={_pv_verdict.type}"
                            f" to={getattr(_pv_verdict, 'to', '')}",
                            flush=True,
                        )
                        if isinstance(_pv_verdict, VerdictRedirect):
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
                            # Phase Submission Enforcement: force submission after redirect
                            _model_redir = getattr(agent_self, "model", None)
                            if _model_redir is not None and hasattr(_model_redir, "set_force_phase_record"):
                                _model_redir.set_force_phase_record(True)
                            state.pending_redirect_hint = ""
        except Exception as _pg_exc:
            print(f"    [principal_gate] error={_pg_exc}", flush=True)

        try:
            if _extraction_gated:
                raise RuntimeError("extraction_gate blocked, skipping inference check")
            if _analysis_gate_rejected:
                raise RuntimeError("analysis_gate rejected, skipping inference check")
            if _analysis_gate_force_passed:
                raise RuntimeError("analysis_gate FORCE_PASS, skipping inference check")
            if _pr is None:
                raise RuntimeError("phase_record unavailable, skipping inference check")
            try:
                from principal_inference import run_inference as _run_inf
                from jingu_onboard import onboard as _onb_inf
                _gov_inf = _onb_inf()
                _inf_cfg = _gov_inf.get_phase_config(_eval_phase)
                _inf_subtype = _inf_cfg.subtype if _inf_cfg else ""
                _inf_result = _run_inf(_pr, _inf_subtype)
                _inf_telem_parts = []
                for _pname, _pdetail in _inf_result.details.items():
                    _inferred_flag = "\u2713" if _pname in _inf_result.present else "\u2717"
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
            if _inf_violation and "fake_principal" in _inf_violation and state._bypassed_principals:
                _fake_names = [
                    p.strip() for p in _inf_violation.split(":", 1)[1].split(",")
                    if p.strip()
                ]
                _remaining = [p for p in _fake_names if p not in state._bypassed_principals]
                if _remaining:
                    _inf_violation = f"fake_principal:{','.join(_remaining)}"
                else:
                    _inf_violation = None
                    print(
                        f"    [principal_inference] fake_principals_all_bypassed:"
                        f" bypassed={sorted(state._bypassed_principals)}",
                        flush=True,
                    )
            if _inf_violation and "fake_principal" in _inf_violation:
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
                state.phase_records = [
                    r for r in state.phase_records
                    if r.phase.upper() != _eval_phase
                ]
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
                    _fake_principals = []
                    if ":" in _inf_violation:
                        _fake_principals = [
                            p.strip() for p in _inf_violation.split(":", 1)[1].split(",")
                            if p.strip()
                        ]
                    state._bypassed_principals.update(_fake_principals)
                    state._retryable_loop_counts[_fi_loop_key] = 0
                    _emit_limit_triggered(
                        state, step_n=_cp_s.step_index,
                        limit_name="fake_loop_force_pass",
                        configured_value=_FAKE_LOOP_LIMIT, actual_value=_fi_loop_count,
                        action_taken="bypass", source_file="step_sections.py", source_line=1074,
                        reason=f"phase={_eval_phase} violation={_inf_violation} bypassed={sorted(state._bypassed_principals)}",
                    )
                    print(
                        f"    [principal_inference] FAKE_LOOP_SELECTIVE_BYPASS:"
                        f" phase={_eval_phase} violation={_inf_violation}"
                        f" count={_fi_loop_count} >= {_FAKE_LOOP_LIMIT}"
                        f" → bypassed_principals={sorted(state._bypassed_principals)}"
                        f" (selective bypass, other principals still enforced)",
                        flush=True,
                    )
                    state.pending_redirect_hint = ""
            elif _inf_violation and "missing_required" in _inf_violation:
                pass
        except Exception as _pi_exc:
            print(f"    [principal_inference] check error={_pi_exc}", flush=True)

        try:
            if _extraction_gated:
                raise RuntimeError("extraction_gate blocked, skipping telemetry")
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

        # Plan-A: phase advance at the BOTTOM — only if all gates passed.
        # _extraction_gated: extraction failed, staying in current phase
        # _cognition_rejected: cognition validation failed
        # _analysis_gate_rejected: analysis gate rejected (not force-passed)
        # _design_gate_rejected: design gate rejected (not force-passed)
        # Principal gate RETRYABLE: sets pending_redirect_hint + redirects phase already
        # Principal gate REJECTED: raises StopExecution (never reaches here)
        _any_gate_rejected = (
            _extraction_gated
            or _cognition_rejected
            or _analysis_gate_rejected
            or _design_gate_rejected
            or _pg_retryable_no_bypass
        )
        if not _any_gate_rejected:
            import dataclasses as _dc_adv
            if cp_state_holder is not None:
                cp_state_holder[0] = _dc_adv.replace(
                    cp_state_holder[0], phase=_new_phase, no_progress_steps=0
                )
            else:
                state.cp_state = _dc_adv.replace(
                    state.cp_state, phase=_new_phase, no_progress_steps=0
                )
            print(
                f"    [Plan-A] phase_advance COMMITTED: {_old_phase} → {_new_phase}",
                flush=True,
            )


# ═══════════════════════════════════════════════════════════════════════════════
# Section 4: _step_check_structure
# ═══════════════════════════════════════════════════════════════════════════════

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
            return

        if not latest_assistant_text:
            return

        missing: list[str] = []
        for field in required:
            if f"{field}:" not in latest_assistant_text and f"{field.lower()}:" not in latest_assistant_text:
                _dedup_key = f"structure:{field}"
                if _dedup_key not in state._injected_signals:
                    missing.append(field)

        if not missing:
            print(f"    [structure-check] phase={_phase} all_present=true", flush=True)
            return

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

        agent_self.messages.append({"role": "user", "content": _hint})
        for field in missing:
            _dedup_key = f"structure:{field}"
            state._injected_signals.add(_dedup_key)
            print(f"    [structure-check] phase={_phase} missing={field} injected=true", flush=True)

    except Exception as _exc:
        print(f"    [structure-check] error (non-fatal): {_exc}", flush=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Section 5: _step_inject_phase
# ═══════════════════════════════════════════════════════════════════════════════

def _step_inject_phase(agent_self, *, cp_state_holder: "list | None", state: "StepMonitorState") -> None:
    """
    Section p189: inject current phase as a user message prefix.
    Also consumes state.pending_redirect_hint — any hint set during this step
    is injected now so the agent sees it at the start of the next step.
    Exception-safe — injection failure must not crash main flow.
    """
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
            _phase_key = f"{state._llm_step}:phase_prefix:{_phase_str}"
            if _phase_key not in state._injected_signals:
                state._injected_signals.add(_phase_key)
                agent_self.messages.append({"role": "user", "content": _phase_content})
                print(f"    [phase_injection] phase={_phase_str} injected=true", flush=True)
            else:
                print(f"    [phase_injection] phase={_phase_str} skipped=dedup", flush=True)
    except Exception as _phase_exc:
        print(f"    [phase_injection] error (non-fatal): {_phase_exc}", flush=True)

    # Plan-B: set current phase + schema on model for submit_phase_record tool
    try:
        _model = getattr(agent_self, "model", None)
        if _model is not None and hasattr(_model, "set_current_phase"):
            _cp_s_b = cp_state_holder[0] if cp_state_holder is not None else state.cp_state
            _phase_b = str(_cp_s_b.phase).upper()
            _schema_b = None
            try:
                from jingu_onboard import onboard as _onboard_b
                _gov_b = _onboard_b()
                _schema_b = _gov_b.get_constrained_schema(_phase_b)
            except Exception:
                pass
            _model.set_current_phase(_phase_b, _schema_b)
            print(
                f"    [plan-b] set_current_phase={_phase_b}"
                f" schema_available={_schema_b is not None}",
                flush=True,
            )
    except Exception as _pb_exc:
        print(f"    [plan-b] set_current_phase error (non-fatal): {_pb_exc}", flush=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Section 6: _check_materialization_gate
# ═══════════════════════════════════════════════════════════════════════════════

def _check_materialization_gate(
    agent_self,
    *,
    cp_state_holder: "list | None",
    state: "StepMonitorState",
    patch_non_empty: bool,
) -> None:
    """
    p25 Materialization Gate Layer 1 (in-loop liveness, K=2):
    Once EXECUTE phase is entered, agent MUST write a patch within 2 steps.
    If no write happens in K steps, inject a strong forcing hint.

    Also runs retroactive analysis gate on first EXECUTE entry.
    Exception-safe — failure must not crash main flow.
    """
    _mat_phase = str((cp_state_holder[0] if cp_state_holder else state.cp_state).phase).upper()
    _mat_step = getattr(agent_self, "n_calls", -1)
    if _mat_phase == "EXECUTE":
        if state._execute_entry_step < 0:
            state._execute_entry_step = _mat_step
            state._execute_write_seen = False
            print(f"    [mat-gate] EXECUTE entered at step={_mat_step}", flush=True)

            # Retroactive analysis gate
            _retro_ag_max = 2
            _retro_ag_count = state.analysis_gate_rejects
            _last_analyze_pr = next(
                (r for r in reversed(state.phase_records)
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
                        import dataclasses as _dc_retro
                        _cp_ref = cp_state_holder[0] if cp_state_holder else state.cp_state
                        _cp_ref_new = _dc_retro.replace(
                            _cp_ref, phase="ANALYZE", no_progress_steps=0
                        )
                        if cp_state_holder:
                            cp_state_holder[0] = _cp_ref_new
                        else:
                            state.cp_state = _cp_ref_new
                        state._execute_entry_step = -1
                        state.analysis_gate_rejects += 1

                        _retro_feedback_injected = False
                        if _SDG_ENABLED and getattr(_retro_verdict, "rejection", None):
                            try:
                                _retro_sdg = _build_sdg_repair(_retro_verdict.rejection)
                                _retro_sdg += (
                                    "\n\nFix only the failing fields. Do not rewrite fields already OK."
                                    "\nStay in ANALYZE phase."
                                )
                                agent_self.messages.append({"role": "user", "content": _retro_sdg})
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
                            agent_self.messages.append({
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
                            f" ({state.analysis_gate_rejects}/{_retro_ag_max})"
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
        if patch_non_empty:
            state._execute_write_seen = True
        _MAT_GATE_K = 2
        _steps_since_entry = _mat_step - state._execute_entry_step
        if _steps_since_entry >= _MAT_GATE_K and not state._execute_write_seen:
            _emit_decision(
                state, decision_type="materialization_gate", step_n=_mat_step,
                verdict="force",
                reason=f"no_patch_after_{_steps_since_entry}_steps_in_EXECUTE",
                signals={"steps_since_entry": _steps_since_entry, "K": _MAT_GATE_K},
            )
            print(
                f"    [mat-gate] FORCE: {_steps_since_entry} steps in EXECUTE, no patch written",
                flush=True,
            )
            agent_self.messages.append({
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
        if state._execute_entry_step >= 0:
            state._execute_entry_step = -1
            state._execute_write_seen = False
