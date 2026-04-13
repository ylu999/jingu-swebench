"""
StepMonitorState — shared mutable state for one agent run's step monitor.

Extracted from run_with_jingu_gate.py (p225-01) to reduce file size and
enable independent testing of step-monitor logic.
"""

import threading
import time

from control.reasoning_state import (
    initial_reasoning_state, update_reasoning_state,
    normalize_signals,
)
from control.swe_signal_adapter import extract_step_signals


class StopExecution(Exception):
    """Raised by _monitored_step when VerdictStop is issued.
    Immediately interrupts the agent step loop — no delayed enforcement via n_calls.
    Caught in run_agent's process_instance wrapper; treated as a clean early exit.
    """
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"StopExecution: {reason}")


# Stop-scope taxonomy (Bug A fix, p17):
# Determines whether an early_stop_verdict terminates the current attempt only
# (attempt-terminal → continue to next attempt) or the whole instance
# (instance-terminal → break attempt loop entirely).
#
# Extracted as a pure function so it can be unit-tested independently of
# the attempt loop machinery in run_with_jingu().
_INSTANCE_TERMINAL_REASONS = frozenset({"task_success"})
_ATTEMPT_TERMINAL_REASONS  = frozenset({"no_signal"})

def early_stop_scope(reason: str) -> str:
    """Return the scope of an early-stop verdict.

    Returns:
        "instance_terminal" — break the attempt loop (verified pass or hard stop)
        "attempt_terminal"  — continue to next attempt (retriable failure)
        "unknown"           — unrecognised reason; caller should treat conservatively
    """
    if reason in _INSTANCE_TERMINAL_REASONS:
        return "instance_terminal"
    if reason in _ATTEMPT_TERMINAL_REASONS:
        return "attempt_terminal"
    # Step-governance timeouts are attempt-terminal (dynamic reasons with phase suffix)
    if reason.startswith("step_governance_timeout_"):
        return "attempt_terminal"
    return "unknown"


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
        self._lock = threading.Lock()
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
        # p211: analysis gate reject counter — escape hatch after N rejects
        self.analysis_gate_rejects: int = 0
        self.design_gate_rejects: int = 0
        # P16: RETRYABLE loop breaker — counts consecutive identical (phase, reason) RETRYABLE.
        # Same phase + same reason N times in a row → ESCALATE_CONTRACT_BUG (VerdictStop).
        # This is a safety fuse, not a contract substitute. Prevents infinite gate loops.
        self._retryable_loop_counts: dict[tuple, int] = {}   # (phase, reason) → count
        # p207-P9: selective principal bypass — tracks principals that hit fake loop limit.
        # Only these specific principals are bypassed; other principals remain enforced.
        self._bypassed_principals: set[str] = set()       # principal names bypassed due to fake loop
        # 改动9c: per-LLM-step keyed idempotency for control signal injection.
        # key = f"{llm_step}:{kind}:{signal_id}" where kind ∈ {phase_prefix, cognition_violation}
        # _llm_step increments only when a NEW assistant text is observed in _step_observe,
        # i.e. once per LLM response — NOT once per tool call (n_calls).
        # 改动9b bug: used n_calls as step id, but n_calls increments per tool call →
        # each _monitored_step invocation got a unique key → dedup never fired.
        self._injected_signals: set[str] = set()
        self._llm_step: int = 0                    # increments on each new assistant response
        self._last_assistant_text: str = ""       # tracks last seen assistant text for change detection
        # Y-lite: observe_tool_signal — True if agent used any tool in the current LLM step.
        # Tools (Read/Grep/Search/Bash) in OBSERVE phase constitute implicit evidence basis.
        # Reset to False on each new LLM step (when _last_assistant_text changes).
        self._observe_tool_signal: bool = False
        # p23: causal binding — last ANALYZE root_cause, passed to EXECUTE gate.
        self.last_analyze_root_cause: str = ""
        # p221: per-phase accumulated assistant text for phase record extraction.
        # Agent outputs short thinking texts across many steps. Accumulate them so
        # extract_phase_record has enough material at VerdictAdvance time.
        # Reset on phase change (key = phase name, value = accumulated text).
        self._phase_accumulated_text: dict[str, str] = {}
        # p25 Materialization Gate Layer 1 (in-loop liveness):
        # When ADVANCE_TO_EXECUTE fires, agent MUST write a patch within K=2 steps.
        # _execute_entry_step: n_calls when EXECUTE phase was entered (-1 = not yet entered)
        # _execute_write_seen: True once a write/patch signal is observed in EXECUTE phase
        self._execute_entry_step: int = -1
        self._execute_write_seen: bool = False
        # Plan-A: extraction retry counts per phase — gates phase advance on extraction failure.
        # Key = phase name (str), value = consecutive extraction failure count.
        # Reset per attempt. After _MAX_EXTRACTION_RETRIES, force advance with no record.
        self.extraction_retry_counts: dict[str, int] = {}
        # E1: Quick Judge — in-loop targeted test signal
        self.quick_judge_history: list[dict] = []      # structured quick judge results
        self.quick_judge_count: int = 0                # invocation count this attempt
        self.last_quick_judge_step: int = -10          # _llm_step at last quick judge (-10 = never)
        self.last_quick_judge_time: float = 0.0        # monotonic time at last quick judge
        self.last_quick_judge_patch: str = ""          # patch hash at last quick judge
        self._quick_judge_selected_tests: list[str] | None = None  # locked test subset for this attempt
        self._pending_quick_judge_message: str = ""  # transient: message to inject after quick judge
        # Phase Submission Enforcement (p14 governance activation)
        # Tracks consecutive steps in the current phase without a submitted phase record.
        # Three-level escalation: soft reminder → hard warning → forced tool_choice.
        self._steps_without_submission: int = 0
        self._submission_escalation_level: int = 0  # 0=none, 1=reminder, 2=warning, 3=forced
        self._last_submission_phase: str = ""  # phase at last submission (to detect phase change)
        # Telemetry
        self._phase_record_force_total: int = 0
        self._phase_record_admit_total: int = 0
        self._phase_record_reject_total: int = 0
        # Analysis gate redirect tracking
        self._analysis_observe_redirects: int = 0

    @classmethod
    def from_checkpoint_dict(cls, d: dict, instance: dict | None = None) -> "StepMonitorState":
        """Reconstruct StepMonitorState from a checkpoint dict (inverse of to_checkpoint_dict).

        Args:
            d: Dict produced by to_checkpoint_dict().
            instance: SWE-bench instance dict. If None, uses minimal stub.

        Returns:
            StepMonitorState with restored control-plane fields.
            Fields not captured in checkpoint (threading lock, cp_state object)
            are initialized to defaults.
        """
        _instance = instance or {"instance_id": d.get("instance_id", "unknown")}
        state = cls(
            instance_id=d.get("instance_id", "unknown"),
            attempt=d.get("attempt", 1),
            instance=_instance,
        )
        state._llm_step = d.get("step_n", 0)
        state.container_id = d.get("container_id")
        state.pending_redirect_hint = d.get("pending_redirect_hint", "")
        state.analysis_gate_rejects = d.get("analysis_gate_rejects", 0)
        state.design_gate_rejects = d.get("design_gate_rejects", 0)
        state._execute_entry_step = d.get("execute_entry_step", -1)
        state._execute_write_seen = d.get("execute_write_seen", False)
        state.last_analyze_root_cause = d.get("last_analyze_root_cause", "")
        state._bypassed_principals = set(d.get("bypassed_principals", []))
        # Restore verify_history length marker (actual history not serialized)
        state._prev_verify_history_len = d.get("verify_history_len", 0)
        # Restore phase_records from checkpoint (serialized as list of dicts)
        state.phase_records = d.get("phase_records", [])
        # cp_state fields: patch_first_write and no_progress_steps live on cp_state,
        # not directly on StepMonitorState. We store them so the caller can
        # reconstruct cp_state if needed.
        state._checkpoint_no_progress_steps = d.get("no_progress_steps", 0)
        state._checkpoint_patch_first_write = d.get("patch_first_write", False)
        state._checkpoint_phase = d.get("phase")
        state.quick_judge_count = d.get("quick_judge_count", 0)
        return state

    def to_checkpoint_dict(self) -> dict:
        """Serialize control-plane state for checkpoint snapshots (p231).

        Returns a dict of all fields relevant to replay-from-step analysis.
        Never raises — returns partial dict on attribute errors.
        """
        result: dict = {}
        try:
            result["step_n"] = getattr(self, "_llm_step", 0)
            result["no_progress_steps"] = getattr(self.cp_state, "no_progress_steps", 0)
            result["patch_first_write"] = getattr(self.cp_state, "patch_first_write", False)
            result["phase"] = str(getattr(self.cp_state, "phase", None))
            result["container_id"] = self.container_id
            result["instance_id"] = self.instance_id
            result["attempt"] = self.attempt
            result["verify_history_len"] = len(self.verify_history)
            result["early_stop_verdict"] = (
                str(self.early_stop_verdict) if self.early_stop_verdict else None
            )
            result["pending_redirect_hint"] = self.pending_redirect_hint or ""
            result["analysis_gate_rejects"] = self.analysis_gate_rejects
            result["design_gate_rejects"] = self.design_gate_rejects
            result["execute_entry_step"] = self._execute_entry_step
            result["execute_write_seen"] = self._execute_write_seen
            result["last_analyze_root_cause"] = self.last_analyze_root_cause
            result["bypassed_principals"] = list(self._bypassed_principals)
            # phase_records: serialize each PhaseRecord
            _prs = []
            for pr in (self.phase_records or []):
                if isinstance(pr, dict):
                    _prs.append(pr)
                elif hasattr(pr, "__dict__"):
                    _prs.append(vars(pr))
                else:
                    _prs.append(str(pr))
            result["phase_records"] = _prs
            result["quick_judge_count"] = self.quick_judge_count
            result["quick_judge_history_len"] = len(self.quick_judge_history)
        except Exception:
            pass
        return result

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

        _cur_step_index = (
            cp_state_holder[0].step_index if cp_state_holder is not None
            else self.cp_state.step_index
        )
        step_partial, progress_evaluable_event, _pee_reason = extract_step_signals(
            tests_passed_count=tests_now,
            tests_passed_prev=tests_prev,
            env_error_detected=env_error_detected,
            patch_non_empty=patch_non_empty,
            patch_was_non_empty_prev=patch_prev,
            verify_history_len=verify_len_now,
            verify_history_len_prev=verify_len_prev,
            step_index=_cur_step_index,
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
                "stdout": result.get("stdout", "")[:10240],
                "stderr": result.get("stderr", "")[:10240],
                # BUG-10 fix: eval-aligned fields
                "f2p_passed": result.get("f2p_passed"),
                "f2p_failed": result.get("f2p_failed"),
                "p2p_passed": result.get("p2p_passed"),
                "p2p_failed": result.get("p2p_failed"),
                "eval_resolved": result.get("eval_resolved"),
            }
            self.verify_history.append(entry)
            delta_str = f"  delta={delta:+d}" if delta is not None else ""
            print(f"    [inner-verify] step={step}  "
                  f"passed={passed}  failed={result.get('tests_failed', -1)}"
                  f"{delta_str}  elapsed={result.get('elapsed_ms', 0):.0f}ms  "
                  f"kind={result.get('verification_kind', '?')}",
                  flush=True)

    def record_quick_judge(self, step: int, result: dict) -> None:
        """Record a quick judge invocation in history.

        Thread-safe. Called from _step_verify_if_needed after quick judge completes.
        """
        with self._lock:
            self.quick_judge_history.append(result)
            self.quick_judge_count += 1
            self.last_quick_judge_step = self._llm_step
            self.last_quick_judge_time = time.monotonic()
            if "patch_hash" in result:
                self.last_quick_judge_patch = str(result["patch_hash"])
            _target = result.get('target_test_id', '?')
            _tstatus = result.get('target_status', '?')
            _signal = result.get('signal_kind', '?')
            _scope = result.get('command_scope', '?')
            print(
                f"    [quick-judge] step={step} "
                f"target_status={_tstatus} signal={_signal} "
                f"scope={_scope} "
                f"passed={result.get('tests_passed', '?')}/{result.get('tests_targeted', '?')} "
                f"elapsed={result.get('elapsed_ms', 0):.0f}ms "
                f"target={_target}",
                flush=True,
            )

    def should_trigger_quick_judge(self, current_patch_hash: str, *, current_phase: str | None = None) -> bool:
        """Check all trigger conditions for quick judge.

        Returns True only when ALL conditions are met:
        C1: Must be in EXECUTE phase
        C2: Patch must have changed since last quick judge
        C3: At least 3 agent steps since last quick judge
        C4: At least 15s since last quick judge
        C5: Attempt not in terminal path
        C6: Quota not exhausted (max 3 per attempt)

        Args:
            current_patch_hash: MD5 hash of the current patch.
            current_phase: Override phase from cp_state_holder (state.cp_state
                may be stale when cp_state_holder is used by jingu_agent).
        """
        # C1: EXECUTE phase only
        phase = current_phase or getattr(self.cp_state, 'phase', None)
        if phase != "EXECUTE":
            return False
        # C2: Patch changed
        if current_patch_hash == self.last_quick_judge_patch:
            return False
        # C3: Step interval
        if (self._llm_step - self.last_quick_judge_step) < 3:
            return False
        # C4: Time interval
        if (time.monotonic() - self.last_quick_judge_time) < 15.0:
            return False
        # C5: Not terminal
        if self.early_stop_verdict is not None:
            return False
        # C6: Quota
        if self.quick_judge_count >= 3:
            return False
        return True

    def latest_tests_passed(self) -> int:
        """Return most recent known tests_passed count, or -1."""
        with self._lock:
            for entry in reversed(self.verify_history):
                if entry["tests_passed"] >= 0:
                    return entry["tests_passed"]
        return -1
