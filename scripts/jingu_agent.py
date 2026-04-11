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
"""

import json
import logging
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from minisweagent.models import get_model
from minisweagent.run.benchmarks.swebench import (
    ProgressTrackingAgent,
    get_sb_environment,
    remove_from_preds_file,
    update_preds_file,
)
from minisweagent.run.benchmarks.utils.batch_progress import RunBatchProgressManager
from minisweagent.utils.log import logger

# Type alias for agent classes that are compatible with process_instance flow.
# Must accept (model, env, *, progress_manager, instance_id, **agent_config).
AgentClass = type


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

    # -- step-level hooks (called by JinguProgressTrackingAgent.step) --------

    def on_step_start(self, agent_self: Any, step_n: int) -> None:
        """Called before each agent step.

        Runs observation (Section 1) and detects container readiness.
        Stores observation results on self for use by on_step_end().
        """
        from step_sections import _step_observe

        # Wire monitor state onto agent instance for _step_observe dedup
        if self._state is not None:
            agent_self._jingu_monitor_state = self._state

        text, snippet, env_error = _step_observe(
            agent_self, step_n=step_n, mode=self._mode
        )
        # Stash for on_step_end
        self._last_observe_result = (text, snippet, env_error)

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

        # Section 2: verify
        patch_non_empty = _step_verify_if_needed(
            agent_self, state=self._state, verify_debounce_s=5.0
        )

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

        # Check for early stop or redirect from state
        if self._state.early_stop_verdict:
            return StepDecision(
                action="stop",
                reason=getattr(self._state.early_stop_verdict, "reason", "no_signal"),
            )
        if self._state.pending_redirect_hint:
            hint = self._state.pending_redirect_hint
            self._state.pending_redirect_hint = ""
            return StepDecision(action="redirect", message=hint)

        return StepDecision(action="continue")

    # -- attempt-level hooks ------------------------------------------------

    def on_attempt_start(self, attempt: int, previous_failure: str | None) -> str:  # noqa: ARG002
        """Called before each attempt. Returns initial hint string (may be empty)."""
        return ""

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

        _monitor = self._state
        if _monitor is None:
            return

        cid = getattr(getattr(agent_self, "env", None), "container_id", None)
        if not cid:
            return
        submitted = submission or ""
        if not submitted:
            return

        print(f"    [controlled-verify] final verify on container {cid[:12]}...", flush=True)

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
        cv_result = run_controlled_verify(submitted, self._instance, cid, timeout_s=60)
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

    def run_attempt(self, attempt: int, previous_failure: str | None) -> Any:
        """Execute a single attempt. Must be overridden."""
        raise NotImplementedError

    def run(self) -> Any:
        """Execute the full multi-attempt loop. Must be overridden."""
        raise NotImplementedError


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
        result = super().run(*args, **kwargs)
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
