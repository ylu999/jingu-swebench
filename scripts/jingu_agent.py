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
        except Exception as _onb_exc:
            print(f"    [jingu_onboard] prompt load error (fallback): {_onb_exc}", flush=True)

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

        fail_to_pass = self._instance.get("FAIL_TO_PASS", [])
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

        print(f"    [agent] running {instance_id} attempt={attempt}...")

        preds_path = attempt_dir / "preds.json"
        progress = RunBatchProgressManager(num_instances=1)

        # Initialize StepMonitorState for this attempt.
        _monitor = StepMonitorState(
            instance_id=instance_id,
            attempt=attempt,
            instance=instance,
        )
        # p226-05: per-attempt extraction metrics counters
        _monitor._extraction_structured = 0
        _monitor._extraction_regex_fallback = 0
        _monitor._extraction_no_schema = 0

        self._state = _monitor
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
                # p226-05: structured extraction metrics — track structured vs regex fallback rates
                _em_structured = getattr(_monitor, "_extraction_structured", 0)
                _em_regex = getattr(_monitor, "_extraction_regex_fallback", 0)
                _em_no_schema = getattr(_monitor, "_extraction_no_schema", 0)
                _em_total = _em_structured + _em_regex + _em_no_schema
                jingu_body["extraction_metrics"] = {
                    "structured": _em_structured,
                    "regex_fallback": _em_regex,
                    "no_schema": _em_no_schema,
                    "total": _em_total,
                }
                print(
                    f"    [extraction_metrics] attempt={attempt}"
                    f" structured={_em_structured}/{_em_total}"
                    f" regex_fallback={_em_regex}/{_em_total}"
                    f" no_schema={_em_no_schema}/{_em_total}",
                    flush=True,
                )
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
