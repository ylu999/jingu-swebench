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

    def on_step_start(self, agent_self: Any, step_n: int) -> None:  # noqa: ARG002
        """Called before each agent step. Override to add pre-step governance."""

    def on_step_end(self, agent_self: Any, step_n: int) -> StepDecision:  # noqa: ARG002
        """Called after each agent step. Return a StepDecision to control flow."""
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
