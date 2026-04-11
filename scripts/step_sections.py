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
from controlled_verify import run_controlled_verify
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


# ── Per-step structure validation (p207-P2) ──────────────────────────────────
# Required structured fields per phase, derived from phase_prompt.py templates.
PHASE_REQUIRED_FIELDS: dict[str, list[str]] = {
    "UNDERSTAND": ["PROBLEM_STATEMENT", "EXPECTED_BEHAVIOR", "ACTUAL_BEHAVIOR", "SCOPE"],
    "OBSERVE":    ["EVIDENCE"],
    "ANALYZE":    ["ROOT_CAUSE", "EVIDENCE", "CAUSAL_CHAIN"],
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
                capture_output=True, text=True, timeout=30,
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
        agent_self.messages.append({
            "role": "user",
            "content": (
                f"[Control-plane redirect: {_step_verdict.reason}] "
                f"Re-examine your environment assumptions. "
                f"Transition to phase {_step_verdict.to} before patching."
            ),
        })
        state.pending_redirect_hint = ""

    elif isinstance(_step_verdict, VerdictAdvance):
        _old_phase = _cp_s.phase
        if _step_verdict.to is not None:
            import dataclasses as _dc_adv
            if cp_state_holder is not None:
                cp_state_holder[0] = _dc_adv.replace(cp_state_holder[0], phase=_step_verdict.to, no_progress_steps=0)
                _cp_s = cp_state_holder[0]
            else:
                state.cp_state = _dc_adv.replace(state.cp_state, phase=_step_verdict.to, no_progress_steps=0)
                _cp_s = state.cp_state
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

        _eval_phase = str(_old_phase).upper()

        _pr = None
        _pr_source = "none"
        _pr_foreign_phase = ""
        try:
            from declaration_extractor import extract_record_for_phase as _extract_for_phase
            _prev_pr = next(
                (r for r in reversed(state.phase_records)
                 if r.phase.upper() == _eval_phase),
                None,
            )
            if _prev_pr is not None:
                _pr = _prev_pr
                _pr_source = "cache"
            else:
                _accumulated = state._phase_accumulated_text.get(_eval_phase, "")
                _extract_text = _accumulated if _accumulated.strip() else latest_assistant_text
                _structured_parsed = None

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
            print(
                f"    [phase_record] eval_phase={_eval_phase}"
                f" record_phase={_pr.phase} source={_pr_source}"
                f" subtype={_pr.subtype} principals={_pr.principals}"
                f" evidence_refs={_pr.evidence_refs}",
                flush=True,
            )
        except Exception as _pr_exc:
            print(f"    [phase_record] error (non-fatal): {_pr_exc}", flush=True)

        # p222: Cognition validation
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
                structured_output=(_pr_source == "structured"),
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
                        _admission.status = "ADMITTED"
                        _admission.reasons = [f"contract_bypass:{_loop_key[1]}"]
                        state._retryable_loop_counts[_loop_key] = 0
                        _contract_bypass = True

                    if not _contract_bypass and not state.early_stop_verdict:
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
