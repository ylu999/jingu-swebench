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
    extract_from_structured,
    build_phase_record_from_structured,
)
from patch_signals import extract_patch_signals
from cognition_check import check_cognition, format_cognition_feedback
from preflight import run_preflight
# B1-CP: reasoning control plane (Python port of jingu-trust-gate control plane v0.3)
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
# ║    JinguAgent, JinguProgressTrackingAgent (jingu_agent.py)                  ║
# ║    debounce, patch_first_write detection, pee gating,                       ║
# ║    inner-verify scheduling, stagnation counter, VerdictStop enforcement     ║
# ║                                                                              ║
# ║  Key rule: "should_trigger_verify" and "should_stop" are RUNTIME decisions. ║
# ║  They depend on history. They do NOT belong in Jingu governance.            ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

# p225-05: step section functions extracted to step_sections.py
from step_sections import (  # noqa: F401, E402 — re-export for backward compat
    _step_observe,
    _step_verify_if_needed,
    _step_cp_update_and_verdict,
    _step_check_structure,
    _step_inject_phase,
    _check_materialization_gate,
    PHASE_REQUIRED_FIELDS,
)




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
    """Compatibility wrapper — delegates to JinguAgent.run_attempt() (p225-09).

    Returns the original 4-tuple: (patch, exit_status, jingu_body, monitor).
    """
    from jingu_agent import JinguAgent
    agent = JinguAgent(instance, Path(output_dir), governance=None, mode=mode)
    agent._cp_state_holder = cp_state_holder if cp_state_holder is not None else []
    outcome = agent.run_attempt(attempt, previous_failure, parent_timer=parent_timer)
    r = outcome.result
    return (r.patch, r.exit_status, r.jingu_body, r.monitor)


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

        # p186: check early_stop_verdict set by JinguAgent step hooks during the attempt.
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
