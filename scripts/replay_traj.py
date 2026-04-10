#!/usr/bin/env python3
"""
replay_traj.py — Deep RCA analysis on a single traj.json file.

Replays the agent's trajectory step-by-step, showing phases, tool calls,
principal gate results, controlled verify results, and identifying where
things went wrong.

Usage:
  python scripts/replay_traj.py <local-path-to-traj.json>
  python scripts/replay_traj.py --s3 <batch-name> <instance-id>
  python scripts/replay_traj.py --batch-dir <local-dir>
  python scripts/replay_traj.py --s3-batch <batch-name>

Flags:
  --verbose     Full message content (no truncation)
  --summary     Key metrics only (no step-by-step replay)
  --attempt N   Only show attempt N (default: all)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Optional


# ── Colors ──────────────────────────────────────────────────────────────────

class C:
    """ANSI color codes — disabled when not a TTY."""
    ENABLED = sys.stdout.isatty()

    @staticmethod
    def _w(code: str, text: str) -> str:
        if not C.ENABLED:
            return text
        return f"{code}{text}\033[0m"

    @classmethod
    def bold(cls, t: str) -> str:    return cls._w("\033[1m", t)
    @classmethod
    def red(cls, t: str) -> str:     return cls._w("\033[31m", t)
    @classmethod
    def green(cls, t: str) -> str:   return cls._w("\033[32m", t)
    @classmethod
    def yellow(cls, t: str) -> str:  return cls._w("\033[33m", t)
    @classmethod
    def blue(cls, t: str) -> str:    return cls._w("\033[34m", t)
    @classmethod
    def magenta(cls, t: str) -> str: return cls._w("\033[35m", t)
    @classmethod
    def cyan(cls, t: str) -> str:    return cls._w("\033[36m", t)
    @classmethod
    def dim(cls, t: str) -> str:     return cls._w("\033[2m", t)


# ── Traj Loading ────────────────────────────────────────────────────────────

S3_BUCKET = "jingu-swebench-results"
S3_REGION = "us-west-2"


def load_traj_local(path: str) -> dict:
    """Load traj.json from local filesystem."""
    with open(path) as f:
        return json.load(f)


def load_traj_s3(batch_name: str, instance_id: str, attempt: int = 0) -> dict:
    """Load traj.json from S3.

    If attempt=0, tries all attempts and returns the last one found.
    """
    import boto3
    s3 = boto3.client("s3", region_name=S3_REGION)

    if attempt > 0:
        key = f"{batch_name}/attempt_{attempt}/{instance_id}/{instance_id}.traj.json"
        resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
        return json.loads(resp["Body"].read())

    # Try attempts 1-5, collect all
    trajs = []
    for att in range(1, 6):
        key = f"{batch_name}/attempt_{att}/{instance_id}/{instance_id}.traj.json"
        try:
            resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
            trajs.append((att, json.loads(resp["Body"].read())))
        except s3.exceptions.NoSuchKey:
            break
        except Exception:
            break
    if not trajs:
        raise FileNotFoundError(
            f"No traj found in s3://{S3_BUCKET}/{batch_name}/ for {instance_id}"
        )
    return trajs  # Return list of (attempt, traj) tuples


def list_s3_batch_instances(batch_name: str) -> list[str]:
    """List all instance IDs in an S3 batch."""
    import boto3
    s3 = boto3.client("s3", region_name=S3_REGION)
    paginator = s3.get_paginator("list_objects_v2")
    instances = set()
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=f"{batch_name}/attempt_1/", Delimiter="/"):
        for prefix in page.get("CommonPrefixes", []):
            # prefix like "batch-name/attempt_1/django__django-11099/"
            parts = prefix["Prefix"].rstrip("/").split("/")
            if len(parts) >= 3:
                instances.add(parts[2])
    return sorted(instances)


def find_local_trajs(batch_dir: str) -> list[tuple[str, str]]:
    """Find all traj.json files in a local batch directory.

    Returns list of (instance_id, path) tuples.
    """
    results = []
    batch_path = Path(batch_dir)
    for traj_file in sorted(batch_path.rglob("*.traj.json")):
        instance_id = traj_file.stem.replace(".traj", "")
        results.append((instance_id, str(traj_file)))
    return results


# ── Message Parsing ─────────────────────────────────────────────────────────

def parse_action(action: dict | str) -> str:
    """Parse a single action into a human-readable string."""
    if isinstance(action, str):
        return action[:200]

    # Bash/command action
    if "command" in action:
        cmd = action["command"]
        return f"bash({cmd[:150]})"

    # Structured tool call
    tool = action.get("tool", action.get("name", "unknown"))
    inp = action.get("input", action.get("arguments", {}))
    if isinstance(inp, dict):
        # Extract key fields
        path = inp.get("path", inp.get("file_path", inp.get("filename", "")))
        if path:
            return f"{tool}({path})"
        # Fallback: show first key=value
        preview = ", ".join(f"{k}={str(v)[:50]}" for k, v in list(inp.items())[:2])
        return f"{tool}({preview})"
    return f"{tool}({str(inp)[:100]})"


def extract_tool_output(content: str, max_len: int = 200) -> str:
    """Extract meaningful content from tool output message."""
    # Try <output> tag first
    m = re.search(r"<output>(.*?)</output>", content, re.DOTALL)
    text = m.group(1).strip() if m else content

    # Extract returncode
    rc_match = re.search(r"<returncode>(\d+)</returncode>", content)
    rc = rc_match.group(1) if rc_match else None

    if len(text) > max_len:
        text = text[:max_len] + "..."

    prefix = f"[rc={rc}] " if rc and rc != "0" else ""
    return prefix + text


def detect_phase_from_content(content: str) -> Optional[str]:
    """Detect declared phase from assistant message content."""
    # PHASE: OBSERVE / ANALYZE / EXECUTE / JUDGE
    m = re.search(r"PHASE:\s*(UNDERSTAND|OBSERVE|ANALYZE|DECIDE|EXECUTE|JUDGE)", content, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return None


def detect_principals_from_content(content: str) -> list[str]:
    """Detect declared principals from assistant message content."""
    m = re.search(r"PRINCIPALS?:\s*(.+?)(?:\n|$)", content, re.IGNORECASE)
    if m:
        raw = m.group(1).strip()
        # Split on comma or space
        return [p.strip().lower() for p in re.split(r"[,\s]+", raw) if p.strip()]
    return []


def detect_root_cause(content: str) -> Optional[str]:
    """Extract ROOT_CAUSE declaration from content."""
    m = re.search(r"ROOT_CAUSE:\s*(.+?)(?:\n(?:[A-Z_]+:|$)|\Z)", content, re.DOTALL)
    if m:
        return m.group(1).strip()[:300]
    return None


def detect_plan(content: str) -> Optional[str]:
    """Extract PLAN declaration from content."""
    m = re.search(r"PLAN:\s*(.+?)(?:\n(?:[A-Z_]+:|$)|\Z)", content, re.DOTALL)
    if m:
        return m.group(1).strip()[:300]
    return None


def detect_fix_type(content: str) -> Optional[str]:
    """Extract FIX_TYPE declaration from content."""
    m = re.search(r"FIX_TYPE:\s*(.+?)(?:\n|$)", content, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


# ── Step Reconstruction ─────────────────────────────────────────────────────

class Step:
    """Reconstructed agent step from messages."""

    def __init__(self, index: int):
        self.index = index
        self.phase: Optional[str] = None
        self.principals: list[str] = []
        self.root_cause: Optional[str] = None
        self.plan: Optional[str] = None
        self.fix_type: Optional[str] = None
        self.assistant_content: str = ""
        self.actions: list[str] = []  # parsed action descriptions
        self.tool_outputs: list[str] = []  # parsed tool output summaries
        self.phase_injection: Optional[str] = None  # user message with phase hint
        self.files_read: list[str] = []
        self.files_written: list[str] = []
        self.is_submit: bool = False

    def summary(self, verbose: bool = False) -> str:
        """One-line summary of this step."""
        parts = []
        if self.phase:
            parts.append(C.magenta(f"PHASE: {self.phase}"))
        if self.root_cause:
            parts.append(C.yellow(f"ROOT_CAUSE: {self.root_cause[:100]}"))
        if self.plan:
            parts.append(C.cyan(f"PLAN: {self.plan[:100]}"))
        if self.fix_type:
            parts.append(C.green(f"FIX_TYPE: {self.fix_type}"))
        if self.principals:
            parts.append(f"PRINCIPALS: [{', '.join(self.principals)}]")
        return "\n  ".join(parts) if parts else ""


def reconstruct_steps(messages: list[dict], verbose: bool = False) -> list[Step]:
    """Reconstruct agent steps from raw messages.

    A "step" is one assistant message + its corresponding tool outputs.
    """
    steps: list[Step] = []
    current_step: Optional[Step] = None
    step_idx = 0

    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        content = str(msg.get("content", ""))

        if role == "system":
            continue

        if role == "user":
            # Check for phase injection
            phase_hint = None
            if "[Phase:" in content:
                m = re.search(r"\[Phase:\s*(\w+)\]", content)
                if m:
                    phase_hint = m.group(1).upper()
            # If this is a phase injection and we have a pending step, annotate it
            if phase_hint and current_step is None:
                # Will be picked up by next step
                pass
            # Store for next step
            if phase_hint:
                # Create a placeholder if no current step
                if current_step is not None:
                    current_step.phase_injection = phase_hint
            continue

        if role == "assistant":
            # New step
            step_idx += 1
            current_step = Step(step_idx)
            steps.append(current_step)

            current_step.assistant_content = content

            # Extract declarations
            current_step.phase = detect_phase_from_content(content)
            current_step.principals = detect_principals_from_content(content)
            current_step.root_cause = detect_root_cause(content)
            current_step.plan = detect_plan(content)
            current_step.fix_type = detect_fix_type(content)

            # Parse actions
            extra = msg.get("extra", {})
            actions = extra.get("actions", [])
            for action in actions:
                parsed = parse_action(action)
                current_step.actions.append(parsed)

                # Detect file operations from actions
                if isinstance(action, dict):
                    cmd = action.get("command", "")
                    tool = action.get("tool", action.get("name", ""))
                    inp = action.get("input", action.get("arguments", {}))

                    # Detect file reads from bash
                    if cmd:
                        if any(kw in cmd for kw in ("cat ", "head ", "less ", "view ")):
                            m = re.search(r"(?:cat|head|less|view)\s+([/\w._-]+\.py)", cmd)
                            if m:
                                current_step.files_read.append(m.group(1))
                        if "git diff" in cmd:
                            current_step.is_submit = "submit" in cmd.lower()

                    # Detect structured tool file ops
                    if isinstance(inp, dict):
                        path = inp.get("path", inp.get("file_path", ""))
                        if path:
                            write_tools = {"edit_file", "write_file", "create_file",
                                           "str_replace_editor", "str_replace"}
                            if any(t in tool.lower() for t in write_tools):
                                current_step.files_written.append(path)
                            else:
                                current_step.files_read.append(path)

            continue

        if role == "tool":
            if current_step is not None:
                max_len = 500 if verbose else 200
                current_step.tool_outputs.append(extract_tool_output(content, max_len))
            continue

    return steps


# ── Display Functions ───────────────────────────────────────────────────────

def print_header(text: str, char: str = "=", width: int = 80):
    print()
    print(C.bold(f" {text} ".center(width, char)))


def print_subheader(text: str, char: str = "-", width: int = 60):
    print(C.bold(f" {text} ".center(width, char)))


def display_step(step: Step, verbose: bool = False):
    """Display a single reconstructed step."""
    phase_str = C.magenta(f" PHASE: {step.phase}") if step.phase else ""
    print(f"  {C.bold(f'[step {step.index}]')}{phase_str}")

    if step.principals:
        print(f"    PRINCIPALS: [{', '.join(step.principals)}]")
    if step.root_cause:
        print(f"    {C.yellow('ROOT_CAUSE:')} {step.root_cause[:200]}")
    if step.plan:
        print(f"    {C.cyan('PLAN:')} {step.plan[:200]}")
    if step.fix_type:
        print(f"    {C.green('FIX_TYPE:')} {step.fix_type}")

    # Show assistant reasoning (first 200 chars if not verbose)
    if step.assistant_content:
        content = step.assistant_content
        # Skip if it's just the phase/principal declarations
        clean = re.sub(r"(?:PHASE|PRINCIPALS?|ROOT_CAUSE|PLAN|FIX_TYPE|EVIDENCE):.*?(?:\n|$)", "", content, flags=re.IGNORECASE)
        clean = clean.strip()
        if clean:
            max_len = 0 if not verbose else 500
            if max_len and len(clean) > max_len:
                clean = clean[:max_len] + "..."
            if verbose and clean:
                print(f"    {C.dim('reasoning:')} {clean[:300]}")

    # Show tool calls
    for j, action in enumerate(step.actions):
        print(f"    {C.blue('tool:')} {action}")

    # Show tool outputs (condensed)
    for j, output in enumerate(step.tool_outputs):
        if output.strip():
            lines = output.strip().split("\n")
            first_line = lines[0][:150]
            if len(lines) > 1:
                first_line += f" ... ({len(lines)} lines)"
            print(f"    {C.dim('result:')} {first_line}")


def display_phase_records(phase_records: list[dict]):
    """Display phase records from jingu_body."""
    if not phase_records:
        print(f"  {C.dim('(no phase records)')}")
        return

    for pr in phase_records:
        phase = pr.get("phase", "?")
        subtype = pr.get("subtype", "?")
        principals = pr.get("principals", [])
        content = pr.get("content_preview", "")
        root_cause = pr.get("root_cause", "")
        plan = pr.get("plan", "")

        print(f"  {C.magenta(phase)} ({subtype})")
        if principals:
            print(f"    principals: [{', '.join(principals)}]")
        if root_cause:
            print(f"    {C.yellow('root_cause:')} {root_cause}")
        if plan:
            print(f"    {C.cyan('plan:')} {plan}")
        if content:
            print(f"    {C.dim('content:')} {content[:150]}")


def display_principal_inference(pi_list: list[dict]):
    """Display principal inference results."""
    if not pi_list:
        print(f"  {C.dim('(no principal inference data)')}")
        return

    for pi in pi_list:
        phase = pi.get("phase", "?")
        subtype = pi.get("subtype", "?")
        declared = pi.get("declared", [])
        inferred = pi.get("inferred", {})
        diff = pi.get("diff", {})
        details = pi.get("details", {})

        print(f"  {C.magenta(phase)} ({subtype})")
        print(f"    declared:  [{', '.join(declared)}]" if declared else "    declared:  []")
        present = inferred.get("present", [])
        absent = inferred.get("absent", [])
        if present:
            print(f"    {C.green('inferred:')}  [{', '.join(present)}]")
        if absent:
            print(f"    {C.red('absent:')}    [{', '.join(absent)}]")

        # Diff results
        missing_req = diff.get("missing_required", [])
        missing_exp = diff.get("missing_expected", [])
        fake = diff.get("fake", [])
        if missing_req:
            print(f"    {C.red('missing_required:')} [{', '.join(missing_req)}]")
        if missing_exp:
            print(f"    {C.yellow('missing_expected:')} [{', '.join(missing_exp)}]")
        if fake:
            print(f"    {C.red('FAKE:')} [{', '.join(fake)}]")

        # Per-principal details
        if details:
            for pname, detail in details.items():
                score = detail.get("score", 0)
                signals = detail.get("signals", [])
                explanation = detail.get("explanation", "")
                color = C.green if score >= 0.5 else C.red
                print(f"    {color(f'{pname}')}: score={score} signals={signals}")
                if explanation:
                    print(f"      {C.dim(explanation[:150])}")


def display_verify_history(vh_list: list[dict]):
    """Display verify history entries."""
    if not vh_list:
        print(f"  {C.dim('(no verify history)')}")
        return

    prev_passed = None
    for vh in vh_list:
        step = vh.get("step", "?")
        passed = vh.get("tests_passed", 0)
        failed = vh.get("tests_failed", 0)
        exit_code = vh.get("exit_code", "?")
        elapsed = vh.get("elapsed_ms", 0)
        kind = vh.get("kind", "?")
        delta = vh.get("delta")

        # Color based on result
        if passed > 0 and failed == 0:
            status = C.green("ALL PASS")
        elif passed > 0:
            status = C.yellow(f"PARTIAL ({passed} passed, {failed} failed)")
        else:
            status = C.red(f"FAIL ({failed} failed)")

        delta_str = ""
        if delta is not None:
            if delta > 0:
                delta_str = C.green(f" delta=+{delta}")
            elif delta < 0:
                delta_str = C.red(f" delta={delta}")
            else:
                delta_str = C.dim(" delta=0")
        elif prev_passed is not None and passed >= 0:
            d = passed - prev_passed
            if d > 0:
                delta_str = C.green(f" delta=+{d}")
            elif d < 0:
                delta_str = C.red(f" delta={d}")

        print(f"  step={step:>3}  {kind:<30}  {status}  "
              f"exit={exit_code}  {elapsed:.0f}ms{delta_str}")

        if passed >= 0:
            prev_passed = passed


def display_controlled_verify(cv: dict):
    """Display controlled verify result."""
    if not cv:
        print(f"  {C.dim('(no controlled verify)')}")
        return

    kind = cv.get("verification_kind", "?")
    passed = cv.get("tests_passed", 0)
    failed = cv.get("tests_failed", 0)
    exit_code = cv.get("exit_code", "?")
    elapsed = cv.get("elapsed_ms", 0)

    if passed > 0 and failed == 0:
        status = C.green("ALL PASS")
    elif passed == -1 and failed == -1:
        status = C.red("PARSE ERROR (passed=-1, failed=-1)")
    elif passed > 0:
        status = C.yellow(f"PARTIAL ({passed} passed, {failed} failed)")
    else:
        status = C.red(f"FAIL ({failed} failed)")

    print(f"  kind: {kind}")
    print(f"  result: {status}")
    print(f"  exit_code: {exit_code}")
    print(f"  elapsed: {elapsed:.0f}ms")


def display_parsed_test_results(ptr: dict):
    """Display parsed test results."""
    if not ptr:
        return

    failing = ptr.get("failing_tests", [])
    excerpts = ptr.get("error_excerpts", [])
    summary = ptr.get("summary", "")
    partial = ptr.get("partial_progress", False)

    if failing:
        print(f"  {C.red('failing tests:')}")
        for t in failing[:10]:
            print(f"    - {t}")
        if len(failing) > 10:
            print(f"    ... and {len(failing) - 10} more")
    if summary:
        print(f"  summary: {summary[:200]}")
    if partial:
        print(f"  {C.yellow('partial progress: yes')}")
    if excerpts:
        print(f"  error excerpts ({len(excerpts)}):")
        for exc in excerpts[:3]:
            print(f"    {C.dim(str(exc)[:200])}")


def display_patch(jb: dict, info: dict):
    """Display the patch diff."""
    submission = info.get("submission", "")
    if not submission:
        print(f"  {C.dim('(no patch submitted)')}")
        return

    lines = submission.strip().split("\n")
    for line in lines[:50]:
        if line.startswith("+") and not line.startswith("+++"):
            print(f"  {C.green(line)}")
        elif line.startswith("-") and not line.startswith("---"):
            print(f"  {C.red(line)}")
        elif line.startswith("@@"):
            print(f"  {C.cyan(line)}")
        elif line.startswith("diff "):
            print(f"  {C.bold(line)}")
        else:
            print(f"  {line}")
    if len(lines) > 50:
        print(f"  {C.dim(f'... ({len(lines) - 50} more lines)')}")


def compute_verdict(jb: dict, info: dict) -> str:
    """Compute a verdict string from jingu_body."""
    exit_status = jb.get("exit_status", info.get("exit_status", ""))
    cv = jb.get("controlled_verify", {})
    cv_passed = cv.get("tests_passed", 0)
    cv_failed = cv.get("tests_failed", 0)
    verify_skipped = jb.get("verify_skipped", False)
    skip_reason = jb.get("verify_skip_reason", "")
    files_written = jb.get("files_written", [])

    if cv_passed > 0 and cv_failed == 0:
        return C.green("RESOLVED (all tests pass)")
    if cv_passed > 0 and cv_failed > 0:
        return C.yellow(f"PARTIAL ({cv_passed} pass, {cv_failed} fail)")
    if verify_skipped:
        return C.red(f"VERIFY SKIPPED ({skip_reason})")
    if not files_written:
        return C.red("NO PATCH (no files written)")
    if cv_failed > 0:
        return C.red(f"UNRESOLVED ({cv_failed} tests still failing)")
    if cv_passed == -1:
        return C.red("UNRESOLVED (test parse error)")
    if exit_status == "early_exit":
        return C.red("EARLY EXIT (agent stopped early)")
    return C.red(f"UNRESOLVED (exit={exit_status})")


# ── Main Replay ─────────────────────────────────────────────────────────────

def replay_single_traj(traj: dict, verbose: bool = False, summary_only: bool = False):
    """Full replay of a single traj.json."""

    info = traj.get("info", {})
    messages = traj.get("messages", [])
    jb = traj.get("jingu_body", {})
    instance_id = traj.get("instance_id", info.get("instance_id", "unknown"))

    print_header(f"INSTANCE: {instance_id}")

    # ── Summary metrics ──
    exit_status = jb.get("exit_status", info.get("exit_status", ""))
    tool_calls = jb.get("tool_calls_made", 0)
    files_read = jb.get("files_read", [])
    files_written = jb.get("files_written", [])
    patch_summary = jb.get("patch_summary", {})

    print(f"  exit_status: {exit_status}")
    print(f"  tool_calls: {tool_calls}")
    print(f"  files_read: {len(files_read)}")
    print(f"  files_written: {len(files_written)}")
    if files_written:
        for f in files_written[:10]:
            print(f"    {C.green(f)}")
    print(f"  patch: {patch_summary.get('files_changed', 0)} files, "
          f"{patch_summary.get('hunks', 0)} hunks, "
          f"+{patch_summary.get('lines_added', 0)}/-{patch_summary.get('lines_removed', 0)}")

    # Cost info from messages
    total_cost = 0.0
    for msg in messages:
        if msg.get("role") == "assistant":
            cost = msg.get("extra", {}).get("cost", 0)
            if cost:
                total_cost += cost
    if total_cost > 0:
        print(f"  cost: ${total_cost:.4f}")

    # ── Phase Records ──
    print_subheader("PHASE RECORDS")
    display_phase_records(jb.get("phase_records", []))

    # ── Principal Inference ──
    print_subheader("PRINCIPAL INFERENCE")
    display_principal_inference(jb.get("principal_inference", []))

    # ── Verify History ──
    print_subheader("VERIFY HISTORY")
    display_verify_history(jb.get("verify_history", []))

    # ── Controlled Verify ──
    print_subheader("CONTROLLED VERIFY (final)")
    display_controlled_verify(jb.get("controlled_verify", {}))
    display_parsed_test_results(jb.get("parsed_test_results", {}))

    # ── Bypassed Principals ──
    bp = jb.get("bypassed_principals", [])
    if bp:
        print_subheader("BYPASSED PRINCIPALS")
        print(f"  {C.yellow(', '.join(bp))}")

    # ── Verdict ──
    print_subheader("VERDICT")
    print(f"  {compute_verdict(jb, info)}")

    if summary_only:
        return

    # ── Step-by-step Replay ──
    print_subheader("STEP-BY-STEP REPLAY")
    steps = reconstruct_steps(messages, verbose=verbose)

    # Track phase transitions
    current_phase = None
    for step in steps:
        if step.phase and step.phase != current_phase:
            current_phase = step.phase
        elif not step.phase and current_phase:
            step.phase = current_phase  # inherit from previous

        display_step(step, verbose=verbose)
        print()

    # ── Patch ──
    print_subheader("PATCH DIFF")
    display_patch(jb, info)


def replay_multi_attempt_s3(batch_name: str, instance_id: str,
                            verbose: bool = False, summary_only: bool = False,
                            attempt_filter: int = 0):
    """Load and replay all attempts for an instance from S3."""
    trajs = load_traj_s3(batch_name, instance_id, attempt=0)

    if isinstance(trajs, dict):
        # Single traj returned
        trajs = [(1, trajs)]

    print_header(f"INSTANCE: {instance_id} (batch: {batch_name})", char="=", width=80)
    print(f"  attempts found: {len(trajs)}")

    for att_num, traj in trajs:
        if attempt_filter > 0 and att_num != attempt_filter:
            continue
        print_header(f"ATTEMPT {att_num}", char="-", width=60)
        replay_single_traj(traj, verbose=verbose, summary_only=summary_only)

    # Cross-attempt analysis
    if len(trajs) > 1 and attempt_filter == 0:
        print_header("CROSS-ATTEMPT ANALYSIS", char="=", width=80)
        for att_num, traj in trajs:
            jb = traj.get("jingu_body", {})
            info = traj.get("info", {})
            cv = jb.get("controlled_verify", {})
            cv_passed = cv.get("tests_passed", 0)
            cv_failed = cv.get("tests_failed", 0)
            files_written = jb.get("files_written", [])
            phase_records = jb.get("phase_records", [])
            phases = [pr.get("phase", "?") for pr in phase_records]

            verdict = compute_verdict(jb, info)
            print(f"  attempt {att_num}: {verdict}")
            print(f"    phases: {' -> '.join(phases) if phases else '(none)'}")
            print(f"    files_written: {len(files_written)}")
            print(f"    cv: passed={cv_passed} failed={cv_failed}")


# ── Batch Summary ───────────────────────────────────────────────────────────

def replay_batch_summary(trajs: list[tuple[str, dict]]):
    """Display summary table for a batch of trajs."""
    print_header("BATCH SUMMARY", char="=", width=100)

    # Table header
    print(f"  {'Instance':<35} {'Exit':<15} {'Files':<6} {'CV Pass':<8} "
          f"{'CV Fail':<8} {'Phases':<30} {'Verdict'}")
    print(f"  {'-'*35} {'-'*15} {'-'*6} {'-'*8} {'-'*8} {'-'*30} {'-'*20}")

    resolved = 0
    total = 0
    for instance_id, traj in trajs:
        total += 1
        jb = traj.get("jingu_body", {})
        info = traj.get("info", {})
        exit_status = jb.get("exit_status", info.get("exit_status", ""))[:14]
        files_written = len(jb.get("files_written", []))
        cv = jb.get("controlled_verify", {})
        cv_passed = cv.get("tests_passed", 0)
        cv_failed = cv.get("tests_failed", 0)
        phase_records = jb.get("phase_records", [])
        phases = [pr.get("phase", "?")[:3] for pr in phase_records]
        phase_str = "->".join(phases)[:29] if phases else "(none)"

        is_resolved = cv_passed > 0 and cv_failed == 0
        if is_resolved:
            resolved += 1

        verdict_short = "RESOLVED" if is_resolved else "UNRESOLVED"
        color = C.green if is_resolved else C.red

        print(f"  {instance_id:<35} {exit_status:<15} {files_written:<6} "
              f"{cv_passed:<8} {cv_failed:<8} {phase_str:<30} {color(verdict_short)}")

    print()
    rate = resolved / total * 100 if total else 0
    print(f"  Total: {total}  Resolved: {C.green(str(resolved))}  "
          f"Rate: {C.bold(f'{rate:.1f}%')}")


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Deep RCA analysis on SWE-bench traj.json files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s results/attempt_1/django__django-11099/django__django-11099.traj.json
  %(prog)s --s3 batch-p25-django-30 django__django-10914
  %(prog)s --s3-batch batch-p25-django-30
  %(prog)s --batch-dir results/batch-p25/
  %(prog)s --s3 batch-p25-django-30 django__django-10914 --summary
  %(prog)s --s3 batch-p25-django-30 django__django-10914 --verbose --attempt 2
        """,
    )

    # Input sources (mutually exclusive)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("local_path", nargs="?", help="Local path to traj.json")
    group.add_argument("--s3", nargs=2, metavar=("BATCH", "INSTANCE"),
                       help="Load from S3: --s3 <batch-name> <instance-id>")
    group.add_argument("--s3-batch", metavar="BATCH",
                       help="Replay all instances in an S3 batch (summary mode)")
    group.add_argument("--batch-dir", metavar="DIR",
                       help="Replay all traj.json files in a local directory")

    # Display options
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show full message content")
    parser.add_argument("--summary", "-s", action="store_true",
                        help="Show only key metrics, no step-by-step replay")
    parser.add_argument("--attempt", "-a", type=int, default=0,
                        help="Only show specific attempt number")
    parser.add_argument("--no-color", action="store_true",
                        help="Disable colored output")

    args = parser.parse_args()

    if args.no_color:
        C.ENABLED = False

    # Dispatch based on input source
    if args.s3:
        batch_name, instance_id = args.s3
        replay_multi_attempt_s3(
            batch_name, instance_id,
            verbose=args.verbose,
            summary_only=args.summary,
            attempt_filter=args.attempt,
        )

    elif args.s3_batch:
        instances = list_s3_batch_instances(args.s3_batch)
        if not instances:
            print(f"No instances found in s3://{S3_BUCKET}/{args.s3_batch}/")
            sys.exit(1)
        print(f"Found {len(instances)} instances in batch {args.s3_batch}")

        # Load all trajs (attempt_1 only for batch summary)
        trajs = []
        for inst_id in instances:
            try:
                traj = load_traj_s3(args.s3_batch, inst_id, attempt=1)
                trajs.append((inst_id, traj))
            except Exception as e:
                print(f"  WARN: failed to load {inst_id}: {e}")

        if args.summary or not args.verbose:
            replay_batch_summary(trajs)
        else:
            for inst_id, traj in trajs:
                replay_single_traj(traj, verbose=args.verbose, summary_only=args.summary)

    elif args.batch_dir:
        local_trajs = find_local_trajs(args.batch_dir)
        if not local_trajs:
            print(f"No traj.json files found in {args.batch_dir}")
            sys.exit(1)
        print(f"Found {len(local_trajs)} traj files in {args.batch_dir}")

        trajs = []
        for inst_id, path in local_trajs:
            try:
                traj = load_traj_local(path)
                trajs.append((inst_id, traj))
            except Exception as e:
                print(f"  WARN: failed to load {path}: {e}")

        if args.summary:
            replay_batch_summary(trajs)
        else:
            for inst_id, traj in trajs:
                replay_single_traj(traj, verbose=args.verbose, summary_only=False)

    elif args.local_path:
        traj = load_traj_local(args.local_path)
        replay_single_traj(traj, verbose=args.verbose, summary_only=args.summary)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
