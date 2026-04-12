"""Replay engine — resume agent execution from a checkpoint snapshot.

p232: Core replay capability. Loads a checkpoint (messages + control-plane state),
optionally applies modifications (hint injection, prompt replacement), then
continues execution with real LLM API calls from that conversation point.

The replay approach:
  - Load checkpoint messages + state
  - Create a NEW Docker container (fresh repo state, no patches applied)
  - The LLM sees full message history from the checkpoint and continues from context
  - File state in the new container is base state — the LLM has conversation context
    to know what it previously did

All operations are exception-safe at the top level — replay failures produce
a ReplayResult with error information, never crash the caller.
"""

from __future__ import annotations

import json
import logging
import os
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from checkpoint import Checkpoint, load_checkpoint

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ReplayModifications:
    """Modifications to apply before resuming from checkpoint.

    All fields are optional — None means "no change".
    """

    inject_hint: str | None = None
    """Appended as a user message before resuming (e.g., strategic hint)."""

    replace_system_prompt: str | None = None
    """Replaces the system prompt (messages[0]) entirely."""

    replace_phase_prompt: dict[str, str] | None = None
    """Replaces specific phase prompts by name (key=phase, value=new prompt)."""

    inject_user_message: str | None = None
    """Injected as the next user message (after any hint)."""


@dataclass
class ReplayResult:
    """Result of a replay-from-checkpoint execution."""

    success: bool
    """Whether the replay completed without infrastructure errors."""

    checkpoint_step: int
    """Step number from the checkpoint we resumed from."""

    checkpoint_trigger: str
    """Trigger type of the checkpoint (phase_advance, gate_stop, etc.)."""

    modifications_applied: list[str] = field(default_factory=list)
    """List of modification names that were applied."""

    total_steps: int = 0
    """Number of new steps executed during replay."""

    traj_path: str | None = None
    """Path to the trajectory file from the replay, if available."""

    output_dir: str = ""
    """Directory where replay artifacts are saved."""

    cost: dict = field(default_factory=dict)
    """Model usage/cost information."""

    error: str | None = None
    """Error message if replay failed."""

    elapsed_s: float = 0.0
    """Wall-clock time for the replay execution."""

    instance_id: str = ""
    """Instance ID from the checkpoint."""

    attempt: int = 0
    """Attempt number used for the replay."""

    patch: str = ""
    """Patch produced by the replay (if any)."""


# ---------------------------------------------------------------------------
# Message modification helpers
# ---------------------------------------------------------------------------

def _apply_modifications(
    messages: list[dict[str, Any]],
    modifications: ReplayModifications | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Apply modifications to checkpoint messages.

    Returns (modified_messages, list_of_applied_modification_names).
    """
    if modifications is None:
        return messages, []

    applied: list[str] = []
    msgs = [dict(m) for m in messages]  # shallow copy each message

    # 1. Replace system prompt
    if modifications.replace_system_prompt is not None:
        if msgs and msgs[0].get("role") == "system":
            msgs[0]["content"] = modifications.replace_system_prompt
            applied.append("replace_system_prompt")
        else:
            # Insert system message at front
            msgs.insert(0, {"role": "system", "content": modifications.replace_system_prompt})
            applied.append("replace_system_prompt(inserted)")

    # 2. Replace phase prompts (search for phase markers in user messages)
    if modifications.replace_phase_prompt:
        for phase_name, new_prompt in modifications.replace_phase_prompt.items():
            marker = f"PHASE: {phase_name}"
            for i, m in enumerate(msgs):
                if m.get("role") == "user" and marker in str(m.get("content", "")):
                    msgs[i]["content"] = new_prompt
                    applied.append(f"replace_phase_prompt({phase_name})")
                    break

    # 3. Inject hint as user message
    if modifications.inject_hint is not None:
        msgs.append({
            "role": "user",
            "content": f"[REPLAY HINT] {modifications.inject_hint}",
        })
        applied.append("inject_hint")

    # 4. Inject user message
    if modifications.inject_user_message is not None:
        msgs.append({
            "role": "user",
            "content": modifications.inject_user_message,
        })
        applied.append("inject_user_message")

    return msgs, applied


# ---------------------------------------------------------------------------
# Core replay function
# ---------------------------------------------------------------------------

def replay_from_checkpoint(
    checkpoint_path: Path,
    output_dir: Path,
    *,
    modifications: ReplayModifications | None = None,
    max_steps: int | None = None,
    mode: str = "jingu",
) -> ReplayResult:
    """Replay agent execution from a checkpoint.

    Loads the checkpoint, applies optional modifications, then runs a new
    agent attempt using the checkpoint's message history as context.

    Args:
        checkpoint_path: Path to checkpoint .json.gz file.
        output_dir: Root directory for replay output artifacts.
        modifications: Optional modifications to apply before resuming.
        max_steps: Override max steps for the replay (None = use default).
        mode: Agent mode ("jingu" or "baseline").

    Returns:
        ReplayResult with execution details. On infrastructure error,
        success=False and error is populated.
    """
    t_start = time.monotonic()

    # --- 1. Load checkpoint ---
    ckpt = load_checkpoint(checkpoint_path)
    if ckpt is None:
        return ReplayResult(
            success=False,
            checkpoint_step=0,
            checkpoint_trigger="unknown",
            error=f"Failed to load checkpoint from {checkpoint_path}",
            elapsed_s=time.monotonic() - t_start,
        )

    instance_id = ckpt.instance_id
    ckpt_step = ckpt.step_n
    ckpt_trigger = ckpt.trigger

    print(
        f"  [replay] loaded checkpoint: instance={instance_id} "
        f"step={ckpt_step} trigger={ckpt_trigger} "
        f"messages={len(ckpt.messages_so_far)} "
        f"phase_records={len(ckpt.phase_records)}",
        flush=True,
    )

    # --- 2. Apply modifications to messages ---
    modified_messages, applied_mods = _apply_modifications(
        ckpt.messages_so_far, modifications,
    )

    if applied_mods:
        print(f"  [replay] modifications applied: {', '.join(applied_mods)}", flush=True)

    # --- 3. Create output directory ---
    replay_dir = output_dir / f"replay_from_step_{ckpt_step}"
    replay_dir.mkdir(parents=True, exist_ok=True)

    # Save replay metadata
    replay_meta = {
        "checkpoint_path": str(checkpoint_path),
        "checkpoint_step": ckpt_step,
        "checkpoint_trigger": ckpt_trigger,
        "instance_id": instance_id,
        "original_attempt": ckpt.attempt,
        "modifications_applied": applied_mods,
        "messages_count": len(modified_messages),
        "timestamp": time.time(),
    }
    try:
        meta_path = replay_dir / "replay_meta.json"
        with open(meta_path, "w") as f:
            json.dump(replay_meta, f, indent=2)
    except Exception as e:
        logger.warning("[replay] failed to save metadata: %s", e)

    # --- 4. Reconstruct instance data ---
    # The checkpoint metadata should contain model info; instance data comes from
    # the checkpoint's metadata or must be provided externally.
    try:
        instance = _reconstruct_instance(ckpt)
    except Exception as e:
        return ReplayResult(
            success=False,
            checkpoint_step=ckpt_step,
            checkpoint_trigger=ckpt_trigger,
            modifications_applied=applied_mods,
            output_dir=str(replay_dir),
            error=f"Failed to reconstruct instance data: {e}",
            elapsed_s=time.monotonic() - t_start,
            instance_id=instance_id,
        )

    # --- 5. Build JinguAgent and run attempt ---
    try:
        result = _run_replay_attempt(
            instance=instance,
            output_dir=replay_dir,
            modified_messages=modified_messages,
            checkpoint=ckpt,
            mode=mode,
            max_steps=max_steps,
        )
    except Exception as e:
        tb = traceback.format_exc()
        logger.error("[replay] attempt execution failed: %s\n%s", e, tb)
        return ReplayResult(
            success=False,
            checkpoint_step=ckpt_step,
            checkpoint_trigger=ckpt_trigger,
            modifications_applied=applied_mods,
            output_dir=str(replay_dir),
            error=f"Replay execution failed: {e}",
            elapsed_s=time.monotonic() - t_start,
            instance_id=instance_id,
        )

    elapsed = time.monotonic() - t_start

    return ReplayResult(
        success=True,
        checkpoint_step=ckpt_step,
        checkpoint_trigger=ckpt_trigger,
        modifications_applied=applied_mods,
        total_steps=result.get("total_steps", 0),
        traj_path=result.get("traj_path"),
        output_dir=str(replay_dir),
        cost=result.get("cost", {}),
        elapsed_s=elapsed,
        instance_id=instance_id,
        attempt=result.get("attempt", 0),
        patch=result.get("patch", ""),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _reconstruct_instance(ckpt: Checkpoint) -> dict:
    """Reconstruct the SWE-bench instance dict from checkpoint metadata.

    The checkpoint stores instance_id and metadata (phase, model, etc.).
    For full replay, we need the complete instance dict which includes
    problem_statement, FAIL_TO_PASS, repo, version, etc.

    Strategy: look for instance data in the checkpoint's metadata,
    or fall back to loading from the dataset.
    """
    meta = ckpt.metadata or {}

    # If checkpoint metadata contains the full instance, use it directly
    if "instance" in meta and isinstance(meta["instance"], dict):
        return meta["instance"]

    # Otherwise, reconstruct minimal instance from available data
    instance: dict[str, Any] = {
        "instance_id": ckpt.instance_id,
    }

    # Extract problem_statement from the first user message in the conversation
    # (it's typically in the instance_template which becomes the first user message)
    for msg in ckpt.messages_so_far:
        if msg.get("role") == "user":
            content = str(msg.get("content", ""))
            if len(content) > 200:  # problem statements are typically long
                instance["problem_statement"] = content
                break

    # Extract FAIL_TO_PASS from prompt sections if available
    for section in (ckpt.prompt_sections or []):
        if section.get("name") == "fail_to_pass":
            content = section.get("content", "")
            # Parse test names from "- test_name" lines
            tests = []
            for line in content.split("\n"):
                line = line.strip()
                if line.startswith("- "):
                    tests.append(line[2:].strip())
            if tests:
                instance["FAIL_TO_PASS"] = tests
            break

    # Extract repo/version from instance_id (format: repo__repo-NNNNN)
    parts = ckpt.instance_id.rsplit("-", 1)
    if len(parts) == 2:
        repo_part = parts[0].replace("__", "/")
        instance.setdefault("repo", repo_part)

    return instance


def _run_replay_attempt(
    *,
    instance: dict,
    output_dir: Path,
    modified_messages: list[dict[str, Any]],
    checkpoint: Checkpoint,
    mode: str,
    max_steps: int | None,
) -> dict[str, Any]:
    """Execute the replay attempt using JinguAgent infrastructure.

    Creates a JinguAgent, injects the checkpoint's message history,
    and runs a single attempt. The LLM sees the full conversation
    history and continues from that context.

    Returns dict with: total_steps, traj_path, cost, attempt, patch.
    """
    from jingu_agent import JinguAgent, jingu_process_instance, JinguDefaultAgent
    from jingu_onboard import onboard
    from step_monitor_state import StepMonitorState, StopExecution
    from minisweagent.config import get_config_from_spec
    from minisweagent.utils.serialize import recursive_merge
    from minisweagent.run.benchmarks.swebench import RunBatchProgressManager

    instance_id = instance["instance_id"]
    replay_attempt = checkpoint.attempt + 100  # offset to distinguish from original attempts

    print(
        f"  [replay] starting replay attempt={replay_attempt} "
        f"instance={instance_id} messages={len(modified_messages)}",
        flush=True,
    )

    # Load governance
    try:
        governance = onboard()
    except Exception as e:
        logger.warning("[replay] governance load failed (proceeding without): %s", e)
        governance = None

    # Build config
    from run_with_jingu_gate import BASE_CONFIG
    config = get_config_from_spec("jingu-swebench.yaml")
    config = recursive_merge(config, BASE_CONFIG)

    # Override max_steps if specified
    if max_steps is not None:
        config.setdefault("agent", {})["max_steps"] = max_steps

    # Create the replay agent instance directory
    instance_dir = output_dir / instance_id
    instance_dir.mkdir(parents=True, exist_ok=True)

    # Initialize progress manager (minimal, for API compatibility)
    progress = RunBatchProgressManager(num_instances=1)

    # Restore StepMonitorState from checkpoint
    _monitor = StepMonitorState.from_checkpoint_dict(
        checkpoint.cp_state, instance=instance,
    )
    _monitor.attempt = replay_attempt

    # Run the agent with message injection.
    # The key insight: we use jingu_process_instance with a custom agent class
    # that pre-loads the checkpoint messages before the LLM loop starts.
    #
    # We create a closure-based agent class that injects messages after
    # the agent is constructed but before the step loop begins.

    _injected_messages = modified_messages
    _replay_monitor = _monitor

    class ReplayAgent(JinguDefaultAgent):
        """Agent subclass that injects checkpoint messages before running.

        On run(), injects the full checkpoint conversation history into
        self.messages before calling super().run(). The LLM then sees
        the full history and continues from that context.

        The Docker container is fresh (no patches applied), but the LLM
        has the conversation context to know what it previously did.
        """

        def run(self, *args: Any, **kwargs: Any) -> dict:
            # Inject checkpoint messages (replace the initial messages
            # which only contain the system prompt + task description)
            if _injected_messages:
                # Keep only messages after the initial system+task setup
                # The agent's self.messages has [system, task_description] at this point.
                # We replace with the full checkpoint history.
                self.messages = list(_injected_messages)

            print(
                f"    [replay-agent] injected {len(self.messages)} messages "
                f"from checkpoint, starting LLM loop",
                flush=True,
            )
            return super().run(*args, **kwargs)

    # Create a JinguAgent to provide governance hooks
    jingu_agent = JinguAgent(
        instance=instance,
        output_dir=output_dir,
        governance=governance,
        mode=mode,
        max_attempts=1,  # replay = single attempt
    )
    # Wire up the monitor state
    jingu_agent._state = _replay_monitor

    # Run via jingu_process_instance with our ReplayAgent
    traj_path = None
    patch = ""
    try:
        jingu_process_instance(
            instance, instance_dir, config, progress,
            agent_class=ReplayAgent,
            agent_kwargs={"jingu_agent": jingu_agent},
        )
    except StopExecution as e:
        print(f"  [replay] StopExecution: {e.reason}", flush=True)
    except Exception as e:
        logger.error("[replay] agent execution error: %s", e)
        traceback.print_exc()

    # Collect results
    _traj_path = instance_dir / instance_id / f"{instance_id}.traj.json"
    total_steps = 0
    if _traj_path.exists():
        traj_path = str(_traj_path)
        try:
            traj_data = json.loads(_traj_path.read_text())
            # Count new steps (messages beyond the injected ones)
            all_msgs = traj_data.get("messages", [])
            total_steps = max(0, len(all_msgs) - len(modified_messages))
            patch = traj_data.get("info", {}).get("submission", "") or ""
        except Exception:
            pass

    # Check preds.json for submission
    preds_path = instance_dir / "preds.json"
    if not patch and preds_path.exists():
        try:
            preds = json.loads(preds_path.read_text())
            for pred in (preds if isinstance(preds, list) else [preds]):
                if pred.get("instance_id") == instance_id:
                    patch = pred.get("model_patch", "") or ""
                    break
        except Exception:
            pass

    return {
        "total_steps": total_steps,
        "traj_path": traj_path,
        "cost": {},  # TODO: integrate with ModelUsage tracking
        "attempt": replay_attempt,
        "patch": patch,
    }
