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
"""

import json
import logging
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

    def on_container_ready(self, container_id: str) -> None:  # noqa: ARG002
        """Called once the SWE-bench container is running."""

    def on_attempt_end(self, agent_self: Any, submission: str | None) -> None:
        """Called after each attempt completes. Must be overridden."""
        raise NotImplementedError

    # -- top-level lifecycle ------------------------------------------------

    def run_attempt(self, attempt: int, previous_failure: str | None) -> Any:
        """Execute a single attempt. Must be overridden."""
        raise NotImplementedError

    def run(self) -> Any:
        """Execute the full multi-attempt loop. Must be overridden."""
        raise NotImplementedError


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
