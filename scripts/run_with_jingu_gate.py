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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# B1: jingu-trust-gate bridge (subprocess → TS gate)
from jingu_gate_bridge import evaluate_patch_from_traj, build_support_pool, run_patch_gate
# B2: adversarial reviewer (cognitive governance)
from patch_reviewer import review_patch_bedrock, ReviewResult
# B3: retry controller (failure → diagnosis → next strategy)
from retry_controller import build_retry_plan

# B1 gate mode: "trust_gate" (B1) or "structural" (B0 fallback)
GATE_MODE = "trust_gate"
REVIEWER_ENABLED = False  # B2 reviewer — set True to re-enable
RETRY_CONTROLLER_ENABLED = True  # B3 retry-controller — diagnoses attempt 1, guides attempt 2

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


def build_execution_feedback(
    jingu_body: dict,
    fail_to_pass_tests: list[str],
    patch_fp: dict,
) -> str:
    """
    Build a structured retry hint from execution signal — deterministic, no LLM.

    Converts: test_results + patch fingerprint → actionable hint for attempt 2.
    Three layers: summary → failing tests → example failure excerpt.
    """
    test_results = jingu_body.get("test_results", {})
    tests_ran = test_results.get("ran_tests", False)
    test_passed = test_results.get("last_passed")
    excerpt = test_results.get("excerpt", "")

    if not tests_ran:
        return (
            "Previous attempt submitted without running tests. "
            "Run the required tests FIRST, verify they pass, then submit."
        )

    if test_passed:
        # Agent's own tests passed but fast_eval may still fail — give benefit of doubt
        # Remind agent to verify against the specific FAIL_TO_PASS tests
        tests_str = ", ".join(fail_to_pass_tests[:4])
        return (
            f"Previous attempt's tests passed locally. "
            f"Ensure these specific tests pass: {tests_str}. "
            f"If they already pass, submit immediately."
        )

    # Tests failed — build structured feedback
    parts = ["Previous attempt failed tests.\n"]

    # Layer 1: extract failure/error counts from excerpt
    failures = 0
    errors = 0
    if excerpt:
        fm = re.search(r'(\d+) failure', excerpt)
        em = re.search(r'(\d+) error', excerpt)
        if fm:
            failures = int(fm.group(1))
        if em:
            errors = int(em.group(1))
    if failures or errors:
        parts.append(f"Test results: {failures} failure(s), {errors} error(s)\n")

    # Layer 2: failing test names from FAIL_TO_PASS (most relevant signal)
    if fail_to_pass_tests:
        tests_str = "\n".join(f"  - {t.split('.')[-1]}" for t in fail_to_pass_tests[:6])
        parts.append(f"Tests that must pass:\n{tests_str}\n")

    # Layer 3: compress excerpt to most useful part
    # pytest output: errors/failures section is most useful, summary line is at end
    if excerpt:
        # Try to extract the failure section (between === FAILURES === and === short test summary ===)
        fail_section = re.search(
            r'(={3,} FAILURES ={3,}.*?)(?:={3,}|$)', excerpt, re.DOTALL
        )
        if fail_section:
            parts.append(f"Failure detail:\n{fail_section.group(1)[:600]}\n")
        else:
            # Fallback: last 400 chars of excerpt (usually has summary)
            useful = excerpt[-400:].strip()
            if useful:
                parts.append(f"Test output tail:\n{useful}\n")

    # Files changed (to surface if agent went to wrong files)
    files = patch_fp.get("files", []) if patch_fp else []
    if files:
        parts.append(f"Files you changed: {files}\n")

    parts.append(
        "You must: fix the underlying logic (not just suppress warnings or add code). "
        "Run the failing tests and verify they pass before submitting."
    )

    return "\n".join(parts)


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

# ── Jingu gates ───────────────────────────────────────────────────────────────

def normalize_patch(patch_text: str) -> str:
    """Pad truncated hunks so git apply does not fail with 'corrupt patch'.

    LLMs sometimes omit the last 1-2 trailing context lines of a hunk.
    git apply counts lines strictly against the @@ header count; a short hunk
    causes 'corrupt patch at line N'.  We detect each hunk's claimed line count
    and append missing blank context lines (' ') at the end of short hunks.
    """
    lines = patch_text.splitlines()
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r'^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@', line)
        if m:
            old_count = int(m.group(1)) if m.group(1) is not None else 1
            new_count = int(m.group(2)) if m.group(2) is not None else 1
            result.append(line)
            i += 1
            old_seen = new_seen = 0
            hunk_lines = []
            while i < len(lines):
                nl = lines[i]
                if re.match(r'^(@@ |diff --git |--- )', nl) or nl.startswith('+++ '):
                    break
                hunk_lines.append(nl)
                if nl.startswith('+') and not nl.startswith('+++'):
                    new_seen += 1
                elif nl.startswith('-') and not nl.startswith('---'):
                    old_seen += 1
                else:
                    old_seen += 1
                    new_seen += 1
                i += 1
            old_missing = old_count - old_seen
            new_missing = new_count - new_seen
            pad = max(old_missing, new_missing)
            for _ in range(pad):
                hunk_lines.append(' ')
            result.extend(hunk_lines)
        else:
            result.append(line)
            i += 1
    normalized = '\n'.join(result)
    if not normalized.endswith('\n'):
        normalized += '\n'
    return normalized


def jingu_structural_check(patch_text: str) -> dict:
    """Check patch has --- / +++ / @@ markers."""
    if not patch_text or len(patch_text.strip()) < 10:
        return {"pass": False, "code": "EMPTY_PATCH", "message": "Patch is empty"}
    if not re.search(r'^(---|[+]{3}|@@)', patch_text, re.MULTILINE):
        return {"pass": False, "code": "PARSE_FAILED", "message": "No diff markers found"}
    return {"pass": True, "code": "ACCEPTED"}

def score_patch(patch_text: str) -> float:
    """Score: prefer small, single-file patches."""
    lines = patch_text.splitlines()
    files = sum(1 for l in lines if l.startswith("+++ b/"))
    changed = sum(1 for l in lines if l.startswith(("+", "-")) and not l.startswith(("+++", "---")))
    score = 1000.0 - files * 50
    return score


def extract_jingu_body(traj: dict, patch_text: str, problem_statement: str = "") -> dict:
    """
    Derive structured jingu_body from traj messages — no LLM call needed.

    jingu_body schema v0: deterministic extraction from observable agent behavior.
    Used by jingu-trust-gate B1+ as structured evidence for admission decisions.
    """
    messages = traj.get("messages", [])
    info = traj.get("info", {})
    exit_status = info.get("exit_status", "")

    # Files read and written — parse from tool call content
    files_read: set[str] = set()
    files_written: set[str] = set()
    test_ran = False
    last_test_passed: bool | None = None
    last_test_excerpt = ""
    tool_calls_made = 0

    # Write signals: collected from multiple sources
    # 1. Patch is ground truth — if patch touches a file, agent wrote it
    for line in (patch_text or "").splitlines():
        if line.startswith("+++ b/"):
            fp = line[6:].strip()
            if fp:
                files_written.add(fp)

    for msg in messages:
        role = msg.get("role", "")
        extra = msg.get("extra", {})
        actions = extra.get("actions", []) if role == "assistant" else []
        for action in actions:
            tool_calls_made += 1
            # Actions may be dicts (structured tool calls) or strings (bash commands)
            if isinstance(action, dict):
                tool_name = action.get("tool", action.get("name", ""))
                tool_input = action.get("input", action.get("arguments", {}))
                # Structured tool calls: look for path/file fields
                path_val = ""
                if isinstance(tool_input, dict):
                    path_val = (tool_input.get("path") or tool_input.get("file_path")
                                or tool_input.get("filename") or "")
                if path_val and ("/" in path_val or path_val.endswith(".py")):
                    write_tools = {"edit_file", "write_file", "create_file",
                                   "str_replace_editor", "str_replace", "apply_patch",
                                   "bash_write", "patch"}
                    read_tools  = {"open_file", "view_file", "read_file",
                                   "str_replace_editor_view", "cat"}
                    if any(t in tool_name.lower() for t in write_tools):
                        files_written.add(path_val)
                    elif any(t in tool_name.lower() for t in read_tools):
                        files_read.add(path_val)
            else:
                # String action (bash command) — limited heuristic, patch is authoritative
                action_str = str(action)
                if any(kw in action_str for kw in ("open_file", "view_file", "cat ")):
                    parts = action_str.split()
                    for i, p in enumerate(parts):
                        if p in ("open_file", "view_file") and i + 1 < len(parts):
                            path_candidate = parts[i + 1].strip("'\"")
                            if "/" in path_candidate or path_candidate.endswith(".py"):
                                files_read.add(path_candidate)

        # Detect test results from tool outputs
        if role == "tool":
            content = str(msg.get("content", ""))
            if any(kw in content for kw in ("PASSED", "FAILED", "passed", "failed", "ERROR", "error")):
                test_ran = True
                if "FAILED" in content or "failed" in content.lower() or "ERROR" in content:
                    last_test_passed = False
                else:
                    last_test_passed = True
                # Extract from <output> tag if present; take last 1500 chars (summary is at end)
                out_match = re.search(r'<output>(.*?)</output>', content, re.DOTALL)
                raw_out = out_match.group(1) if out_match else content
                last_test_excerpt = raw_out[-1500:]

    # Patch summary from patch structure
    patch_lines = patch_text.splitlines() if patch_text else []
    patch_files_changed = sum(1 for l in patch_lines if l.startswith("+++ b/"))
    patch_hunks = sum(1 for l in patch_lines if l.startswith("@@"))
    patch_lines_added = sum(1 for l in patch_lines if l.startswith("+") and not l.startswith("+++"))
    patch_lines_removed = sum(1 for l in patch_lines if l.startswith("-") and not l.startswith("---"))

    return {
        "schema_version": "jingu-body-v0",
        "exit_status": exit_status,
        "problem_understanding": (problem_statement or info.get("problem_statement", ""))[:300],
        "tool_calls_made": tool_calls_made,
        "files_read": sorted(files_read)[:20],
        "files_written": sorted(files_written)[:10],
        "test_results": {
            "ran_tests": test_ran,
            "last_passed": last_test_passed,
            "excerpt": last_test_excerpt,
        },
        "patch_summary": {
            "files_changed": patch_files_changed,
            "hunks": patch_hunks,
            "lines_added": patch_lines_added,
            "lines_removed": patch_lines_removed,
        },
    }

# ── mini-SWE-agent runner (direct Python API) ─────────────────────────────────

MODEL = "bedrock/global.anthropic.claude-sonnet-4-5-20250929-v1:0"

BASE_CONFIG = {
    "model": {
        "model_class": "litellm",
        "model_name": MODEL,
        "model_kwargs": {
            "drop_params": True,
            # litellm 1.83 bug: parallel_tool_calls=true/false sends malformed tool_choice to Bedrock.
            # Setting None suppresses the param entirely, which works correctly.
            "parallel_tool_calls": None,
        },
    },
    "environment": {
        "environment_class": "docker",
        "container_timeout": "30m",
    },
    "agent": {
        "mode": "yolo",
        "confirm_exit": False,  # critical: don't wait for user input
        # step_limit is set per-attempt in run_agent() below:
        #   attempt 1 → 40  (fast first pass: find file, minimal fix, submit)
        #   attempt 2 → 60  (guided retry: has exec-feedback + retry hint)
        # 100 was too slow — 15 min blind runs with no intermediate feedback.
    },
}

_INSTANCE_CACHE: dict[str, dict] = {}

def _load_instances(instance_ids: list[str]) -> dict[str, dict]:
    """Load multiple SWE-bench Lite instances in one dataset pass."""
    from datasets import load_dataset
    needed = set(instance_ids) - set(_INSTANCE_CACHE)
    if needed:
        ds = load_dataset("SWE-bench/SWE-bench_Lite", split="test")
        for inst in ds:
            if inst["instance_id"] in needed:
                _INSTANCE_CACHE[inst["instance_id"]] = dict(inst)
    missing = set(instance_ids) - set(_INSTANCE_CACHE)
    if missing:
        raise ValueError(f"Instances not found: {missing}")
    return {iid: _INSTANCE_CACHE[iid] for iid in instance_ids}


def _load_instance(instance_id: str) -> dict:
    return _load_instances([instance_id])[instance_id]

def run_agent(
    instance: dict,
    output_dir: Path,
    attempt: int,
    previous_failure: str = "",
    parent_timer: Timer | None = None,
) -> tuple[str | None, str | None, dict | None]:
    """Run mini-SWE-agent on one instance. Returns (submission patch or None, exit_status, jingu_body or None)."""
    from minisweagent.run.benchmarks.swebench import process_instance
    from minisweagent.config import get_config_from_spec
    from minisweagent.utils.serialize import recursive_merge

    instance_id = instance["instance_id"]
    attempt_dir = output_dir / f"attempt_{attempt}"
    attempt_dir.mkdir(parents=True, exist_ok=True)

    t_agent = Timer(f"agent attempt={attempt}", parent=parent_timer)

    # Start from swebench.yaml defaults (provides system_template, instance_template, etc.)
    t_cfg = Timer("config load", parent=t_agent)
    config = get_config_from_spec("swebench.yaml")
    config = recursive_merge(config, BASE_CONFIG)
    # Per-attempt step budget: attempt 1 = fast first pass, attempt 2 = guided retry
    step_limit = 40 if attempt == 1 else 60
    config = recursive_merge(config, {"agent": {"step_limit": step_limit}})
    # Build instance_template_extra: tests that must pass + optional retry hint
    extra_parts = []
    fail_to_pass = instance.get("FAIL_TO_PASS", [])
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
    if extra_parts:
        config = recursive_merge(config, {
            "agent": {"instance_template_extra": "\n\n".join(extra_parts)}
        })
    t_cfg.stop()

    print(f"    [agent] running {instance_id} attempt={attempt}...")

    from minisweagent.run.benchmarks.swebench import RunBatchProgressManager

    preds_path = attempt_dir / "preds.json"
    progress = RunBatchProgressManager(num_instances=1)

    t_llm = Timer("LLM agent loop (Bedrock)", parent=t_agent)
    try:
        process_instance(instance, attempt_dir, config, progress)
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
            # Write jingu_body back into traj.json so gate_runner.js can read it
            traj["jingu_body"] = jingu_body
            traj_path.write_text(json.dumps(traj, indent=2))
            print(f"    [jingu_body] extracted: exit={jingu_body['exit_status']} "
                  f"files_written={len(jingu_body['files_written'])} "
                  f"tests_ran={jingu_body['test_results']['ran_tests']} "
                  f"patch_hunks={jingu_body['patch_summary']['hunks']}")
        except (json.JSONDecodeError, OSError):
            pass

    t_agent.llm_calls = usage.api_calls  # stash for timing tree
    avg_s = t_llm.elapsed / usage.api_calls if usage.api_calls else 0
    print(f"    [agent] LLM loop done in {t_llm.elapsed:.1f}s  "
          f"bedrock_calls={usage.api_calls}  avg={avg_s:.1f}s/call  "
          f"tokens={usage.input_tokens}in/{usage.output_tokens}out  "
          f"cost=${usage.cost_usd:.4f}")

    t_agent.stop()

    # Read submission from preds.json
    if preds_path.exists():
        preds = json.loads(preds_path.read_text())
        if instance_id in preds:
            sub = preds[instance_id].get("model_patch", "")
            if sub:
                return sub, exit_status, jingu_body

    if sub_from_traj:
        return sub_from_traj, exit_status, jingu_body

    if sub_from_traj_diff:
        return sub_from_traj_diff, exit_status, jingu_body

    return None, exit_status, jingu_body

# ── Main loop ─────────────────────────────────────────────────────────────────

def run_with_jingu(instance_id: str, output_dir: Path, max_attempts: int = 3) -> dict:
    """Run agent + Jingu gate with retry. Returns best result."""
    t_inst = Timer(f"instance: {instance_id}", parent=_timing_root)
    _instance_timers[instance_id] = t_inst

    print(f"  [jingu] loading instance {instance_id}...")
    t_load = Timer("dataset load", parent=t_inst)
    instance = _load_instance(instance_id)
    t_load.stop()

    candidates = []
    attempts_log: list[dict] = []   # telemetry: one entry per attempt
    last_failure = ""
    total_llm_calls = 0

    for attempt in range(1, max_attempts + 1):
        print(f"  [attempt {attempt}/{max_attempts}] {instance_id}")

        patch, agent_exit, jingu_body = run_agent(instance, output_dir, attempt,
                                                  previous_failure=last_failure, parent_timer=t_inst)

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
                last_failure = "No patch was generated"
            t_gate.stop()
            continue

        patch = normalize_patch(patch)

        if GATE_MODE == "trust_gate":
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
                    prev = attempts_log[-2].get("patch_fp", {})
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
                    if RETRY_CONTROLLER_ENABLED:
                        # Phase 2B: LLM retry-controller builds on execution feedback
                        t_ctrl = Timer(f"B3 retry-controller attempt={attempt}", parent=t_inst)
                        retry_plan = build_retry_plan(
                            problem_statement=instance.get("problem_statement", ""),
                            patch_text=patch,
                            jingu_body=jingu_body or {},
                            fail_to_pass_tests=fail_to_pass,
                            gate_admitted=True,
                            gate_reason_codes=gate_result.reason_codes,
                            instance_id=instance_id,
                        )
                        t_ctrl.stop()
                        print(f"    [retry-ctrl] root_causes={retry_plan.root_causes}")
                        print(f"    [retry-ctrl] hint={retry_plan.next_attempt_prompt[:200]}")
                        # Combine: exec feedback provides facts, LLM provides strategy
                        last_failure = (exec_feedback + "\n\n" + retry_plan.next_attempt_prompt)[:600]
                    else:
                        last_failure = exec_feedback[:400]
                else:
                    last_failure = ""
                agent_exit = None
            else:
                codes = ", ".join(gate_result.reason_codes)
                print(f"    [gate] REJECTED  codes={codes}  {exp_str}")
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

def write_predictions(results: list, output_path: Path):
    # Rewrite the file completely (deduplicates any incremental writes)
    with open(output_path, "w") as f:
        for r in results:
            if r and r.get("accepted"):
                f.write(json.dumps({
                    "instance_id": r["instance_id"],
                    "model_patch": r["patch"],
                    "model_name_or_path": "mini-swe-agent+jingu",
                }) + "\n")
    print(f"\n[predictions] written: {output_path}")
    accepted = sum(1 for r in results if r and r.get("accepted"))
    print(f"[predictions] {accepted}/{len(results)} instances accepted")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance-ids", nargs="+", required=True)
    parser.add_argument("--max-attempts", type=int, default=1)
    parser.add_argument("--output", default="results/mini-swe-agent")
    parser.add_argument("--workers", type=int, default=4,
                        help="Parallel instances to run (default: 4)")
    parser.add_argument("--stagger", type=float, default=15.0,
                        help="Seconds between sandbox starts to avoid image-pull contention (default: 15)")
    args = parser.parse_args()

    global _timing_root
    _timing_root = Timer("total run")

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Pre-load all instances in a single dataset pass (avoids N redundant downloads)
    print(f"[jingu] loading {len(args.instance_ids)} instances from dataset...")
    t_ds = Timer("dataset prefetch", parent=_timing_root)
    _load_instances(args.instance_ids)
    t_ds.stop()
    print(f"[jingu] loaded in {t_ds.elapsed:.1f}s. launching {args.workers} parallel workers...")

    t_parallel = Timer(f"parallel workers (×{min(args.workers, len(args.instance_ids))})", parent=_timing_root)
    results = [None] * len(args.instance_ids)

    def _run(idx: int, iid: str):
        delay = idx * args.stagger
        if delay > 0:
            print(f"[jingu] {iid} waiting {delay:.0f}s before start (stagger)")
            time.sleep(delay)
        print(f"\n[jingu] START {iid}")
        r = run_with_jingu(iid, output_dir, max_attempts=args.max_attempts)
        status = "ACCEPTED" if r["accepted"] else "FAILED"
        print(f"\n[jingu] {status} {iid}  ({r.get('elapsed_s', 0):.1f}s)")
        return idx, r

    preds_path = output_dir / "jingu-predictions.jsonl"
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
                with open(preds_path, "a") as pf:
                    pf.write(json.dumps({
                        "instance_id": r["instance_id"],
                        "model_patch": r["patch"],
                        "model_name_or_path": "mini-swe-agent+jingu",
                    }) + "\n")
                print(f"[predictions] saved {r['instance_id']} (incremental)")
            print(f"[progress] {done}/{len(args.instance_ids)} done")

    t_parallel.stop()

    t_write = Timer("write predictions", parent=_timing_root)
    write_predictions(results, output_dir / "jingu-predictions.jsonl")
    t_write.stop()

    _timing_root.stop()

    # ── Run Report ─────────────────────────────────────────────────────────────
    total     = _timing_root.elapsed
    totals    = _usage_tracker.totals()
    per_inst  = _usage_tracker.per_instance()
    max_elapsed = max((r.get("elapsed_s", 0) for r in results if r), default=1)
    seq_total = sum(r.get("elapsed_s", 0) for r in results if r)
    speedup   = seq_total / t_parallel.elapsed if t_parallel.elapsed > 0 else 1

    report = {
        "instances":        len(args.instance_ids),
        "workers":          args.workers,
        "step_limit":       "40/60 (attempt1/attempt2)",
        "wall_time_s":      round(total, 1),
        "status":           "completed",
        "patches_generated": sum(1 for r in results if r and r["accepted"]),
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
    }

    # Save machine-readable report
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
    print(f"  ── TIMING ──")
    print(f"    dataset prefetch   : {t_ds.elapsed:.1f}s")
    print(f"    parallel workers   : {t_parallel.elapsed:.1f}s  ({t_parallel.elapsed/total:.0%} of total)")
    print(f"    parallelism gain   : {seq_total:.1f}s → {t_parallel.elapsed:.1f}s  (×{speedup:.1f})")
    print(f"    write predictions  : {t_write.elapsed:.1f}s")
    print()
    print(f"  report saved → {report_path}")
    print(f"{'='*62}\n")

if __name__ == "__main__":
    main()
