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
import time
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
        # Plan-C: skip structured_extract traj entries
        if msg.get("extra", {}).get("type", "").startswith("structured_extract_"):
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

def _get_bundle_activation(mode: str) -> dict | None:
    """PR1: Read bundle activation proof from jingu_agent module."""
    if mode != "jingu":
        return None
    try:
        import jingu_agent
        return getattr(jingu_agent, '_bundle_activation_proof', {"bundle_loaded": "unknown"})
    except Exception:
        return {"bundle_loaded": "unknown"}


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

# ── Main loop ─────────────────────────────────────────────────────────────────

def run_with_jingu(instance_id: str, output_dir: Path, max_attempts: int = 3,
                   mode: str = "jingu") -> dict:
    """Run agent + Jingu gate with retry. Returns best result.

    Thin wrapper — delegates to JinguAgent.run() (p225-10).
    """
    from jingu_agent import JinguAgent
    instance = _load_instance(instance_id)
    agent = JinguAgent(instance, Path(output_dir), governance=None, mode=mode, max_attempts=max_attempts)
    return agent.run().to_dict()

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
    parser.add_argument("--no-eval", action="store_true", default=False,
                        help="Skip official SWE-bench evaluation after inference (eval runs by default)")
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

    # ── Failure layer breakdown (semantic rootcause) ──────────────────────────
    failure_layer_breakdown: dict[str, int] = {}
    failure_layer_instances: dict[str, list[str]] = {}
    for r in results:
        if not r or r.get("accepted"):
            continue
        fl = r.get("failure_layer") or "unknown"
        failure_layer_breakdown[fl] = failure_layer_breakdown.get(fl, 0) + 1
        failure_layer_instances.setdefault(fl, []).append(r.get("instance_id", "?"))

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
        "failure_layer_breakdown": failure_layer_breakdown,
        "failure_layer_instances": failure_layer_instances,
        "bundle_activation": _get_bundle_activation(args.mode),
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
        "eval_results": None,  # filled in below (eval runs by default, skip with --no-eval)
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
    if failure_layer_breakdown:
        print(f"  ── FAILURE LAYER (semantic rootcause) ──")
        for fl, cnt in sorted(failure_layer_breakdown.items(), key=lambda x: -x[1]):
            insts = failure_layer_instances.get(fl, [])
            insts_str = ", ".join(i.replace("django__django-", "dj-") for i in insts[:5])
            print(f"    {fl:45s}: {cnt}  [{insts_str}]")
    print()
    print(f"  ── TIMING ──")
    print(f"    dataset prefetch   : {t_ds.elapsed:.1f}s")
    print(f"    parallel workers   : {t_parallel.elapsed:.1f}s  ({t_parallel.elapsed/total:.0%} of total)")
    print(f"    parallelism gain   : {seq_total:.1f}s → {t_parallel.elapsed:.1f}s  (×{speedup:.1f})")
    print(f"    write predictions  : {t_write.elapsed:.1f}s")
    print()
    print(f"  report saved → {report_path}")

    # ── Official evaluation (optional) ─────────────────────────────────────────
    if not args.no_eval:
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
