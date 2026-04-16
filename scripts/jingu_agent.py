"""jingu_process_instance — drop-in replacement for minisweagent's process_instance().

Investigation result (2026-04-11, p225-03):
  minisweagent.run.benchmarks.swebench.process_instance() has a fixed 4-parameter
  signature: (instance, output_dir, config, progress_manager). It does NOT accept
  agent_class=, agent_factory=, or any similar injection parameter.

  ProgressTrackingAgent is hardcoded at line 161 of swebench.py:
      agent = ProgressTrackingAgent(model, env, progress_manager=..., instance_id=..., **config.get("agent", {}))

  DefaultAgent (parent of ProgressTrackingAgent) has no __slots__ or __init_subclass__
  restrictions — subclassing is fully supported.

  Therefore: jingu_process_instance() mirrors process_instance() core logic but accepts
  an agent_class parameter, allowing JinguProgressTrackingAgent (or any other subclass)
  to be injected without monkey-patching.

Integration path:
  run_with_jingu_gate.py's run_agent() currently calls process_instance() and works
  around the lack of agent_class= by monkey-patching DefaultAgent.run and
  ProgressTrackingAgent.step via ScopedPatch. jingu_process_instance() provides a
  cleaner alternative: pass the agent class directly, no monkey-patching needed for
  agent instantiation.

p225-08:
  JinguDefaultAgent overrides run() to call on_attempt_end() immediately after
  super().run() returns — while the container is still alive. This eliminates the
  second ScopedPatch on DefaultAgent.run from run_with_jingu_gate.py.

  Container lifecycle: DefaultAgent.run() does NOT close the env. The env (Docker
  container) goes out of scope after jingu_process_instance()'s try block returns.
  Therefore on_attempt_end() called from JinguDefaultAgent.run() runs before env cleanup.

p225-09:
  JinguAgent.on_attempt_start() contains prompt assembly logic (moved from run_agent()).
  JinguAgent.run_attempt() contains the full attempt execution (moved from run_agent()).
  run_agent() in run_with_jingu_gate.py is now a 3-line compatibility wrapper that
  delegates to JinguAgent.run_attempt() and returns the original 4-tuple interface.
"""

import json
import logging
import re
import subprocess
import traceback
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

from minisweagent.models import get_model
from minisweagent.run.benchmarks.swebench import (
    ProgressTrackingAgent,
    get_sb_environment,
    remove_from_preds_file,
    update_preds_file,
)
from minisweagent.run.benchmarks.utils.batch_progress import RunBatchProgressManager
from minisweagent.utils.log import logger


def _parse_fail_to_pass(instance: dict) -> list[str]:
    """Parse FAIL_TO_PASS from instance dict, handling both list and JSON-string formats."""
    raw = instance.get("FAIL_TO_PASS", [])
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return []


def _extract_approach_summary(jingu_body: dict | None, patch: str, fp: dict) -> str:
    """Extract a short summary of the approach direction from this attempt.

    Uses: files changed + root cause from phase records (if available).
    This is a deterministic extraction — no LLM call.
    """
    parts = []
    # Files changed
    files = fp.get("files", []) if fp else []
    if files:
        parts.append(f"files={','.join(sorted(files))}")

    # Root cause from ANALYZE phase record
    if jingu_body:
        phase_recs = jingu_body.get("phase_records", [])
        analyze_rec = next((r for r in phase_recs if r.get("phase") == "ANALYZE"), None)
        if analyze_rec and analyze_rec.get("root_cause"):
            rc = analyze_rec["root_cause"][:100]
            parts.append(f"root_cause={rc}")

    return " | ".join(parts) if parts else ""


# Type alias for agent classes that are compatible with process_instance flow.
# Must accept (model, env, *, progress_manager, instance_id, **agent_config).
AgentClass = type

# PR1: bundle activation proof — module-level so run_report.json can access it
_bundle_activation_proof: dict = {"bundle_loaded": "not_yet_attempted"}

# ---------------------------------------------------------------------------
# AttemptResult / AttemptOutcome — return types for JinguAgent.run_attempt()
# ---------------------------------------------------------------------------

@dataclass
class AttemptResult:
    """Holds all outputs from a single attempt execution.

    Produced by JinguAgent.run_attempt(); consumed by run_agent() compatibility
    wrapper which extracts the 4-tuple (patch, exit_status, jingu_body, monitor).
    """

    patch: str | None
    exit_status: str | None
    jingu_body: dict | None
    monitor: Any  # StepMonitorState


@dataclass
class AttemptOutcome:
    """Wraps AttemptResult with attempt metadata.

    Produced by JinguAgent.run_attempt().
    """

    attempt: int
    result: AttemptResult


@dataclass
class InstanceResult:
    """Final result for a full instance run (all attempts).

    Produced by JinguAgent.run(); consumed by run_with_jingu() thin wrapper
    which calls .to_dict() to maintain backward-compatible dict interface.
    """

    instance_id: str
    accepted: bool
    patch: str
    attempts: int
    best_attempt: Optional[int] = None
    score: Optional[float] = None
    gate_code: Optional[str] = None
    gate_reason_codes: list = field(default_factory=list)
    admission_reason: Optional[str] = None
    elapsed_s: float = 0.0
    model_usage: dict = field(default_factory=dict)
    attempts_log: list = field(default_factory=list)
    attempt_delta: Optional[dict] = None
    # Semantic rootcause layer (from failure_classifier.classify_failure_layer)
    failure_layer: Optional[str] = None
    failure_record: Optional[dict] = None
    # Rejection-only fields
    status: Optional[str] = None
    failure_type: Optional[str] = None
    reason: Optional[str] = None

    def to_dict(self) -> dict:
        """Return dict compatible with run_with_jingu() caller expectations."""
        d: dict[str, Any] = {"instance_id": self.instance_id}
        if self.status == "rejected":
            # Onboarding rejection path
            d["status"] = "rejected"
            d["failure_type"] = self.failure_type
            d["reason"] = self.reason
            d["patch"] = ""
            d["accepted"] = False
            return d
        if not self.accepted:
            d["accepted"] = False
            d["patch"] = ""
            d["attempts"] = self.attempts
            d["elapsed_s"] = self.elapsed_s
            d["model_usage"] = self.model_usage
            d["attempts_log"] = self.attempts_log
            d["attempt_delta"] = self.attempt_delta
            d["failure_layer"] = self.failure_layer
            d["failure_record"] = self.failure_record
            return d
        d["accepted"] = True
        d["patch"] = self.patch
        d["attempts"] = self.attempts
        d["best_attempt"] = self.best_attempt
        d["score"] = self.score
        d["gate_code"] = self.gate_code
        d["gate_reason_codes"] = self.gate_reason_codes
        d["admission_reason"] = self.admission_reason
        d["elapsed_s"] = self.elapsed_s
        d["model_usage"] = self.model_usage
        d["attempts_log"] = self.attempts_log
        d["attempt_delta"] = self.attempt_delta
        return d


def jingu_process_instance(
    instance: dict,
    output_dir: Path,
    config: dict,
    progress_manager: RunBatchProgressManager,
    *,
    agent_class: AgentClass = ProgressTrackingAgent,
    agent_kwargs: dict[str, Any] | None = None,
) -> None:
    """Process a single SWE-bench instance with injectable agent class.

    This is a mirror of minisweagent's process_instance() (swebench.py:136-191)
    with one key difference: the agent class is a parameter, not hardcoded.

    Args:
        instance: SWE-bench instance dict (must have 'instance_id', 'problem_statement').
        output_dir: Root output directory. Instance artifacts go to output_dir/instance_id/.
        config: Agent config dict (model, agent, environment sections).
        progress_manager: RunBatchProgressManager for status updates.
        agent_class: Agent class to instantiate. Must accept the same constructor
            signature as ProgressTrackingAgent: (model, env, *, progress_manager,
            instance_id, **config.get("agent", {})). Defaults to ProgressTrackingAgent.
        agent_kwargs: Extra keyword arguments passed to agent_class constructor
            (merged with config.get("agent", {})). Optional.
    """
    instance_id = instance["instance_id"]
    instance_dir = output_dir / instance_id

    # Clean up any leftover state from previous runs (same as process_instance)
    remove_from_preds_file(output_dir / "preds.json", instance_id)
    (instance_dir / f"{instance_id}.traj.json").unlink(missing_ok=True)

    model = get_model(config=config.get("model", {}))
    task = instance["problem_statement"]

    progress_manager.on_instance_start(instance_id)
    progress_manager.update_instance_status(instance_id, "Pulling/starting environment")

    agent = None
    exit_status = None
    result = None
    extra_info: dict[str, Any] = {}

    try:
        env = get_sb_environment(config, instance)

        # --- KEY DIFFERENCE: agent_class instead of hardcoded ProgressTrackingAgent ---
        merged_agent_config = dict(config.get("agent", {}))
        if agent_kwargs:
            merged_agent_config.update(agent_kwargs)

        agent = agent_class(
            model,
            env,
            progress_manager=progress_manager,
            instance_id=instance_id,
            **merged_agent_config,
        )
        info = agent.run(task)
        exit_status = info.get("exit_status")
        result = info.get("submission")

    except Exception as e:
        logger.error(f"Error processing instance {instance_id}: {e}", exc_info=True)
        exit_status, result = type(e).__name__, ""
        extra_info = {"traceback": traceback.format_exc(), "exception_str": str(e)}

    finally:
        if agent is not None:
            traj_path = instance_dir / f"{instance_id}.traj.json"
            agent.save(
                traj_path,
                {
                    "info": {
                        "exit_status": exit_status,
                        "submission": result,
                        **extra_info,
                    },
                    "instance_id": instance_id,
                },
            )
            logger.info(f"Saved trajectory to '{traj_path}'")

        update_preds_file(
            output_dir / "preds.json", instance_id, model.config.model_name, result
        )
        progress_manager.on_instance_end(instance_id, exit_status)


# ---------------------------------------------------------------------------
# StepDecision — return type for JinguAgent.on_step_end()
# ---------------------------------------------------------------------------

@dataclass
class StepDecision:
    """Decision returned by JinguAgent.on_step_end() to control agent flow.

    action:
        "continue" — proceed normally (default).
        "redirect" — inject *message* into agent conversation and continue.
        "stop"     — raise StopExecution with *reason*.
    target_phase: optional phase hint when redirecting (e.g. "EXECUTE").
    reason: short machine-readable reason (used for StopExecution / logging).
    message: user-role message injected when action="redirect".
    """

    action: Literal["continue", "redirect", "stop"]
    target_phase: str | None = None
    reason: str = ""
    message: str = ""


# ---------------------------------------------------------------------------
# JinguAgent — orchestration skeleton (hooks + attempt/run lifecycle)
# ---------------------------------------------------------------------------

class JinguAgent:
    """Orchestrates governance hooks around a minisweagent agent.

    Lifecycle:
        run()  →  for each attempt:
            on_attempt_start()  →  run_attempt()  →  on_attempt_end()
        Inside run_attempt(), the agent calls step() repeatedly;
        JinguProgressTrackingAgent delegates to on_step_start / on_step_end.
    """

    def __init__(
        self,
        instance: dict,
        output_dir: Path,
        governance: Any,
        *,
        mode: str = "jingu",
        max_attempts: int = 3,
    ):
        self._instance = instance
        self._output_dir = output_dir
        self._governance = governance
        self._mode = mode
        self._max_attempts = max_attempts
        self._cp_state_holder: list[Any] = []
        self._state: Any | None = None  # StepMonitorState, populated in run()
        self._step_emitter: Any | None = None  # StepEventEmitter, per-attempt
        self._step_start_ts: float = 0.0  # ms timestamp set in on_step_start
        self._decision_logger: Any | None = None  # DecisionLogger, per-attempt (p230)
        self._prompt_sections: list[dict] = []  # p231: prompt sections from p229 snapshot
        self._prev_phase_records_count: int = 0  # p231: track phase_records length for checkpoint trigger

    # -- step-level hooks (called by JinguProgressTrackingAgent.step) --------

    def on_step_start(self, agent_self: Any, step_n: int) -> None:
        """Called before each agent step.

        Runs observation (Section 1) and detects container readiness.
        Stores observation results on self for use by on_step_end().
        """
        import time as _time_mod
        from step_sections import _step_observe

        # p228: record step start time for duration calculation
        self._step_start_ts = _time_mod.time() * 1000

        # Wire monitor state onto agent instance for _step_observe dedup
        if self._state is not None:
            agent_self._jingu_monitor_state = self._state

        text, snippet, env_error = _step_observe(
            agent_self, step_n=step_n, mode=self._mode
        )
        # Stash for on_step_end
        self._last_observe_result = (text, snippet, env_error)

        # P0.4: QJ ack detection — check if agent acknowledged corrective QJ
        if (self._state is not None
                and self._state.quick_judge_history
                and text):
            _last_qj = self._state.quick_judge_history[-1]
            if (_last_qj.get("acknowledged") is None
                    and _last_qj.get("signal_kind") == "corrective"):
                try:
                    from quick_judge import detect_acknowledged
                    from types import SimpleNamespace
                    _qj_ns = SimpleNamespace(**_last_qj)
                    _ack = detect_acknowledged(_qj_ns, text, [])
                    _last_qj["acknowledged"] = _ack
                    if not _ack:
                        self._state._qj_corrective_ignored = True
                        print(f"    [qj-ack] corrective QJ IGNORED by agent", flush=True)
                    else:
                        self._state._qj_corrective_ignored = False
                        print(f"    [qj-ack] corrective QJ acknowledged by agent", flush=True)
                except Exception as _ack_exc:
                    print(f"    [qj-ack] detection error (non-fatal): {_ack_exc}", flush=True)

        # Accumulate text per phase for PhaseRecord extraction (p221)
        if self._state is not None and text:
            _cp = (
                self._cp_state_holder[0]
                if self._cp_state_holder
                else self._state.cp_state
            )
            _phase = str(_cp.phase).upper()
            self._state._phase_accumulated_text[_phase] = (
                self._state._phase_accumulated_text.get(_phase, "") + "\n" + text
            )

        # Container readiness detection
        cid = getattr(getattr(agent_self, "env", None), "container_id", None)
        if cid and self._state is not None and self._state.container_id is None:
            self.on_container_ready(cid)

    def on_step_end(self, agent_self: Any, step_n: int) -> StepDecision:  # noqa: ARG002
        """Called after each agent step.

        Runs sections 2-6 (verify, cp_update, structure, phase_inject, mat_gate).
        Returns a StepDecision to control agent flow.
        """
        from step_sections import (
            _step_verify_if_needed,
            _step_cp_update_and_verdict,
            _step_check_structure,
            _step_inject_phase,
            _check_materialization_gate,
        )
        from step_monitor_state import StopExecution

        # Retrieve observation results from on_step_start
        text, snippet, env_error = getattr(
            self, "_last_observe_result", ("", "", False)
        )

        if self._state is None:
            return StepDecision(action="continue")

        cp_holder = self._cp_state_holder if self._cp_state_holder else None

        # Section 2: verify + quick judge
        patch_non_empty = _step_verify_if_needed(
            agent_self, state=self._state, verify_debounce_s=5.0,
            cp_state_holder=cp_holder,
        )

        # E1: inject quick judge message if pending (system-originated signal)
        _qj_msg = getattr(self._state, '_pending_quick_judge_message', '')
        if _qj_msg:
            agent_self.messages.append({
                "role": "user",
                "content": _qj_msg,
            })
            self._state._pending_quick_judge_message = ""

        # Section 3: cp update + verdict
        try:
            _step_cp_update_and_verdict(
                agent_self,
                state=self._state,
                cp_state_holder=cp_holder,
                env_error_detected=env_error,
                step_patch_non_empty=patch_non_empty,
                latest_assistant_text=text,
            )
        except StopExecution:
            return StepDecision(
                action="stop",
                reason=getattr(self._state.early_stop_verdict, "reason", "no_signal"),
            )

        # Section 5: phase inject (before structure check, matching _monitored_step order)
        _step_inject_phase(agent_self, cp_state_holder=cp_holder, state=self._state)

        # Section 4: structure check
        _step_check_structure(
            agent_self,
            cp_state_holder=cp_holder,
            state=self._state,
            latest_assistant_text=text,
        )

        # Section 6: materialization gate
        _check_materialization_gate(
            agent_self,
            cp_state_holder=cp_holder,
            state=self._state,
            patch_non_empty=patch_non_empty,
        )

        # Determine step decision
        _decision: StepDecision
        if self._state.early_stop_verdict:
            _decision = StepDecision(
                action="stop",
                reason=getattr(self._state.early_stop_verdict, "reason", "no_signal"),
            )
        elif self._state.pending_redirect_hint:
            hint = self._state.pending_redirect_hint
            self._state.pending_redirect_hint = ""
            _decision = StepDecision(action="redirect", message=hint)
        else:
            _decision = StepDecision(action="continue")

        # p228: emit step event (never crashes the run)
        try:
            self._emit_step_event(agent_self, step_n, env_error, patch_non_empty, _decision)
        except Exception:
            pass

        # p231: checkpoint at key decision points (phase_advance, gate_stop, gate_redirect, materialization_gate)
        try:
            self._maybe_save_checkpoint(agent_self, step_n, _decision)
        except Exception:
            pass

        return _decision

    def _emit_step_event(
        self,
        agent_self: Any,
        step_n: int,
        env_error: bool,
        patch_non_empty: bool,
        decision: StepDecision,
    ) -> None:
        """Build and emit a StepEvent. Called from on_step_end. Never raises to caller."""
        import time as _time_mod
        if self._step_emitter is None:
            return

        from step_event_emitter import StepEvent, extract_tool_usage

        # cp_state snapshot
        cp = self._cp_state_holder[0] if self._cp_state_holder else None
        cp_snapshot = {
            "phase": str(getattr(cp, "phase", None)),
            "step": getattr(cp, "phase_steps", 0),
            "no_progress_steps": getattr(cp, "no_progress_steps", 0),
            "patch_first_write": getattr(cp, "patch_first_write", False),
            "phase_records_count": len(getattr(self._state, "phase_records", [])) if self._state else len(getattr(cp, "phase_records", [])),
        } if cp else None

        # gate verdict from decision
        gate_verdict: str | None = None
        gate_reason: str | None = None
        if decision.action == "stop":
            gate_verdict = "stop"
            gate_reason = decision.reason
        elif decision.action == "redirect":
            gate_verdict = "redirect"
            gate_reason = decision.reason or "redirect"

        # tool usage extraction
        msgs = getattr(agent_self, "messages", [])
        tool_calls_count, files_read, files_written = extract_tool_usage(msgs, step_n)

        now_ms = _time_mod.time() * 1000
        event = StepEvent(
            step_n=step_n,
            timestamp_ms=now_ms,
            phase=str(getattr(cp, "phase", None)) if cp else None,
            gate_verdict=gate_verdict,
            gate_reason=gate_reason,
            cp_state_snapshot=cp_snapshot,
            tool_calls_count=tool_calls_count,
            files_read=files_read,
            files_written=files_written,
            step_duration_ms=now_ms - self._step_start_ts if self._step_start_ts else 0.0,
            patch_non_empty=patch_non_empty,
            env_error=env_error,
        )
        self._step_emitter.emit(event)

    def _maybe_save_checkpoint(
        self, agent_self: Any, step_n: int, decision: StepDecision
    ) -> None:
        """Save a checkpoint if this step is a key decision point (p231).

        Triggers:
        - phase_advance: phase_records count increased since last step
        - gate_stop: decision.action == "stop"
        - gate_redirect: decision.action == "redirect"
        - materialization_gate: detected via decision logger's last event type

        Never raises — all operations wrapped in try/except.
        """
        if self._state is None:
            return

        # Determine trigger type
        trigger: str | None = None
        current_pr_count = len(self._state.phase_records)
        if current_pr_count > self._prev_phase_records_count:
            trigger = "phase_advance"
            self._prev_phase_records_count = current_pr_count
        elif decision.action == "stop":
            trigger = "gate_stop"
        elif decision.action == "redirect":
            trigger = "gate_redirect"

        # Also check for materialization_gate via cp state
        if (
            trigger is None
            and self._state._execute_entry_step >= 0
            and not self._state._execute_write_seen
        ):
            # materialization gate may have fired this step — check decision logger's
            # last event. We detect it by checking if mat gate emitted a decision.
            # Simpler proxy: look at messages for mat-gate injection.
            msgs = getattr(agent_self, "messages", [])
            if msgs and isinstance(msgs[-1], dict):
                last_content = msgs[-1].get("content", "")
                if isinstance(last_content, str) and "[mat-gate]" in last_content.lower():
                    trigger = "materialization_gate"

        if trigger is None:
            return

        from checkpoint import Checkpoint, save_checkpoint

        instance_id = self._instance.get("instance_id", "unknown")
        attempt = self._state.attempt

        # Build cp_state dict
        cp_state_dict = (
            self._state.to_checkpoint_dict()
            if hasattr(self._state, "to_checkpoint_dict")
            else {}
        )

        # Phase records
        phase_records = []
        for pr in (self._state.phase_records or []):
            if isinstance(pr, dict):
                phase_records.append(pr)
            elif hasattr(pr, "__dict__"):
                phase_records.append(vars(pr))
            else:
                phase_records.append({"raw": str(pr)})

        # Pending hints
        pending_hints = []
        if self._state.pending_redirect_hint:
            pending_hints.append(self._state.pending_redirect_hint)

        # Current phase
        cp = self._cp_state_holder[0] if self._cp_state_holder else None
        current_phase = str(getattr(cp, "phase", None)) if cp else "unknown"

        ckpt = Checkpoint(
            step_n=step_n,
            instance_id=instance_id,
            attempt=attempt,
            trigger=trigger,
            messages_so_far=list(getattr(agent_self, "messages", [])),
            cp_state=cp_state_dict,
            phase_records=phase_records,
            pending_hints=pending_hints,
            prompt_sections=self._prompt_sections,
            metadata={
                "timestamp_ms": time.time() * 1000,
                "phase": current_phase,
                "trigger_detail": decision.reason or "",
            },
        )
        result = save_checkpoint(ckpt, self._output_dir / instance_id)
        if result:
            print(
                f"    [p231] checkpoint saved: step={step_n} trigger={trigger}"
                f" phase={current_phase} path={result.name}",
                flush=True,
            )

    # -- attempt-level hooks ------------------------------------------------

    def on_attempt_start(self, attempt: int, previous_failure: str | None) -> list[str]:
        """Build the full extra_parts list for this attempt.

        Moved from run_agent() in run_with_jingu_gate.py (p225-09).
        Returns list of prompt parts to join with double-newline.
        """
        extra_parts: list[str] = []

        # jingu-specific constraint: prevent ENVIRONMENT_NOT_AGENT_WORK violations.
        # baseline uses the official prompt without this block.
        if self._mode == "jingu":
            extra_parts.append(
                "## FORBIDDEN ACTIONS\n\n"
                "The following actions are STRICTLY FORBIDDEN. Do NOT do any of these:\n\n"
                "- `pip install`, `pip3 install`, `uv pip install`, `python setup.py install`, `conda install`\n"
                "- `apt install`, `apt-get install`, `dnf install`, `brew install`\n"
                "- Installing or configuring any software or dependencies\n\n"
                "The environment is already fully set up. If something appears missing, "
                "read the existing code more carefully — the solution is always a code change, not an environment change."
            )

        # B4: phase-structured reasoning protocol — p224-09: loaded via compile_bundle().
        # All phase prompts, principal requirements, type contracts, forbidden moves
        # are derived from bundle.json (compiled by jingu-cognition TS). Zero hardcoded strings.
        global _bundle_activation_proof  # PR1: exposed for run_report.json
        _phase_prompt_parts: list[str] = []
        _type_contracts_block = "Type contracts: (see principal_gate for v2.0 contracts)"
        _analysis_req = "ontology_alignment, phase_boundary_discipline, causal_grounding, evidence_linkage"
        _decision_req = "ontology_alignment, phase_boundary_discipline, option_comparison, constraint_satisfaction"
        _execute_req  = "ontology_alignment, phase_boundary_discipline, action_grounding, minimal_change"

        try:
            from bundle_compiler import compile_bundle as _compile_bundle
            import logging as _logging
            _bundle = _compile_bundle()
            _report = _bundle.activation_report
            _logging.getLogger(__name__).info(
                "[jingu-compiler] activation_ok=%s bundle_version=%s compiler_version=%s "
                "generator_commit=%s phases=%s contracts=%d principals=%d "
                "inference_eligible=%d fake_check_eligible=%d warnings=%d",
                _report.activation_ok, _report.bundle_version, _report.compiler_version,
                _report.generator_commit, _report.phases_compiled, _report.contracts_compiled,
                _report.principals_total, _report.principals_inference_eligible,
                _report.principals_fake_check_eligible, len(_report.prompt_warnings),
            )
            _gov_prompt = _bundle.governance
            # Assemble full reasoning protocol from per-phase prompts
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
            def _get_req(p: str) -> str:
                _g = _gov_prompt.get_gate(p)
                return ", ".join(_g.required_principals) if _g else ""
            _analysis_req = _get_req("ANALYZE")
            _decision_req = _get_req("DECIDE")
            _execute_req  = _get_req("EXECUTE")
            # Activation proof (RT4)
            _bundle_activation_proof = {
                "bundle_loaded": True,
                "bundle_version": _report.bundle_version,
                "compiler_version": _report.compiler_version,
                "phases_compiled": _report.phases_compiled,
                "contracts_compiled": _report.contracts_compiled,
                "principals_total": _report.principals_total,
                "activation_ok": _report.activation_ok,
            }
            print(
                f"    [BUNDLE_ACTIVATED] version={_report.bundle_version} "
                f"phases={_report.phases_compiled} contracts={_report.contracts_compiled} "
                f"principals={_report.principals_total} ok={_report.activation_ok}",
                flush=True,
            )
        except Exception as _onb_exc:
            import traceback as _tb
            _bundle_error_msg = str(_onb_exc)
            _bundle_error_trace = "".join(
                _tb.format_exception(type(_onb_exc), _onb_exc, _onb_exc.__traceback__)
            )
            print(
                f"    [BUNDLE_LOAD_FAILURE] compile_bundle() failed: {_bundle_error_msg}\n"
                f"    {_bundle_error_trace}",
                flush=True,
            )
            _bundle_activation_proof = {
                "bundle_loaded": False,
                "error": _bundle_error_msg,
                "fallback_active": True,
            }

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

        fail_to_pass = _parse_fail_to_pass(self._instance)
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
            extra_parts.append(f"Previous attempt failed: {previous_failure[:800]}")

        # p229: prompt assembly snapshot — persist assembled prompt for offline analysis.
        try:
            def _classify_part(idx: int, part: str) -> str:
                if "FORBIDDEN" in part:
                    return "forbidden_actions"
                if "REASONING PROTOCOL" in part:
                    return "reasoning_protocol"
                if "IMPORTANT: Your fix must" in part or "FAIL_TO_PASS" in part:
                    return "fail_to_pass"
                if "Previous attempt failed" in part:
                    return "retry_hint"
                return f"section_{idx}"

            _snap_sections = []
            _snap_total_chars = 0
            for _i, _part in enumerate(extra_parts):
                _snap_sections.append({
                    "name": _classify_part(_i, _part),
                    "char_count": len(_part),
                    "content": _part,
                })
                _snap_total_chars += len(_part)

            _snap_instance_id = self._instance.get("instance_id", "unknown")
            prompt_snapshot = {
                "attempt": attempt,
                "instance_id": _snap_instance_id,
                "mode": self._mode,
                "sections": _snap_sections,
                "has_retry_hint": bool(previous_failure),
                "fail_to_pass_count": len(_parse_fail_to_pass(self._instance)),
                "total_chars": _snap_total_chars,
            }

            _snap_dir = self._output_dir / _snap_instance_id / f"attempt_{attempt}"
            _snap_dir.mkdir(parents=True, exist_ok=True)
            _snap_path = _snap_dir / "prompt_snapshot.json"
            with open(_snap_path, "w") as _snap_f:
                json.dump(prompt_snapshot, _snap_f, indent=2)
            # p231: store prompt sections for checkpoint inclusion
            self._prompt_sections = _snap_sections
        except Exception as _snap_exc:
            logging.getLogger(__name__).warning("[p229] prompt snapshot failed: %s", _snap_exc)

        return extra_parts

    def on_container_ready(self, container_id: str) -> None:
        """Called once when container_id is first observed in on_step_start().

        Sets self._state.container_id so in-loop controlled_verify can begin.
        Equivalent to: container_id injection that was in _verifying_run (pre-p225-08).
        """
        assert self._state is not None
        self._state.container_id = container_id

    def on_attempt_end(self, agent_self: Any, submission: str | None) -> None:
        """Called after each attempt completes (before container is destroyed).

        Runs the end-of-attempt governance checks (previously in the _verifying_run
        closure in run_with_jingu_gate.py, removed in p225-08):
          1. Cognition gate (p187) — fires when cp_state.phase == "JUDGE"
          2. In-loop judge (p191) — patch format + semantic weakening checks
          3. Unified prerequisite gate (p192) — aggregates cognition + judge
          4. End-of-attempt controlled_verify (step=-1) — oracle eval signal

        Container-lifecycle invariant: this method is called from JinguDefaultAgent.run()
        immediately after super().run() returns. The env (Docker container) is still
        alive at this point — it goes out of scope only after jingu_process_instance()'s
        try block exits.
        """
        from controlled_verify import run_controlled_verify
        from control.reasoning_state import VerdictStop

        # p228: close step event emitter (before any early return)
        if self._step_emitter is not None:
            try:
                self._step_emitter.close()
            except Exception:
                pass
            self._step_emitter = None

        # p230: close decision provenance logger
        if self._decision_logger is not None:
            try:
                self._decision_logger.close()
            except Exception:
                pass
            self._decision_logger = None

        _monitor = self._state
        if _monitor is None:
            print(f"    [attempt-end] skip: no monitor state", flush=True)
            return

        # p241: attempt-end telemetry — observable signal for every attempt termination
        _cp_phase = "unknown"
        if self._cp_state_holder:
            _cp_phase = self._cp_state_holder[0].phase
        _has_submission = bool(submission)

        cid = getattr(getattr(agent_self, "env", None), "container_id", None)
        if not cid:
            print(f"    [attempt-end] phase={_cp_phase} submission={_has_submission}"
                  f" cv_triggered=false cv_skip_reason=no_container_id", flush=True)
            return
        submitted = submission or ""
        if not submitted:
            print(f"    [attempt-end] phase={_cp_phase} submission=false"
                  f" cv_triggered=false cv_skip_reason=no_submission", flush=True)
            return

        print(f"    [attempt-end] phase={_cp_phase} submission=true"
              f" cv_triggered=true container={cid[:12]}", flush=True)

        cp_state_holder = self._cp_state_holder if self._cp_state_holder else None

        # p187: cognition gate — check declaration quality before controlled_verify.
        # Fires when cp_state.phase == "JUDGE" (EXECUTE->JUDGE advance by verdict routing).
        # Pass  → continue to controlled_verify as normal.
        # Fail  → inject feedback as pending_redirect_hint, skip controlled_verify.
        _cg_result_str: str | None = None
        if cp_state_holder is not None and cp_state_holder[0].phase == "JUDGE":
            _cg_decl: dict = {}
            try:
                from declaration_extractor import (
                    extract_declaration,
                    extract_last_agent_message,
                    extract_from_structured,
                )
                from patch_signals import extract_patch_signals
                _cg_msgs = getattr(agent_self, "messages", [])
                # p221: try structured output first
                from run_with_jingu_gate import _try_parse_structured_output
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
                _monitor.pending_redirect_hint = f"[COGNITION_FAIL] {_cg_feedback}"
                print(
                    f"    [cognition_gate] skipping controlled_verify — feedback injected",
                    flush=True,
                )

        # p191: in-loop judge — patch format + semantic weakening checks.
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
                    _monitor.early_stop_verdict = VerdictStop(reason="empty_patch")
                elif not _judge_result.patch_format:
                    _monitor.pending_redirect_hint = "[REDIRECT:EXECUTE] patch_format_error"
                elif not _judge_result.no_semantic_weakening:
                    _monitor.pending_redirect_hint = "[REDIRECT:ANALYZE] semantic_weakening_detected"
                elif not _judge_result.changed_file_relevant:
                    # p204: changed_file_relevant promoted to hard check
                    # Agent modified only test files (not source) — redirect back to EXECUTE
                    _monitor.pending_redirect_hint = "[REDIRECT:EXECUTE] wrong_file_changed"
                print(
                    f"    [in_loop_judge] skipping controlled_verify (hard check failed)",
                    flush=True,
                )
        except Exception as _ilj_exc:
            print(f"    [in_loop_judge] error (non-fatal): {_ilj_exc}", flush=True)

        # p192: unified prerequisite gate — aggregates cognition + judge results
        from run_with_jingu_gate import _verify_prerequisites
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
            return

        t_cv0 = time.monotonic()
        cv_result = run_controlled_verify(submitted, self._instance, cid, timeout_s=None)
        cv_result["elapsed_ms"] = round((time.monotonic() - t_cv0) * 1000, 1)
        # Store as last verify_history entry (step=-1 means end-of-attempt)
        _monitor.record_verify(-1, cv_result)
        # v2 two-column log: final-verify (oracle/eval) vs inner-verify (agent-visible)
        _er = cv_result.get("eval_resolved")
        if _er is not None:
            print(
                f"    [outcome-eval] eval_resolved={_er}"
                f"  f2p={cv_result.get('f2p_passed')}/{(cv_result.get('f2p_passed', 0) or 0) + (cv_result.get('f2p_failed', 0) or 0)}"
                f"  p2p={cv_result.get('p2p_passed')}/{(cv_result.get('p2p_passed', 0) or 0) + (cv_result.get('p2p_failed', 0) or 0)}",
                flush=True,
            )

    # -- top-level lifecycle ------------------------------------------------

    def run_attempt(
        self,
        attempt: int,
        previous_failure: str | None = "",
        parent_timer: Any = None,
    ) -> "AttemptOutcome":
        """Execute a single attempt: prompt -> inner agent -> traj parse -> jingu_body.

        Moved from run_agent() in run_with_jingu_gate.py (p225-09).
        Returns AttemptOutcome wrapping AttemptResult with all attempt outputs.
        """
        from minisweagent.config import get_config_from_spec
        from minisweagent.utils.serialize import recursive_merge
        from minisweagent.run.benchmarks.swebench import RunBatchProgressManager
        from run_with_jingu_gate import (
            Timer, ModelUsage, BASE_CONFIG, _usage_tracker,
            extract_jingu_body, classify_failure, get_failure_routing,
            parse_pytest_output,
        )
        from failure_classifier import classify_failure_layer, route_from_failure, derive_failure_mode, route_from_failure_mode
        from step_monitor_state import StepMonitorState, StopExecution

        instance = self._instance
        instance_id = instance["instance_id"]
        attempt_dir = self._output_dir / f"attempt_{attempt}"
        attempt_dir.mkdir(parents=True, exist_ok=True)

        t_agent = Timer(f"agent attempt={attempt}", parent=parent_timer)

        # Start from jingu-swebench.yaml (fork of swebench.yaml with FORBIDDEN ACTIONS block,
        # patched system_template, and Recommended Workflow steps 2/4/5 removed).
        # Config lives in mini-swe-agent/src/minisweagent/config/benchmarks/jingu-swebench.yaml.
        t_cfg = Timer("config load", parent=t_agent)
        config = get_config_from_spec("jingu-swebench.yaml")
        config = recursive_merge(config, BASE_CONFIG)

        # Build instance_template_extra: tests that must pass + optional retry hint.
        # on_attempt_start() returns the extra_parts list (prompt assembly).
        extra_parts = self.on_attempt_start(attempt, previous_failure)
        if extra_parts:
            # Append directly to instance_template — instance_template_extra is NOT a recognized
            # AgentConfig field and would never be rendered. Direct append is the only correct path.
            config["agent"]["instance_template"] = (
                config["agent"]["instance_template"] + "\n\n" + "\n\n".join(extra_parts)
            )
        t_cfg.stop()

        # p228: create step event emitter for this attempt
        try:
            from step_event_emitter import StepEventEmitter
            self._step_emitter = StepEventEmitter(
                self._output_dir / instance_id, attempt
            )
        except Exception as _emit_exc:
            print(f"    [step-emitter] WARNING: init failed: {_emit_exc}", flush=True)
            self._step_emitter = None

        # p230: create decision provenance logger for this attempt
        try:
            from decision_logger import DecisionLogger
            self._decision_logger = DecisionLogger(
                self._output_dir / instance_id, attempt
            )
        except Exception as _dl_exc:
            print(f"    [decision-logger] WARNING: init failed: {_dl_exc}", flush=True)
            self._decision_logger = None

        print(f"    [agent] running {instance_id} attempt={attempt}...")

        preds_path = attempt_dir / "preds.json"
        progress = RunBatchProgressManager(num_instances=1)

        # Initialize StepMonitorState for this attempt.
        _monitor = StepMonitorState(
            instance_id=instance_id,
            attempt=attempt,
            instance=instance,
        )
        # p226-05 + Plan-B: per-attempt extraction metrics counters
        _monitor._extraction_structured = 0
        _monitor._extraction_regex_fallback = 0
        _monitor._extraction_no_schema = 0
        _monitor._extraction_tool_submitted = 0
        _monitor._missing_submission_count = 0
        # Plan-B: separate storage for diagnostic-only records (never admitted)
        _monitor.diagnostic_phase_records = []
        # C-09: per-phase extraction telemetry from extract_phase_output()
        _monitor.extraction_telemetry = {}
        # Plan-A: reset extraction retry counts per attempt
        _monitor.extraction_retry_counts = {}

        self._state = _monitor
        # P0.2: cross-attempt routing enforcement
        # _monitor IS self._state — the same StepMonitorState object that gets
        # passed as state= to admit_phase_record() and all step sections.
        # Writing self._state.required_next_phase here is the SAME as writing
        # state.required_next_phase that Gate 0 reads in admit_phase_record().
        _routed_phase = str(self._cp_state_holder[0].phase).upper() if self._cp_state_holder else "OBSERVE"
        if attempt > 1 and _routed_phase != "OBSERVE":
            self._state.required_next_phase = _routed_phase
            print(
                f"    [routing-enforcement] attempt={attempt}"
                f" required_next_phase={_routed_phase}",
                flush=True,
            )
        # p231: reset checkpoint tracking for new attempt
        self._prev_phase_records_count = 0
        # p230: pass decision logger to state for step_sections access
        _monitor._decision_logger = self._decision_logger
        # cp_state_holder already set by caller (run_agent wrapper or run_with_jingu)

        t_llm = Timer("LLM agent loop (Bedrock)", parent=t_agent)
        try:
            jingu_process_instance(
                instance, attempt_dir, config, progress,
                agent_class=JinguDefaultAgent,
                agent_kwargs={"jingu_agent": self},
            )
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
        finally:
            # p228: ensure emitter is closed even if on_attempt_end was skipped
            if self._step_emitter is not None:
                try:
                    self._step_emitter.close()
                except Exception:
                    pass
                self._step_emitter = None
            # p230: ensure decision logger is closed
            if self._decision_logger is not None:
                try:
                    self._decision_logger.close()
                except Exception:
                    pass
                self._decision_logger = None
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
                    # P2-fix: include output_tail so build_repair_prompt can
                    # inject concrete test failure output into retry hints.
                    # verify_history now stores output_tail (FAIL/ERROR lines
                    # extracted by controlled_verify); use it directly.
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
                        "output_tail": _cv_source.get("output_tail", ""),
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
                    # Failure layer: semantic rootcause classification (full FailureRecord)
                    _qj_hist = _monitor.quick_judge_history if hasattr(_monitor, 'quick_judge_history') else None
                    _fr = classify_failure_layer(cv_flat, _qj_hist, _ft, instance_id=instance_id)
                    jingu_body["failure_layer"] = _fr.failure_layer
                    jingu_body["failure_record"] = _fr.to_dict()
                    # Route from failure record for enhanced retry
                    _fr_routing = route_from_failure(_fr)
                    jingu_body["failure_layer_routing"] = _fr_routing
                    # P2: Prediction error — compare DECIDE predictions vs actual
                    try:
                        from prediction_error import compute_prediction_error
                        _pred_err = compute_prediction_error(
                            _monitor.phase_records, cv_flat,
                        )
                        jingu_body["prediction_error"] = _pred_err.to_dict()
                        if _pred_err.error_type != "prediction_no_data":
                            print(
                                f"    [prediction-error] type={_pred_err.error_type}"
                                f" severity={_pred_err.severity}"
                                f" f2p={_pred_err.actual_f2p_passed}/{_pred_err.actual_f2p_passed + _pred_err.actual_f2p_failed}"
                                f" repair_target={_pred_err.repair_target}",
                                flush=True,
                            )
                    except Exception as _pe_exc:
                        jingu_body["prediction_error"] = {"error_type": "computation_error", "detail": str(_pe_exc)[:200]}
                        logger.warning("prediction_error computation failed: %s", _pe_exc)
                    if _fr.failure_layer != "unknown":
                        print(f"    [failure-layer] {_fr.failure_layer}"
                              f"  phase={_fr.phase_of_failure}"
                              f"  confidence={_fr.confidence:.2f}"
                              f"  actions={[a.type for a in _fr.recommended_actions]}",
                              flush=True)
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
                # E1: Quick Judge telemetry
                if hasattr(_monitor, 'quick_judge_history') and _monitor.quick_judge_history:
                    jingu_body["quick_judge_history"] = _monitor.quick_judge_history
                    jingu_body["quick_judge_invoked"] = len(_monitor.quick_judge_history)
                    jingu_body["quick_judge_acknowledged"] = sum(
                        1 for qj in _monitor.quick_judge_history if qj.get("acknowledged")
                    )
                    jingu_body["quick_judge_directions"] = [
                        qj.get("direction", "unknown") for qj in _monitor.quick_judge_history
                    ]
                    jingu_body["quick_judge_target_statuses"] = [
                        qj.get("target_status", "unknown") for qj in _monitor.quick_judge_history
                    ]
                    jingu_body["quick_judge_signal_kinds"] = [
                        qj.get("signal_kind", "non_corrective_noise") for qj in _monitor.quick_judge_history
                    ]
                    # L3 effectiveness detection
                    try:
                        from quick_judge import detect_effective
                        jingu_body["quick_judge_effective"] = detect_effective(_monitor.quick_judge_history)
                    except Exception:
                        jingu_body["quick_judge_effective"] = None
                    # Log quick judge summary (target-aware)
                    _qj_targets = [qj.get("target_status", "?") for qj in _monitor.quick_judge_history]
                    _qj_signals = [qj.get("signal_kind", "?") for qj in _monitor.quick_judge_history]
                    print(f"    [quick_judge] invoked={len(_monitor.quick_judge_history)} "
                          f"target_statuses={_qj_targets} signals={_qj_signals} "
                          f"effective={jingu_body.get('quick_judge_effective')}",
                          flush=True)
                else:
                    jingu_body["quick_judge_invoked"] = 0
                    jingu_body["quick_judge_effective"] = None
                # p190: per-phase records — one entry per VerdictAdvance during this attempt
                jingu_body["phase_records"] = [r.as_dict() for r in _monitor.phase_records]
                # Plan-B strong: extraction metrics — admitted vs diagnostic rates
                _em_tool = getattr(_monitor, "_extraction_tool_submitted", 0)
                _em_structured = getattr(_monitor, "_extraction_structured", 0)
                _em_regex = getattr(_monitor, "_extraction_regex_fallback", 0)
                _em_no_schema = getattr(_monitor, "_extraction_no_schema", 0)
                _em_missing = getattr(_monitor, "_missing_submission_count", 0)
                _em_total_attempts = _em_tool + _em_missing
                jingu_body["extraction_metrics"] = {
                    "tool_submitted": _em_tool,
                    "missing_submissions": _em_missing,
                    "phase_completion_rate": (
                        f"{_em_tool}/{_em_total_attempts}"
                        if _em_total_attempts > 0 else "0/0"
                    ),
                    "diagnostic_structured": _em_structured,
                    "diagnostic_regex": _em_regex,
                    "diagnostic_no_schema": _em_no_schema,
                }
                print(
                    f"    [extraction_metrics] attempt={attempt}"
                    f" tool_submitted={_em_tool}"
                    f" missing_submissions={_em_missing}"
                    f" phase_completion_rate={_em_tool}/{_em_total_attempts}"
                    f" diagnostic_structured={_em_structured}"
                    f" diagnostic_regex={_em_regex}",
                    flush=True,
                )
                # C-09: merge extraction_telemetry from step_sections into jingu_body
                _ext_telem = getattr(_monitor, "extraction_telemetry", None)
                if _ext_telem:
                    jingu_body["extraction_telemetry"] = _ext_telem

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
                # ── Dual-layer failure classification ──────────────────────
                # failure_mode: behavioral (full coverage, all attempts)
                # failure_type: semantic (CV-based, high confidence only)
                # failure_source: provenance marker
                _fm = derive_failure_mode(jingu_body)
                jingu_body["failure_mode"] = _fm
                if jingu_body.get("failure_type") is not None:
                    jingu_body["failure_source"] = "cv_based"
                elif _cv_source is None:
                    jingu_body["failure_source"] = "behavioral_fallback"
                else:
                    # CV existed but classify_failure returned None (success)
                    jingu_body["failure_source"] = "cv_based"
                print(f"    [failure-mode] mode={_fm} source={jingu_body['failure_source']}"
                      f" type={jingu_body.get('failure_type', 'none')}", flush=True)
                # Write jingu_body back into traj.json so gate_runner.js can read it
                traj["jingu_body"] = jingu_body
                traj_path.write_text(json.dumps(traj, indent=2, default=str))
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

        # Determine final patch — same priority as original run_agent()
        patch: str | None = None

        # Read submission from preds.json
        if preds_path.exists():
            preds = json.loads(preds_path.read_text())
            if instance_id in preds:
                sub = preds[instance_id].get("model_patch", "")
                if sub:
                    patch = sub

        if patch is None and sub_from_traj:
            patch = sub_from_traj

        if patch is None and sub_from_traj_diff:
            patch = sub_from_traj_diff

        # Container git diff fallback.
        # agent used str_replace_editor to modify files but never called submit, and never
        # printed git diff. sub_from_traj and sub_from_traj_diff are both empty, but the
        # container may still have real changes — pull them directly.
        if patch is None:
            _cid = _monitor.container_id if _monitor else None
            if _cid:
                try:
                    _base_c = instance.get("base_commit", "HEAD")
                    _diff_r = subprocess.run(
                        ["docker", "exec", "-w", "/testbed", _cid, "git", "diff", _base_c],
                        capture_output=True, text=True, timeout=30,
                    )
                    _diff_patch = _diff_r.stdout.strip() if _diff_r.returncode == 0 else ""
                    if _diff_patch:
                        print(
                            f"    [agent] container-diff fallback: extracted {len(_diff_patch)}c patch "
                            f"from container {_cid[:12]}...",
                            flush=True,
                        )
                        patch = _diff_patch
                except Exception as _e:
                    print(f"    [agent] container-diff fallback failed: {_e}", flush=True)

        result = AttemptResult(
            patch=patch,
            exit_status=exit_status,
            jingu_body=jingu_body,
            monitor=_monitor,
        )
        return AttemptOutcome(attempt=attempt, result=result)

    def run(self) -> "InstanceResult":
        """Execute the full multi-attempt loop with retry, gate evaluation, and governance.

        Moved from run_with_jingu() in run_with_jingu_gate.py (p225-10).
        Owns: onboarding check, attempt loop, NBR/EFR enforcement, gate evaluation,
        retry planning, early stop handling, candidate selection.
        """
        # Lazy imports to avoid circular dependencies — all from run_with_jingu_gate.py
        # and its re-exports.
        from run_with_jingu_gate import (
            Timer, _timing_root, _instance_timers, _usage_tracker,
            GATE_MODE, RETRY_CONTROLLER_ENABLED, STRUCTURED_OUTPUT_ENABLED,
            STRATEGY_LOG_PATH, STRATEGY_TABLE_PATH,
            _try_parse_structured_output,
            classify_admission, patch_fingerprint, patch_content_hash,
            score_patch, normalize_patch, extract_test_counts,
            check_test_progress_invariant, compute_attempt_delta,
            build_execution_feedback, extract_principal_violation_codes,
            jingu_structural_check,
        )
        from jingu_gate_bridge import evaluate_patch_from_traj
        from retry_controller import build_retry_plan, RetryPlan
        from failure_classifier import (
            classify_failure, get_routing as get_failure_routing,
            classify_failure_layer, route_from_failure,
            route_from_failure_mode,
        )
        from repair_prompts import build_repair_prompt
        from failure_routing import route_failure as route_failure_p216, is_data_driven_routing_enabled
        from strategy_prompts import get_strategy_prompt
        from governance_runtime import (
            run_governance_packs, override_retry_plan_from_pack,
            ExecutionContext as GovExecutionContext,
        )
        from strategy_logger import log_strategy_entry, make_entry as make_strategy_entry
        from declaration_extractor import (
            extract_declaration, extract_last_agent_message, extract_from_structured,
        )
        from patch_signals import extract_patch_signals
        from cognition_check import check_cognition, format_cognition_feedback
        from control.reasoning_state import (
            initial_reasoning_state, update_reasoning_state, decide_next,
            normalize_signals, VerdictStop, VerdictRedirect,
        )
        from control.swe_signal_adapter import extract_verify_signals
        from control.phase_result import build_phase_result, route_from_phase_result
        from signal_extraction import compute_steps_since_last_signal
        from step_monitor_state import early_stop_scope
        from controlled_verify import _check_onboarding, _build_execution_model, _print_execution_model

        instance_id = self._instance["instance_id"]

        t_inst = Timer(f"instance: {instance_id}", parent=_timing_root)
        _instance_timers[instance_id] = t_inst

        print(f"  [jingu] loading instance {instance_id}...")

        # ONBOARDING_FIRST: verify official harness path is known before any execution
        _ok, _reason = _check_onboarding(self._instance)
        if not _ok:
            print(f"[onboarding-check] FAIL: {_reason}")
            return InstanceResult(
                instance_id=instance_id,
                accepted=False,
                patch="",
                attempts=self._max_attempts,
                status="rejected",
                failure_type="ONBOARDING_REQUIRED",
                reason=_reason,
            )
        print("[onboarding-check] PASS")
        _print_execution_model(_build_execution_model(self._instance))

        candidates: list[dict] = []
        attempts_log: list[dict] = []
        last_failure = ""
        _prev_raw_patch = ""
        _no_progress_streak = 0
        total_llm_calls = 0
        _strategy_entries: list[dict] = []
        _past_approach_summaries: list[str] = []  # WS-4: track approach directions across attempts
        self._prev_files_written: set[str] = set()  # P0.1: L2 same-files detection
        self._prev_failure_mode: str | None = None  # P0.2: environment_failure early terminate
        _test_counts_by_attempt: dict[int, int] = {}
        _next_attempt_start_phase: str = "OBSERVE"  # p-fix: repair routing target for next attempt
        _last_failure_type: str = ""  # telemetry: which failure_type drove the routing
        cp_state_holder: list = [initial_reasoning_state("OBSERVE")]
        self._cp_state_holder = cp_state_holder

        for attempt in range(1, self._max_attempts + 1):
            print(f"  [attempt {attempt}/{self._max_attempts}] {instance_id}")

            # Reset cp_state at attempt boundary — phase must match repair routing target
            # (p-fix: without this, cp_state.phase retains attempt N's final phase
            #  while the prompt says "REPAIR PHASE: X" — 100% mismatch on attempt 2+)
            if attempt > 1:
                # Normalize alias → canonical phase name (defense-in-depth)
                # _next_attempt_start_phase is already canonical (from FAILURE_ROUTING_RULES)
                cp_state_holder[0] = initial_reasoning_state(_next_attempt_start_phase)
                print(f"    [cp-reset] attempt={attempt} start_phase={_next_attempt_start_phase}"
                      f" failure_routing_source={_last_failure_type or 'none'}", flush=True)
                _next_attempt_start_phase = "OBSERVE"  # reset for next iteration
            else:
                import dataclasses as _dc_boundary
                cp_state_holder[0] = _dc_boundary.replace(cp_state_holder[0], principal_violation="")

            # NBR enforcement: No Blind Retry
            if attempt > 1 and not last_failure.strip() and self._mode != "baseline":
                raise RuntimeError(
                    f"[NBR violation] attempt {attempt} has empty last_failure. "
                    "Execution feedback is required before retry. "
                    "Check build_execution_feedback() and ensure tests_ran signal is captured."
                )

            outcome = self.run_attempt(attempt, previous_failure=last_failure, parent_timer=t_inst)
            patch = outcome.result.patch
            agent_exit = outcome.result.exit_status
            jingu_body = outcome.result.jingu_body
            _attempt_monitor = outcome.result.monitor

            # Early stop verdict handling
            if _attempt_monitor is not None and _attempt_monitor.early_stop_verdict is not None:
                _esv = _attempt_monitor.early_stop_verdict
                print(
                    f"  [cp] early_stop instance={instance_id} attempt={attempt}"
                    f" reason={_esv.reason} — verdict-driven attempt termination",
                    flush=True,
                )
                if _esv.reason == "no_signal":
                    _mon = _attempt_monitor
                    _tr = (jingu_body or {}).get("test_results", {})
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
                        last_failure = (
                            "Previous attempt stopped early: no progress signal detected "
                            "(control-plane verdict=STOP no_signal). "
                            "Change your approach entirely — avoid repeated reads without writing code."
                        )
                    # p-fix: propagate phase_result routing target to next attempt cp_state
                    if _pr_target:
                        _next_attempt_start_phase = _pr_target.upper()
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
                    break  # task_success = instance-terminal

                # WS-3: step governance timeout — phase-specific failure attribution
                if _esv.reason.startswith("step_governance_timeout_"):
                    _stalled_phase = _esv.reason.replace("step_governance_timeout_", "").upper()
                    last_failure = (
                        f"GOVERNANCE TIMEOUT: You spent too many steps in {_stalled_phase} phase "
                        f"without submitting a phase record. "
                        f"On retry, you MUST submit a phase record within the deadline. "
                        f"Be direct: read the relevant code, form your conclusion, and submit immediately. "
                        f"Do NOT explore endlessly."
                    )
                    print(
                        f"  [cp] step_governance_timeout phase={_stalled_phase}"
                        f" attempt={attempt}/{self._max_attempts}"
                        f" — attempt-terminal, resetting cp_state for next attempt",
                        flush=True,
                    )
                    cp_state_holder[0] = initial_reasoning_state("OBSERVE")
                    continue

                _scope = early_stop_scope(_esv.reason)
                if _scope == "attempt_terminal":
                    if patch:
                        cp_state_holder[0] = initial_reasoning_state("OBSERVE")
                        print(
                            f"  [cp] no_signal attempt={attempt}/{self._max_attempts}"
                            f" — submission preserved ({len(patch)}c patch),"
                            f" falling through to gate (p24 submission persistence)",
                            flush=True,
                        )
                    else:
                        print(
                            f"  [cp] no_signal attempt={attempt}/{self._max_attempts}"
                            f" — attempt-terminal (no patch), resetting cp_state for next attempt",
                            flush=True,
                        )
                        cp_state_holder[0] = initial_reasoning_state("OBSERVE")
                        continue

            # p179: record test counts
            _test_counts_by_attempt[attempt] = extract_test_counts(jingu_body)

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
                    _jb = jingu_body or {}
                    _files_written_count = len(_jb.get("files_written", []))
                    _phase_recs = _jb.get("phase_records", [])
                    _analyze_rec = next((r for r in _phase_recs if r.get("phase") == "ANALYZE"), None)
                    _execute_rec = next((r for r in _phase_recs if r.get("phase") == "EXECUTE"), None)
                    _has_root_cause = bool(_analyze_rec and _analyze_rec.get("root_cause"))
                    _has_plan = bool(_analyze_rec and _analyze_rec.get("plan")) or bool(_execute_rec and _execute_rec.get("plan"))
                    _execution_ready = bool(_execute_rec or _has_plan)
                    if _files_written_count == 0 and _analyze_rec and _execution_ready:
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

            if self._mode == "baseline":
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
                last_failure = ""
                agent_exit = None
            elif GATE_MODE == "trust_gate":
                attempt_dir = self._output_dir / f"attempt_{attempt}"
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
                    grade = gate_result.gate_code
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
                    # Patch bloat detection
                    if attempt >= 2 and len(attempts_log) >= 2:
                        prev = attempts_log[-2].get("patch_fp") or {}
                        prev_size = prev.get("lines_added", 0) + prev.get("lines_removed", 0)
                        curr_size = fp["lines_added"] + fp["lines_removed"]
                        if prev_size > 0 and curr_size > prev_size * 1.5:
                            print(f"    [bloat-warn] attempt {attempt} patch is {curr_size} lines "
                                  f"(+{curr_size - prev_size} vs attempt {attempt-1} {prev_size}). "
                                  f"Possible wrong direction.")
                    # B3: retry-controller
                    if attempt < self._max_attempts:
                        fail_to_pass = _parse_fail_to_pass(self._instance)
                        exec_feedback = build_execution_feedback(
                            jingu_body=jingu_body or {},
                            fail_to_pass_tests=fail_to_pass,
                            patch_fp=fp,
                        )
                        print(f"    [exec-feedback] {exec_feedback[:200]}")
                        # EFR enforcement
                        tests_ran = (jingu_body or {}).get("test_results", {}).get("ran_tests", False)
                        if tests_ran and not exec_feedback.strip():
                            raise RuntimeError(
                                "[EFR violation] tests ran but exec_feedback is empty. "
                                "build_execution_feedback() must extract test output."
                            )
                        # B4: cognition gate
                        _traj_path = self._output_dir / f"attempt_{attempt}" / instance_id / f"{instance_id}.traj.json"
                        _decl = None
                        _traj_msgs_for_signal: list[dict] = []
                        if _traj_path.exists():
                            try:
                                _traj_msgs_for_signal = json.loads(_traj_path.read_text()).get("messages", [])
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
                        _steps_since_signal = compute_steps_since_last_signal(_traj_msgs_for_signal)
                        if _steps_since_signal > 0:
                            print(f"    [no-signal] steps_since_last_signal={_steps_since_signal}")
                        _principal_viol_codes = extract_principal_violation_codes(_decl)
                        if _principal_viol_codes:
                            print(f"    [principal-viol] {_principal_viol_codes}")
                        if RETRY_CONTROLLER_ENABLED:
                            _tests_now = _test_counts_by_attempt.get(attempt, -1)
                            _tests_prev = _test_counts_by_attempt.get(attempt - 1, -1)
                            _tests_delta = (_tests_now - _tests_prev) if _tests_now >= 0 and _tests_prev >= 0 else None
                            _progress_ok, _progress_code = check_test_progress_invariant(_tests_prev, _tests_now)
                            print(f"    [test-progress] ok={_progress_ok}  code={_progress_code}  "
                                  f"prev={_tests_prev}  now={_tests_now}  delta={_tests_delta}")
                            _inner_cv = (jingu_body or {}).get("controlled_verify") or {}
                            prev_fp = attempts_log[-2]["patch_fp"] if len(attempts_log) >= 2 else None
                            t_ctrl = Timer(f"B3 retry-controller attempt={attempt}", parent=t_inst)
                            retry_plan = build_retry_plan(
                                problem_statement=self._instance.get("problem_statement", ""),
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
                                patch_exists=bool(patch and patch.strip()),
                                inner_f2p_passed=_inner_cv.get("f2p_passed") if _inner_cv.get("f2p_passed") is not None else -1,
                                inner_f2p_total=(_inner_cv.get("f2p_passed") or 0) + (_inner_cv.get("f2p_failed") or 0),
                                inner_new_failures=_inner_cv.get("p2p_failed") or 0,
                            )
                            t_ctrl.stop()
                            # p179: override control_action based on TEST_PROGRESS_MONOTONICITY
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
                                _curr_hash = patch_content_hash(patch)
                                _prev_hash = patch_content_hash(_prev_raw_patch) if _prev_raw_patch else None
                                _same_patch = (_prev_hash is not None and _curr_hash == _prev_hash)
                                _patch_direction = "stuck" if _same_patch else "exploring"
                                print(f"    [outcome-gate] NO_PROGRESS direction={_patch_direction} "
                                      f"curr_hash={_curr_hash} prev_hash={_prev_hash}")
                                if _same_patch:
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
                                    _no_progress_streak = 0
                                else:
                                    _no_progress_streak += 1
                                    print(f"    [outcome_gate] consecutive_no_progress={_no_progress_streak} "
                                          f"strategy_change_forced={_no_progress_streak >= 2}")
                                    if _no_progress_streak >= 2:
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
                                _no_progress_streak = 0
                            # GovernancePack pipeline
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
                            # P2: Enrich retry_plan with prediction error feedback
                            _pred_err_data = (jingu_body or {}).get("prediction_error", {})
                            _pred_err_type = _pred_err_data.get("error_type", "")
                            _pred_feedback = _pred_err_data.get("feedback", "")
                            if _pred_err_type in ("prediction_wrong_direction", "prediction_partial") and _pred_feedback:
                                _pred_repair = _pred_err_data.get("repair_target", "")
                                retry_plan = RetryPlan(
                                    root_causes=retry_plan.root_causes + [f"prediction_error={_pred_err_type}"],
                                    must_do=retry_plan.must_do,
                                    must_not_do=retry_plan.must_not_do,
                                    validation_requirement=retry_plan.validation_requirement,
                                    next_attempt_prompt=(
                                        f"[PREDICTION ERROR — {_pred_err_type.upper()}]\n"
                                        f"{_pred_feedback}\n\n"
                                        + retry_plan.next_attempt_prompt
                                    )[:600],
                                    control_action=retry_plan.control_action,
                                    principal_violations=retry_plan.principal_violations,
                                )
                                print(
                                    f"    [p2-prediction] enriched retry_plan:"
                                    f" error={_pred_err_type}"
                                    f" repair_target={_pred_repair}"
                                    f" severity={_pred_err_data.get('severity', '?')}",
                                    flush=True,
                                )
                            print(f"    [retry-ctrl] action={retry_plan.control_action}  "
                                  f"root_causes={retry_plan.root_causes}")
                            print(f"    [retry-ctrl] must_not_do={retry_plan.must_not_do}")
                            print(f"    [retry-ctrl] hint={retry_plan.next_attempt_prompt[:200]}")
                            # Store strategy metadata
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
                                "tests_passed_count": _tests_now,
                                "tests_passed_prev": _tests_prev,
                                "tests_delta": _tests_delta,
                                "progress_code": _progress_code,
                                "files_written_paths": (jingu_body or {}).get("files_written", []),
                            })
                            # B3-CP: update reasoning state
                            _cv_passed = (_strategy_failure_class_v2 == "verified_pass")
                            _verify_partial = extract_verify_signals(controlled_verify_passed=_cv_passed)
                            cp_state_holder[0] = update_reasoning_state(
                                cp_state_holder[0], normalize_signals(_verify_partial)
                            )
                            _cp_state_now = cp_state_holder[0]
                            cp_verdict = decide_next(_cp_state_now)
                            _iid_short = instance_id.split("__")[-1] if "__" in instance_id else instance_id
                            print(f"    [control-plane] instance={_iid_short} attempt={attempt}"
                                  f" state=phase:{_cp_state_now.phase}"
                                  f" step:{_cp_state_now.step_index} no_progress:{_cp_state_now.no_progress_steps}"
                                  f" task_success:{_cp_state_now.task_success}")
                            print(f"    [control-plane] instance={_iid_short} attempt={attempt} verdict={cp_verdict}")
                            if isinstance(cp_verdict, VerdictStop):
                                # p230: log early_stop from control-plane verdict
                                try:
                                    if self._decision_logger is not None:
                                        from decision_logger import DecisionEvent
                                        self._decision_logger.log(DecisionEvent(
                                            decision_type="early_stop",
                                            step_n=-1,
                                            timestamp_ms=time.time() * 1000,
                                            verdict="stop",
                                            reason_text=f"cp_verdict_stop:{cp_verdict.reason}",
                                        ))
                                except Exception:
                                    pass
                                print(f"    [control-plane] instance={_iid_short} STOPPING — reason={cp_verdict.reason}")
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

                            if retry_plan.control_action in ("STOP_FAIL", "STOP_NO_SIGNAL"):
                                # p230: log early_stop from retry controller
                                try:
                                    if self._decision_logger is not None:
                                        from decision_logger import DecisionEvent
                                        self._decision_logger.log(DecisionEvent(
                                            decision_type="early_stop",
                                            step_n=-1,
                                            timestamp_ms=time.time() * 1000,
                                            verdict="stop",
                                            reason_text=f"retry_ctrl:{retry_plan.control_action}",
                                            signals_evaluated={"root_causes": retry_plan.root_causes[:5]},
                                        ))
                                except Exception:
                                    pass
                                print(f"    [retry-ctrl] STOPPING — action={retry_plan.control_action}")
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
                            if _strategy_failure_class_v2 == "verified_pass":
                                # p230: log early_stop from verified_pass
                                try:
                                    if self._decision_logger is not None:
                                        from decision_logger import DecisionEvent
                                        self._decision_logger.log(DecisionEvent(
                                            decision_type="early_stop",
                                            step_n=-1,
                                            timestamp_ms=time.time() * 1000,
                                            verdict="stop",
                                            reason_text="verified_pass",
                                        ))
                                except Exception:
                                    pass
                                print(f"    [retry-ctrl] STOPPING — verified_pass (controlled_verify tests_failed=0)")
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

                            # p230: log retry decision
                            try:
                                if self._decision_logger is not None:
                                    from decision_logger import DecisionEvent
                                    self._decision_logger.log(DecisionEvent(
                                        decision_type="retry_decision",
                                        step_n=-1,
                                        timestamp_ms=time.time() * 1000,
                                        verdict=retry_plan.control_action,
                                        reason_text=retry_plan.next_attempt_prompt[:200],
                                        signals_evaluated={
                                            "root_causes": retry_plan.root_causes[:5],
                                            "tests_delta": _tests_delta,
                                            "progress_code": _progress_code,
                                        },
                                    ))
                            except Exception:
                                pass

                            # ── P0.2: environment_failure early terminate ─────────
                            _fm_now = (jingu_body or {}).get("failure_mode")
                            if _fm_now == "environment_failure" and attempt > 1:
                                _prev_fm = getattr(self, '_prev_failure_mode', None)
                                if _prev_fm == "environment_failure":
                                    print(f"    [env-early-terminate] consecutive environment_failure "
                                          f"(attempt {attempt-1}→{attempt}) — STOPPING (non-retryable)",
                                          flush=True)
                                    jingu_body["no_progress_repeat"] = "environment_failure_consecutive"
                                    break
                            self._prev_failure_mode = _fm_now

                            # ── P0.1: no_progress_repeat_gate ──────────────────────
                            # A/B flag: NPRG_ENABLED=0 disables gate actions (detection still logged)
                            _nprg_enabled = __import__("os").environ.get("NPRG_ENABLED", "1") != "0"
                            _curr_patch_hash = patch_content_hash(patch) if patch else "empty"
                            _prev_patch_hash = patch_content_hash(_prev_raw_patch) if _prev_raw_patch else None
                            _curr_files = set((jingu_body or {}).get("files_written", []))
                            _prev_files_w = getattr(self, '_prev_files_written', set())

                            # Signal 0: always log state for debugging
                            if attempt > 1:
                                print(f"    [nprg_state] attempt={attempt} "
                                      f"curr_files={sorted(_curr_files)} prev_files={sorted(_prev_files_w)} "
                                      f"curr_hash={_curr_patch_hash} prev_hash={_prev_patch_hash} "
                                      f"jb_keys={sorted((jingu_body or {}).keys())[:5]}",
                                      flush=True)

                            # Signal 1: detection (always logged, even when gate OFF)
                            _nprg_l1 = (attempt > 1 and _prev_patch_hash is not None
                                        and _curr_patch_hash != "empty"
                                        and _curr_patch_hash == _prev_patch_hash)
                            _nprg_l2 = (attempt > 1 and _curr_files and _prev_files_w
                                        and _curr_files == _prev_files_w
                                        and not _nprg_l1)
                            if _nprg_l1 or _nprg_l2:
                                _nprg_level = "L1_identical_patch" if _nprg_l1 else "L2_same_files"
                                print(f"    [nprg_detected] attempt={attempt} level={_nprg_level} "
                                      f"enabled={_nprg_enabled} "
                                      f"prev_hash={_prev_patch_hash} curr_hash={_curr_patch_hash} "
                                      f"prev_files={sorted(_prev_files_w)} curr_files={sorted(_curr_files)}",
                                      flush=True)
                                jingu_body["nprg_detected"] = _nprg_level

                            if attempt > 1 and _nprg_enabled:
                                # L1: identical patch → STOP (requires both patches non-empty)
                                if _nprg_l1 and _prev_raw_patch and patch:
                                    print(f"    [nprg_triggered] level=L1 action=STOP "
                                          f"hash={_curr_patch_hash}", flush=True)
                                    jingu_body["no_progress_repeat"] = "L1_identical_patch"
                                    break
                                # L2: same files written → forced direction change (files-based, no patch needed)
                                if _nprg_l2:
                                    print(f"    [nprg_triggered] level=L2 action=FORCE_DIRECTION_CHANGE "
                                          f"files={sorted(_curr_files)}", flush=True)
                                    jingu_body["no_progress_repeat"] = "L2_same_files"
                                    # Signal 3: post-gate state
                                    print(f"    [nprg_post_gate] prev_phase={retry_plan.control_action if retry_plan else 'none'} "
                                          f"new_phase=ADJUST forced=true", flush=True)
                                    retry_plan = RetryPlan(
                                        root_causes=retry_plan.root_causes + ["no_progress_repeat=L2_same_files"],
                                        must_do=[
                                            "You MUST change which file(s) you modify",
                                            "You MUST form a NEW root cause hypothesis",
                                            "Re-read the failing test to find what you missed",
                                        ],
                                        must_not_do=[
                                            f"Do NOT modify {', '.join(sorted(_curr_files))} again",
                                            "Do NOT reuse any part of your previous patch approach",
                                        ],
                                        validation_requirement="Patch must target DIFFERENT files than previous attempt",
                                        next_attempt_prompt=(
                                            "HARD DIRECTION CHANGE REQUIRED: You modified the exact same files "
                                            "as your previous attempt and still failed. Your hypothesis about "
                                            "WHERE the bug is located is wrong. "
                                            "You MUST: (1) identify a DIFFERENT root cause in DIFFERENT files, "
                                            "(2) re-read the failing test to understand what it actually checks, "
                                            "(3) write a fix targeting different code. "
                                            f"BANNED files: {', '.join(sorted(_curr_files))}"
                                        )[:600],
                                        control_action="ADJUST",
                                        principal_violations=retry_plan.principal_violations,
                                    )
                            self._prev_files_written = _curr_files
                            # ── end no_progress_repeat_gate ────────────────────────

                            _prev_raw_patch = patch
                            # WS-4: Track approach direction for exploration enforcement
                            _approach_summary = _extract_approach_summary(jingu_body, patch, fp)
                            if _approach_summary:
                                _past_approach_summaries.append(_approach_summary)
                            last_failure = retry_plan.next_attempt_prompt[:600]
                            # Decision Quality v1: prediction error feedback
                            try:
                                from prediction_feedback import compute_prediction_error, build_prediction_error_hint
                                _decide_rec = next(
                                    (r for r in (jingu_body or {}).get("phase_records", [])
                                     if r.get("phase") == "DECIDE"),
                                    None,
                                )
                                if _decide_rec and _decide_rec.get("testable_hypothesis"):
                                    _cv = (jingu_body or {}).get("controlled_verify", {})
                                    _pred_err = compute_prediction_error(
                                        _decide_rec, _cv,
                                        actual_files_changed=fp.get("files", []) if fp else [],
                                    )
                                    _pred_hint = build_prediction_error_hint(_pred_err, _decide_rec)
                                    if _pred_hint:
                                        last_failure = _pred_hint + "\n\n" + last_failure
                                    print(f"    [prediction-error] score={_pred_err['score']:.2f} "
                                          f"type={_pred_err['error_type']} "
                                          f"hit={_pred_err['pass_hit']:.2f} "
                                          f"miss={_pred_err['pass_miss']:.2f}")
                            except ImportError:
                                pass  # prediction_feedback module not yet available
                            except Exception as _pe_exc:
                                print(f"    [prediction-error] error (non-fatal): {_pe_exc}")
                            _jb_ft = (jingu_body or {}).get("failure_type")
                            _jb_routing = (jingu_body or {}).get("failure_routing")
                            _jb_cv = (jingu_body or {}).get("controlled_verify") or {}
                            if _jb_ft and _jb_routing:
                                _repair = build_repair_prompt(_jb_ft, _jb_cv, _jb_routing)
                                last_failure = _repair + "\n\n" + last_failure
                                # p-fix: propagate repair routing target to next attempt cp_state
                                _next_attempt_start_phase = _jb_routing['next_phase'].upper()
                                _last_failure_type = _jb_ft or ""
                                print(f"    [repair-route] attempt={attempt} failure_type={_jb_ft} "
                                      f"next_phase={_jb_routing['next_phase']}", flush=True)
                            elif not _jb_ft:
                                # P1 fallback: route from failure_mode when CV absent
                                _jb_fm = (jingu_body or {}).get("failure_mode")
                                if _jb_fm:
                                    _fm_routing = route_from_failure_mode(_jb_fm)
                                    _fm_hint = f"[FAILURE MODE: {_jb_fm}] {_fm_routing['repair_goal']}"
                                    last_failure = _fm_hint + "\n\n" + last_failure
                                    _next_attempt_start_phase = _fm_routing['next_phase'].upper()
                                    _last_failure_type = f"fm:{_jb_fm}"
                                    print(f"    [repair-route-fm] attempt={attempt} failure_mode={_jb_fm} "
                                          f"next_phase={_fm_routing['next_phase']}", flush=True)
                            if is_data_driven_routing_enabled():
                                try:
                                    _p216_phase = (jingu_body or {}).get("last_phase", "ANALYZE").upper()
                                    _p216_principal = (jingu_body or {}).get("top_failed_principal", "")
                                    if _p216_principal:
                                        _p216_next, _p216_strategy = route_failure_p216(_p216_phase, _p216_principal)
                                        _p216_prompt = get_strategy_prompt(_p216_strategy)
                                        last_failure = _p216_prompt + "\n\n" + last_failure
                                        # p-fix: data-driven routing overrides repair routing target
                                        _next_attempt_start_phase = _p216_next.upper()
                                        _last_failure_type = f"{_jb_ft or ''}+p216"
                                        print(f"    [p216-routing] attempt={attempt} phase={_p216_phase} "
                                              f"principal={_p216_principal} -> next={_p216_next} "
                                              f"strategy={_p216_strategy}", flush=True)
                                except Exception as _p216_exc:
                                    print(f"    [p216-routing] error (non-fatal): {_p216_exc}", flush=True)
                            # WS-4: Exploration enforcement — warn about repeated approaches
                            if len(_past_approach_summaries) >= 2:
                                _last_approach = _past_approach_summaries[-1]
                                _repeated = sum(1 for a in _past_approach_summaries[:-1] if a == _last_approach)
                                if _repeated > 0:
                                    _past_str = "\n".join(f"  attempt {i+1}: {a}" for i, a in enumerate(_past_approach_summaries))
                                    _exploration_warning = (
                                        f"EXPLORATION ENFORCEMENT: You have tried the same approach {_repeated + 1} times.\n"
                                        f"Past approaches:\n{_past_str}\n"
                                        f"You MUST try a DIFFERENT approach — different files, different root cause hypothesis.\n\n"
                                    )
                                    last_failure = _exploration_warning + last_failure
                                    print(f"    [ws4-exploration] REPEATED approach detected (count={_repeated + 1})")
                        else:
                            # WS-4: Track approach direction (else branch — no retry_plan)
                            _approach_summary = _extract_approach_summary(jingu_body, patch, fp)
                            if _approach_summary:
                                _past_approach_summaries.append(_approach_summary)
                            last_failure = exec_feedback[:400]
                            _jb_ft = (jingu_body or {}).get("failure_type")
                            _jb_routing = (jingu_body or {}).get("failure_routing")
                            _jb_cv = (jingu_body or {}).get("controlled_verify") or {}
                            if _jb_ft and _jb_routing:
                                _repair = build_repair_prompt(_jb_ft, _jb_cv, _jb_routing)
                                last_failure = _repair + "\n\n" + last_failure
                                # p-fix: propagate repair routing target to next attempt cp_state
                                _next_attempt_start_phase = _jb_routing['next_phase'].upper()
                                _last_failure_type = _jb_ft or ""
                                print(f"    [repair-route] attempt={attempt} failure_type={_jb_ft} "
                                      f"next_phase={_jb_routing['next_phase']}", flush=True)
                            elif not _jb_ft:
                                # P1 fallback: route from failure_mode when CV absent
                                _jb_fm = (jingu_body or {}).get("failure_mode")
                                if _jb_fm:
                                    _fm_routing = route_from_failure_mode(_jb_fm)
                                    _fm_hint = f"[FAILURE MODE: {_jb_fm}] {_fm_routing['repair_goal']}"
                                    last_failure = _fm_hint + "\n\n" + last_failure
                                    _next_attempt_start_phase = _fm_routing['next_phase'].upper()
                                    _last_failure_type = f"fm:{_jb_fm}"
                                    print(f"    [repair-route-fm] attempt={attempt} failure_mode={_jb_fm} "
                                          f"next_phase={_fm_routing['next_phase']}", flush=True)
                            if is_data_driven_routing_enabled():
                                try:
                                    _p216_phase = (jingu_body or {}).get("last_phase", "ANALYZE").upper()
                                    _p216_principal = (jingu_body or {}).get("top_failed_principal", "")
                                    if _p216_principal:
                                        _p216_next, _p216_strategy = route_failure_p216(_p216_phase, _p216_principal)
                                        _p216_prompt = get_strategy_prompt(_p216_strategy)
                                        last_failure = _p216_prompt + "\n\n" + last_failure
                                        # p-fix: data-driven routing overrides repair routing target
                                        _next_attempt_start_phase = _p216_next.upper()
                                        _last_failure_type = f"{_jb_ft or ''}+p216"
                                        print(f"    [p216-routing] attempt={attempt} phase={_p216_phase} "
                                              f"principal={_p216_principal} -> next={_p216_next} "
                                              f"strategy={_p216_strategy}", flush=True)
                                except Exception as _p216_exc:
                                    print(f"    [p216-routing] error (non-fatal): {_p216_exc}", flush=True)
                            # ── P0.1/P0.2 (else branch): same gates apply ──────
                            _fm_now_e = (jingu_body or {}).get("failure_mode")
                            if _fm_now_e == "environment_failure" and attempt > 1:
                                _prev_fm_e = getattr(self, '_prev_failure_mode', None)
                                if _prev_fm_e == "environment_failure":
                                    print(f"    [env-early-terminate] consecutive environment_failure "
                                          f"(attempt {attempt-1}→{attempt}) — STOPPING",
                                          flush=True)
                                    jingu_body["no_progress_repeat"] = "environment_failure_consecutive"
                                    break
                            self._prev_failure_mode = _fm_now_e
                            _nprg_enabled_e = __import__("os").environ.get("NPRG_ENABLED", "1") != "0"
                            _curr_ph_e = patch_content_hash(patch) if patch else "empty"
                            _prev_ph_e = patch_content_hash(_prev_raw_patch) if _prev_raw_patch else None
                            _curr_files_e = set((jingu_body or {}).get("files_written", []))
                            _prev_files_e = getattr(self, '_prev_files_written', set())

                            # Signal 0: debug state (else branch)
                            if attempt > 1:
                                print(f"    [nprg_state] attempt={attempt} branch=else "
                                      f"curr_files={sorted(_curr_files_e)} prev_files={sorted(_prev_files_e)} "
                                      f"curr_hash={_curr_ph_e} prev_hash={_prev_ph_e}",
                                      flush=True)

                            # Signal 1: detection (else branch)
                            _nprg_l1_e = (attempt > 1 and _prev_ph_e is not None
                                          and _curr_ph_e != "empty"
                                          and _curr_ph_e == _prev_ph_e)
                            _nprg_l2_e = (attempt > 1 and _curr_files_e and _prev_files_e
                                          and _curr_files_e == _prev_files_e
                                          and not _nprg_l1_e)
                            if _nprg_l1_e or _nprg_l2_e:
                                _nprg_lvl_e = "L1_identical_patch" if _nprg_l1_e else "L2_same_files"
                                print(f"    [nprg_detected] attempt={attempt} level={_nprg_lvl_e} "
                                      f"enabled={_nprg_enabled_e} branch=else "
                                      f"prev_hash={_prev_ph_e} curr_hash={_curr_ph_e} "
                                      f"prev_files={sorted(_prev_files_e)} curr_files={sorted(_curr_files_e)}",
                                      flush=True)
                                jingu_body["nprg_detected"] = _nprg_lvl_e

                            if attempt > 1 and _nprg_enabled_e:
                                if _nprg_l1_e and _prev_raw_patch and patch:
                                    print(f"    [nprg_triggered] level=L1 action=STOP branch=else "
                                          f"hash={_curr_ph_e}", flush=True)
                                    jingu_body["no_progress_repeat"] = "L1_identical_patch"
                                    break
                                # L2 in else branch: no retry_plan to modify, just record
                                if _nprg_l2_e:
                                    print(f"    [nprg_triggered] level=L2 action=RECORD_ONLY branch=else "
                                          f"files={sorted(_curr_files_e)}", flush=True)
                                    jingu_body["no_progress_repeat"] = "L2_same_files"
                            self._prev_files_written = _curr_files_e
                            _prev_raw_patch = patch
                            # WS-4: Exploration enforcement (else branch — no retry_plan)
                            if len(_past_approach_summaries) >= 2:
                                _last_approach = _past_approach_summaries[-1]
                                _repeated = sum(1 for a in _past_approach_summaries[:-1] if a == _last_approach)
                                if _repeated > 0:
                                    _past_str = "\n".join(f"  attempt {i+1}: {a}" for i, a in enumerate(_past_approach_summaries))
                                    _exploration_warning = (
                                        f"EXPLORATION ENFORCEMENT: You have tried the same approach {_repeated + 1} times.\n"
                                        f"Past approaches:\n{_past_str}\n"
                                        f"You MUST try a DIFFERENT approach — different files, different root cause hypothesis.\n\n"
                                    )
                                    last_failure = _exploration_warning + last_failure
                                    print(f"    [ws4-exploration] REPEATED approach detected (count={_repeated + 1})")
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

        # Flush strategy log entries
        if STRATEGY_LOG_PATH and _strategy_entries:
            _inst_final_admitted = bool(candidates)
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
            # Grab failure_layer + failure_record from last attempt's jingu_body
            _last_fl = None
            _last_fr = None
            if jingu_body and isinstance(jingu_body, dict):
                _last_fl = jingu_body.get("failure_layer")
                _last_fr = jingu_body.get("failure_record")
            return InstanceResult(
                instance_id=instance_id,
                accepted=False,
                patch="",
                attempts=self._max_attempts,
                elapsed_s=t_inst.elapsed,
                model_usage=inst_usage,
                attempts_log=attempts_log,
                attempt_delta=delta,
                failure_layer=_last_fl,
                failure_record=_last_fr,
            )

        best = max(candidates, key=lambda c: c["score"])
        gate_code = best.get("gate_code", "ADMITTED")
        best_admission = next(
            (a["admission_reason"] for a in attempts_log if a["attempt"] == best["attempt"]),
            gate_code.lower(),
        )
        print(f"  [result] ACCEPTED  best_attempt={best['attempt']}  score={best['score']:.0f}  "
              f"gate={gate_code}  admission={best_admission}  elapsed={t_inst.elapsed:.1f}s  "
              f"bedrock_calls={llm_calls}  cost=${inst_usage.get('cost_usd', 0):.4f}")
        return InstanceResult(
            instance_id=instance_id,
            accepted=True,
            patch=best["patch"],
            attempts=self._max_attempts,
            best_attempt=best["attempt"],
            score=best["score"],
            gate_code=gate_code,
            gate_reason_codes=best.get("gate_reason_codes", []),
            admission_reason=best_admission,
            elapsed_s=t_inst.elapsed,
            model_usage=inst_usage,
            attempts_log=attempts_log,
            attempt_delta=delta,
        )


# ---------------------------------------------------------------------------
# JinguDefaultAgent — DefaultAgent subclass that calls on_attempt_end()
# ---------------------------------------------------------------------------

class JinguDefaultAgent(ProgressTrackingAgent):
    """ProgressTrackingAgent subclass with full Jingu governance lifecycle.

    Combines:
    - Per-step governance: delegates step() to jingu_agent.on_step_start / on_step_end
      (same as JinguProgressTrackingAgent)
    - End-of-attempt governance: overrides run() to call jingu_agent.on_attempt_end()
      after super().run() returns — while the Docker container is still alive.

    Extends ProgressTrackingAgent (not raw DefaultAgent) so that jingu_process_instance()
    can pass progress_manager= and instance_id= without extra plumbing.

    This class replaces the combination of:
    - JinguProgressTrackingAgent (step-level governance)
    - ScopedPatch on DefaultAgent.run (end-of-attempt governance, pre-p225-08)
    from run_with_jingu_gate.py.

    Container lifecycle invariant: on_attempt_end() runs before env goes out of scope
    in jingu_process_instance(). DefaultAgent.run() does not close the Docker env —
    env is GC'd after jingu_process_instance()'s try block exits.
    """

    def __init__(self, *args: Any, jingu_agent: "JinguAgent", **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.jingu_agent = jingu_agent

    def step(self) -> dict:
        from step_monitor_state import StopExecution

        self.jingu_agent.on_step_start(self, self.n_calls)
        result = super().step()
        decision = self.jingu_agent.on_step_end(self, self.n_calls)

        if decision.action == "stop":
            raise StopExecution(decision.reason)
        if decision.action == "redirect":
            self.messages.append({"role": "user", "content": decision.message})

        return result

    def run(self, *args: Any, **kwargs: Any) -> dict:
        from step_monitor_state import StopExecution

        try:
            result = super().run(*args, **kwargs)
        except StopExecution:
            # p241: StopExecution bypassed on_attempt_end → controlled_verify never ran.
            # Extract submission from agent messages (if agent submitted before budget exhausted).
            submission = ""
            for msg in reversed(self.messages):
                extra = msg.get("extra", {})
                if isinstance(extra, dict) and extra.get("submission"):
                    submission = extra["submission"]
                    break
            print(f"    [governance] StopExecution caught in JinguDefaultAgent.run()"
                  f" — running forced on_attempt_end (submission={'yes' if submission else 'no'})",
                  flush=True)
            self.jingu_agent.on_attempt_end(self, submission)
            raise  # re-raise so outer handler (run_attempt line 1162) still works

        submission = result.get("submission", "") if isinstance(result, dict) else ""
        self.jingu_agent.on_attempt_end(self, submission)
        return result


# ---------------------------------------------------------------------------
# JinguProgressTrackingAgent — ProgressTrackingAgent with governance hooks
# ---------------------------------------------------------------------------

class JinguProgressTrackingAgent(ProgressTrackingAgent):
    """ProgressTrackingAgent subclass that delegates step lifecycle to JinguAgent.

    Constructor accepts an extra *jingu_agent* keyword argument (the orchestrator).
    On each step(), it calls jingu_agent.on_step_start / on_step_end and acts on
    the returned StepDecision.
    """

    def __init__(self, *args: Any, jingu_agent: JinguAgent, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.jingu_agent = jingu_agent

    def step(self) -> dict:
        from step_monitor_state import StopExecution

        self.jingu_agent.on_step_start(self, self.n_calls)
        result = super().step()
        decision = self.jingu_agent.on_step_end(self, self.n_calls)

        if decision.action == "stop":
            raise StopExecution(decision.reason)
        if decision.action == "redirect":
            self.messages.append({"role": "user", "content": decision.message})

        return result
