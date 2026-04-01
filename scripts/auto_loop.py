#!/usr/bin/env python3
"""
AutoResearch loop for jingu-swebench.

Architecture:
  auto_loop.py            — this file, runs on LOCAL MAC
  run_with_jingu_gate.py  — the file Claude Code may modify
  program.md              — fixed goals (never modified)

Each round:
  1. Build a rich context document (goals + round history + current code + metrics)
  2. Invoke `claude --print` (Claude Code) with the context as prompt
     Claude Code can do ANYTHING:
       - SSH to cloud desktop (ssh cloud ...)
       - Run Docker containers for SWE-bench evaluation
       - Read logs, debug failures, root-cause analysis
       - Modify run_with_jingu_gate.py
       - Fetch documentation from the web
       - Run dry-runs, full evals, whatever it deems necessary
  3. Claude Code writes a JSON result to results/loop_round_NNN_result.json
  4. Loop checks if run_with_jingu_gate.py changed → re-eval → commit/rollback
  5. Append to loop_journal.jsonl for next round's context

Usage:
  python scripts/auto_loop.py
  python scripts/auto_loop.py --max-rounds 10
  python scripts/auto_loop.py --context-only    # just build context, don't call claude

Environment:
  CLAUDE_CLI   — path to claude CLI (default: claude)
  LOOP_TARGET  — stop when acceptance_rate >= this (default: 0.6)

Four-layer architecture:
  1. Laptop (control plane)  — auto_loop.py + claude --print live here
  2. Cloud Dev Desktop       — ssh cloud; Docker host; 1.2TB disk, 8 CPUs
  3. Docker containers       — fast feedback loop: git apply + pytest in ~30s/instance
  4. sb-cli submit           — ground truth / leaderboard evaluation (DO NOT substitute)

Cloud desktop commands:
  Refresh creds:  ssh cloud "~/.toolbox/bin/ada credentials update --account=235494812052 --provider=conduit --role=IibsAdminAccess-DO-NOT-DELETE --once"
  Python:         ssh cloud "~/.local/share/mise/shims/python ..."
  Docker images:  sweb.eval.x86_64.django__django-NNNNN:latest (built locally via prepare_images)
  Tag for agent:  docker tag sweb.eval.x86_64.X:latest swebench/sweb.eval.x86_64.X_with_1776:latest

Fast feedback eval (30s/instance, NOT ground truth):
  ssh cloud "docker run --rm -w /testbed sweb.eval.x86_64.django__django-11039:latest bash -c \
    'git apply /tmp/patch.diff && python -m pytest tests/... -x -q'"

Official eval (ground truth, use sparingly):
  ssh cloud "~/.local/share/mise/shims/python -m swebench.harness.run_evaluation \
    --dataset_name SWE-bench/SWE-bench_Lite \
    --predictions_path ~/jingu-swebench/results/run/jingu-predictions.jsonl \
    --instance_ids django__django-11039 --max_workers 4 --run_id test_X"

Generate patches (mini-SWE-agent + Jingu gate, run on cloud desktop):
  ssh cloud "~/.local/share/mise/shims/python ~/jingu-swebench/scripts/run_with_jingu_gate.py \
    --instance-ids django__django-11039 django__django-11001 \
    --max-attempts 3 --workers 4 --output ~/jingu-swebench/results/run_X/"

IMPORTANT:
  Docker pytest = fast iteration signal, NOT leaderboard ground truth
  sb-cli = ground truth judge; use only to validate significant improvements
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────

SCRIPT_DIR    = Path(__file__).parent
REPO_ROOT     = SCRIPT_DIR.parent
TARGET_SCRIPT = SCRIPT_DIR / "run_with_jingu_gate.py"
PROGRAM_MD    = SCRIPT_DIR / "program.md"
JOURNAL_PATH  = REPO_ROOT / "results" / "loop_journal.jsonl"
DOCS_DIR      = REPO_ROOT / "docs" / "swebench"

CLAUDE_CLI    = os.environ.get("CLAUDE_CLI", "claude")

DEFAULT_INSTANCES = [
    "django__django-11039",
    "django__django-11001",
    "django__django-11019",
    "django__django-11049",
    "django__django-11099",
]

# ── Journal ────────────────────────────────────────────────────────────────────

def load_journal() -> list[dict]:
    if not JOURNAL_PATH.exists():
        return []
    rounds = []
    for line in JOURNAL_PATH.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rounds.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rounds


def append_journal(entry: dict) -> None:
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(JOURNAL_PATH, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def format_round_history(rounds: list[dict]) -> str:
    if not rounds:
        return "No previous rounds."
    lines = []
    for r in rounds[-10:]:
        rn     = r.get("round", "?")
        metric = r.get("metric", {})
        rate   = metric.get("acceptance_rate", 0.0)
        acc    = metric.get("accepted", 0)
        tot    = metric.get("total", 0)
        hyp    = r.get("hypothesis", "")[:200]
        change = r.get("change_summary", "")[:200]
        nexts  = r.get("next_steps", "")[:200]
        commit = r.get("committed", False)
        delta  = f" delta={r['delta']:+.1%}" if "delta" in r else ""
        note   = r.get("note", "")
        lines.append(
            f"Round {rn}: {acc}/{tot} ({rate:.1%}){delta} committed={commit}"
            + (f" [{note}]" if note else "") + "\n"
            f"  hypothesis: {hyp}\n"
            f"  change_summary: {change}\n"
            f"  next_steps: {nexts}"
        )
    return "\n\n".join(lines)

# ── File hash ──────────────────────────────────────────────────────────────────

def file_hash(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()

# ── Context builder ────────────────────────────────────────────────────────────

def build_context(round_num: int, past_rounds: list[dict], instances: list[str]) -> str:
    program_goals = PROGRAM_MD.read_text() if PROGRAM_MD.exists() else "(program.md not found)"
    current_code  = TARGET_SCRIPT.read_text() if TARGET_SCRIPT.exists() else "(run_with_jingu_gate.py not found)"
    round_history = format_round_history(past_rounds)

    # Last metric for quick reference
    last_metric_str = "No previous runs."
    if past_rounds:
        last = past_rounds[-1]
        m = last.get("metric", {})
        last_metric_str = (
            f"resolve_rate={m.get('resolve_rate', m.get('acceptance_rate', 0)):.1%} "
            f"acceptance_rate(gate)={m.get('acceptance_rate', 0):.1%} "
            f"({m.get('accepted', 0)}/{m.get('total', 0)}) "
            f"resolved_ids={m.get('resolved_ids', [])} "
            f"fail_counts={json.dumps(m.get('fail_counts', {}))}"
        )
        if "metric_after" in last:
            ma = last["metric_after"]
            last_metric_str += (
                f"\n  After change: resolve_rate={ma.get('resolve_rate', ma.get('acceptance_rate', 0)):.1%} "
                f"acceptance_rate(gate)={ma.get('acceptance_rate', 0):.1%} "
                f"({ma.get('accepted', 0)}/{ma.get('total', 0)}) "
                f"resolved_ids={ma.get('resolved_ids', [])}"
            )

    return f"""# AutoResearch Loop — Round {round_num}

You are the autonomous optimization agent for jingu-swebench.
Your goal is to improve resolve_rate on SWE-bench instances.

---

## YOUR ROLE (read carefully before doing anything)

You have ONE job: **analyze round history → form hypothesis → change ONE thing in
`run_with_jingu_gate.py` → write result JSON → EXIT.**

You do NOT run eval. You do NOT trigger patch generation. You do NOT check cloud logs
unless you need to diagnose a specific failure. auto_loop owns the entire eval pipeline.

---

## HOW THE SYSTEM WORKS (complete picture)

### Files and ownership

```
scripts/
  run_with_jingu_gate.py   ← THE ONLY FILE YOU MAY MODIFY
                              Contains: jingu gate logic, BASE_CONFIG, normalize_patch,
                              jingu_structural_check, score_patch, run_with_jingu()
                              Imports from: swebench_infra.py

  swebench_infra.py        ← DO NOT TOUCH (infrastructure, not gate logic)
                              Contains: Timer, ModelUsage, run_agent(), write_predictions(),
                              run_parallel(), _load_instances()

  auto_loop.py             ← DO NOT TOUCH (runs on laptop, owns eval pipeline)
  fast_eval.py             ← DO NOT TOUCH (resolve evaluator, owned by auto_loop)
  program.md               ← DO NOT TOUCH (fixed goals)
```

### What auto_loop does each round (you do NOT do any of this)

```
Step 1 — invoke you (claude --print) with this context
Step 2 — detect if run_with_jingu_gate.py changed (file hash before vs after)
Step 3 — if changed:
    Stage 1 (LOCAL, ~15-20 min):
      python run_with_jingu_gate.py --instance-ids ... → patches
      (runs on laptop, calls Bedrock directly, no ssh needed)
    Stage 2 (CLOUD,  ~2-3 min):
      scp predictions → cloud
      ssh cloud: python fast_eval.py → Docker pytest → resolve_rate
Step 4 — write results to journal → feed into next round context
```

**Key points:**
- `run_with_jingu_gate.py` runs LOCALLY. The cloud is only used for Docker eval (fast_eval.py).
- There is NO sync of scripts to cloud. The local file IS what gets executed.
- You do NOT need to check cloud script versions — they are irrelevant.
- You do NOT need to scp anything. You do NOT need ssh for patch generation.

### Cloud layout (for read-only investigation of past runs only)

```
~/jingu-swebench/results/loop_round_NNN_*/   ← old run outputs from before this change
                                                (when stage1 still ran on cloud)
```

**Note:** Logs from rounds ≤ 033 show stage1 running on cloud (step_limit=60 on cloud).
From round 034 onwards, stage1 runs locally — cloud logs will only contain stage2 (fast_eval).

**Do not SSH to check script versions.** The LOCAL file shown below is what gets executed.

---

## EVAL PIPELINE INVARIANTS

- `resolve_rate` = fraction of instances where FAIL_TO_PASS tests pass in Docker
- `acceptance_rate` = fraction where Jingu structural gate passes (format check only)
- High acceptance + low resolve = gate too weak (not your optimization target)
- Optimize for `resolve_rate`, not `acceptance_rate`
- fast_eval.py is the signal source — DO NOT modify it

---

## Program Goals

{program_goals}

---

## Round History (most recent last)

{round_history}

## Last Known Metric
{last_metric_str}

## Instances Being Tested
{", ".join(instances)}

---

## Current run_with_jingu_gate.py (LOCAL — this is what will be synced to cloud)

```python
{current_code}
```

---

## Your Task for Round {round_num}

**Step 1 — Analyze**: Read the round history above. What is the dominant failure pattern?
What changed between rounds? What hypotheses have already been tested?

**Step 2 — Investigate (only if needed)**: SSH to cloud to read logs or traj files.
- Read logs: `ssh cloud "cat ~/jingu-swebench/results/loop_round_NNN_*/run.log"`
- Read traj: `ssh cloud "cat ~/jingu-swebench/results/loop_round_NNN_*/attempt_1/INSTANCE/INSTANCE.traj.json"`
- DO NOT check cloud script versions (irrelevant — auto_loop overwrites before eval)
- DO NOT run any eval commands on cloud

**Step 3 — Hypothesize**: Form ONE testable hypothesis about what will improve resolve_rate.

**Step 4 — Implement**: Make ONE targeted change to `{TARGET_SCRIPT}`.
Only change what your hypothesis requires. No compound changes.

**Step 5 — Record**: Write result JSON to `{REPO_ROOT}/results/loop_round_{round_num:03d}_result.json`
then EXIT. auto_loop will detect the file change, sync to cloud, run eval, and report in round {round_num + 1}.

```json
{{
  "round": {round_num},
  "hypothesis": "one sentence: what you believe will improve resolve_rate and why",
  "change_summary": "what specific change you made to run_with_jingu_gate.py (or 'none')",
  "expected_improvement": "e.g. +1 resolve (11019) by allowing step_limit=100",
  "next_steps": "what to try next if this hypothesis is wrong or if it works",
  "actions_taken": ["list", "of", "things", "you", "did"]
}}
```

If you made no change, set `change_summary` to `"none"` and explain in `hypothesis`.
""".strip()

# ── Claude Code executor ───────────────────────────────────────────────────────

def run_claude_agent(context: str, round_num: int, timeout_s: int) -> dict:
    """Invoke Claude Code CLI with context. Returns parsed result."""
    result_path = REPO_ROOT / "results" / f"loop_round_{round_num:03d}_result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)

    # Remove stale result from previous attempt
    if result_path.exists():
        result_path.unlink()

    hash_before = file_hash(TARGET_SCRIPT)

    log_path = REPO_ROOT / "results" / f"loop_round_{round_num:03d}_agent.log"
    print(f"  [loop] invoking Claude Code (timeout={timeout_s}s)...")
    print(f"  [loop] result will be written to: {result_path.name}")
    print(f"  [loop] live log: tail -f {log_path}")

    # Deny list: eval files the agent must never write/execute.
    # The agent may SSH to cloud for read-only investigation, but must not
    # trigger fast_eval.py, run_with_jingu_gate.py, or touch infra files.
    DENIED_TOOLS = [
        # Block writes to protected scripts
        f"Edit({SCRIPT_DIR}/fast_eval.py)",
        f"Edit({SCRIPT_DIR}/swebench_infra.py)",
        f"Edit({SCRIPT_DIR}/auto_loop.py)",
        f"Edit({SCRIPT_DIR}/compare_groups.py)",
        f"Write({SCRIPT_DIR}/fast_eval.py)",
        f"Write({SCRIPT_DIR}/swebench_infra.py)",
        f"Write({SCRIPT_DIR}/auto_loop.py)",
        f"Write({SCRIPT_DIR}/compare_groups.py)",
        # Block triggering eval pipeline on cloud
        "Bash(ssh * fast_eval*)",
        "Bash(ssh * run_with_jingu_gate*)",
        "Bash(ssh * swebench.harness*)",
    ]

    # stream-json + --verbose streams one JSON event per line in real time
    cmd = [CLAUDE_CLI, "--print", "--output-format", "stream-json", "--verbose",
           "--dangerously-skip-permissions",
           "--disallowedTools", " ".join(DENIED_TOOLS),
           "-p", context]

    text_chunks = []   # accumulated assistant text (for stdout compat)
    stderr_chunks = []
    returncode = -1

    try:
        import select
        with open(log_path, "w", buffering=1) as log_f:
            # Use /tmp as cwd so claude doesn't load jingu's CLAUDE.md rules
            # (RPP/Architect hooks are irrelevant for swebench agent)
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd="/tmp",
            )
            deadline = time.monotonic() + timeout_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    proc.kill()
                    proc.wait()
                    print(f"  [loop] Claude Code timed out after {timeout_s}s")
                    return {"success": False, "note": "timeout", "file_changed": False}
                rlist, _, _ = select.select([proc.stdout, proc.stderr], [], [], min(remaining, 1.0))
                for stream in rlist:
                    line = stream.readline()
                    if not line:
                        continue
                    if stream is proc.stderr:
                        stderr_chunks.append(line)
                        log_f.write(line)
                        continue
                    # stream-json: parse each event line
                    try:
                        ev = json.loads(line)
                        etype = ev.get("type", "")
                        if etype == "assistant":
                            for block in ev.get("message", {}).get("content", []):
                                if block.get("type") == "text":
                                    text = block.get("text", "")
                                    if text:
                                        text_chunks.append(text)
                                        for tl in text.splitlines(keepends=True):
                                            log_f.write(tl)
                                            print(f"  [agent] {tl.rstrip()}", flush=True)
                        elif etype == "system" and ev.get("subtype") == "init":
                            model = ev.get("model", "")
                            log_f.write(f"[model] {model}\n")
                            print(f"  [agent:model] {model}", flush=True)
                        elif etype == "result":
                            cost = ev.get("total_cost_usd", 0)
                            turns = ev.get("num_turns", 0)
                            log_f.write(f"[result] turns={turns} cost=${cost:.4f}\n")
                            print(f"  [agent:done] turns={turns} cost=${cost:.4f}", flush=True)
                        # skip hook/tool_use noise from log (write raw line for debugging)
                        log_f.write(line)
                    except json.JSONDecodeError:
                        log_f.write(line)
                if proc.poll() is not None and not rlist:
                    for line in proc.stdout:
                        log_f.write(line)
                        try:
                            ev = json.loads(line)
                            if ev.get("type") == "assistant":
                                for block in ev.get("message", {}).get("content", []):
                                    if block.get("type") == "text":
                                        text_chunks.append(block.get("text", ""))
                        except json.JSONDecodeError:
                            pass
                    for line in proc.stderr:
                        stderr_chunks.append(line)
                        log_f.write(line)
                    break
            returncode = proc.wait()
    except FileNotFoundError:
        print(f"  [loop] ERROR: claude CLI not found at: {CLAUDE_CLI}")
        print(f"  [loop] Install: npm install -g @anthropic-ai/claude-code")
        sys.exit(1)

    stdout = "\n".join(text_chunks)
    stderr = "".join(stderr_chunks)

    hash_after   = file_hash(TARGET_SCRIPT)
    file_changed = (hash_after != hash_before)

    print(f"  [loop] Claude exit={returncode}  file_changed={file_changed}")
    if returncode != 0 and stderr:
        print(f"  [loop] stderr: {stderr[:300]}")

    # Read result JSON written by Claude
    result = {}
    if result_path.exists():
        try:
            result = json.loads(result_path.read_text())
            print(f"  [loop] result JSON loaded from {result_path.name}")
        except json.JSONDecodeError as e:
            print(f"  [loop] result JSON parse error: {e}")
    else:
        print(f"  [loop] WARNING: Claude did not write result JSON to {result_path.name}")
        # Try to extract JSON from stdout as fallback
        import re
        match = re.search(r'\{[^{}]*"hypothesis"[^{}]*\}', stdout, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group())
                print(f"  [loop] extracted result from stdout (fallback)")
            except json.JSONDecodeError:
                pass

    return {
        "success": bool(result),
        "file_changed": file_changed,
        "hypothesis": result.get("hypothesis", ""),
        "change_summary": result.get("change_summary", "none"),
        "expected_improvement": result.get("expected_improvement", ""),
        "next_steps": result.get("next_steps", ""),
        "actions_taken": result.get("actions_taken", []),
        "note": result.get("note", ""),
        "stdout_tail": stdout[-500:] if stdout else "",
    }

# ── Git helpers ────────────────────────────────────────────────────────────────

def git_commit(message: str) -> bool:
    subprocess.run(["git", "add", str(TARGET_SCRIPT)], cwd=str(REPO_ROOT), capture_output=True)
    r = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(REPO_ROOT), capture_output=True, text=True
    )
    if r.returncode == 0:
        print(f"  [loop] committed: {message.splitlines()[0][:80]}")
        return True
    print(f"  [loop] commit failed: {r.stderr.strip()[:200]}")
    return False


def git_reset_target() -> None:
    subprocess.run(
        ["git", "checkout", "HEAD", "--", str(TARGET_SCRIPT)],
        cwd=str(REPO_ROOT), check=True
    )
    print(f"  [loop] reset {TARGET_SCRIPT.name} to HEAD")

# ── Eval runner (for measuring delta after Claude's change) ───────────────────

CLOUD_HOST    = os.environ.get("CLOUD_HOST", "cloud")
CLOUD_SCRIPTS = os.environ.get("CLOUD_SCRIPTS", "~/jingu-swebench/scripts")
CLOUD_RESULTS = os.environ.get("CLOUD_RESULTS", "~/jingu-swebench/results")
CLOUD_PYTHON  = os.environ.get("CLOUD_PYTHON", "~/.local/share/mise/shims/python")


def _ssh(cmd: str, timeout: int = 600, prefix: str = "") -> subprocess.CompletedProcess:
    """Run a command on cloud desktop via SSH, streaming output in real time."""
    proc = subprocess.Popen(
        ["ssh", CLOUD_HOST, cmd],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    )
    out_lines = []
    try:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                proc.wait()
                raise subprocess.TimeoutExpired(cmd, timeout)
            import select as _select
            rlist, _, _ = _select.select([proc.stdout], [], [], min(remaining, 1.0))
            if rlist:
                line = proc.stdout.readline()
                if line:
                    out_lines.append(line)
                    tag = f"  [{prefix}] " if prefix else "  [ssh] "
                    print(tag + line.rstrip(), flush=True)
            if proc.poll() is not None:
                for line in proc.stdout:
                    out_lines.append(line)
                    tag = f"  [{prefix}] " if prefix else "  [ssh] "
                    print(tag + line.rstrip(), flush=True)
                break
    except subprocess.TimeoutExpired:
        raise
    stdout = "".join(out_lines)
    return subprocess.CompletedProcess(
        args=["ssh", CLOUD_HOST, cmd],
        returncode=proc.wait(),
        stdout=stdout,
        stderr="",
    )


def run_cloud_eval(instances: list[str], output_dir: Path, workers: int,
                   max_attempts: int, stagger: int) -> dict:
    """Two-stage eval: generate patches locally, then fast-eval on cloud (Docker).

    Stage 1 (LOCAL, ~15-20 min): run_with_jingu_gate.py → patches via Bedrock
    Stage 2 (CLOUD,  ~2-3 min):  ssh → fast_eval.py → Docker pytest → resolve_rate

    acceptance_rate = Jingu gate pass rate (structural)
    resolve_rate    = FAIL_TO_PASS test pass rate (semantic, fast signal)
    """
    import re
    t_total = time.time()
    run_name = output_dir.name
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "run.log"

    # ── Stage 1: Generate patches LOCALLY ────────────────────────────────────
    # run_with_jingu_gate.py runs on the laptop, calls Bedrock directly.
    # No ssh needed for patch generation.
    LOCAL_PYTHON = sys.executable
    gate_cmd = [
        LOCAL_PYTHON, str(TARGET_SCRIPT),
        "--instance-ids", *instances,
        "--output", str(output_dir),
        "--workers", str(workers),
        "--max-attempts", str(max_attempts),
        "--stagger", str(stagger),
    ]
    print(f"  [eval:stage1] patch generation LOCAL ({len(instances)} instances, "
          f"workers={workers}, max_attempts={max_attempts})...")
    t0 = time.time()

    with open(log_path, "w", buffering=1) as log_f:
        proc = subprocess.Popen(
            gate_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, cwd=str(REPO_ROOT),
        )
        import select as _sel
        deadline = t0 + max_attempts * len(instances) * 400 + 60
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill(); proc.wait()
                print(f"  [eval:stage1] TIMEOUT")
                break
            rlist, _, _ = _sel.select([proc.stdout], [], [], min(remaining, 1.0))
            if rlist:
                line = proc.stdout.readline()
                if line:
                    log_f.write(line)
                    print(f"  [stage1] {line.rstrip()}", flush=True)
            if proc.poll() is not None:
                for line in proc.stdout:
                    log_f.write(line)
                    print(f"  [stage1] {line.rstrip()}", flush=True)
                break

    stage1_time = time.time() - t0
    print(f"  [eval:stage1] done in {stage1_time:.0f}s  (rc={proc.returncode})")

    # Parse run_report.json for LLM usage summary
    report_path = output_dir / "run_report.json"
    llm_summary = {}
    if report_path.exists():
        try:
            rpt = json.loads(report_path.read_text())
            mu = rpt.get("model_usage", {})
            llm_summary = {
                "api_calls":     mu.get("total_api_calls", 0),
                "input_tokens":  mu.get("total_input_tokens", 0),
                "output_tokens": mu.get("total_output_tokens", 0),
                "cost_usd":      mu.get("total_cost_usd", 0),
                "avg_calls":     mu.get("avg_calls_per_instance", 0),
                "avg_cost":      mu.get("avg_cost_per_instance", 0),
            }
            print(f"\n  ┌─ Stage 1 Summary ───────────────────────────────────")
            print(f"  │  wall_time   : {stage1_time:.0f}s")
            print(f"  │  api_calls   : {llm_summary['api_calls']} total  ({llm_summary['avg_calls']} avg/instance)")
            print(f"  │  tokens in   : {llm_summary['input_tokens']:,}")
            print(f"  │  tokens out  : {llm_summary['output_tokens']:,}")
            print(f"  │  cost        : ${llm_summary['cost_usd']:.4f} total  (${llm_summary['avg_cost']:.4f} avg/instance)")
            per = mu.get("per_instance", {})
            for iid in instances:
                u = per.get(iid, {})
                accepted = (output_dir / "jingu-predictions.jsonl").exists()
                print(f"  │  {iid:35s}  calls={u.get('api_calls',0):3d}  cost=${u.get('cost_usd',0):.3f}")
            print(f"  └────────────────────────────────────────────────────\n")
        except Exception:
            pass

    # ── Copy predictions to cloud for fast_eval ───────────────────────────────
    preds_local  = output_dir / "jingu-predictions.jsonl"
    cloud_out    = f"{CLOUD_RESULTS}/{run_name}"
    preds_remote = f"{cloud_out}/jingu-predictions.jsonl"

    # ── Stage 2: Fast resolve eval on cloud (Docker) ──────────────────────────
    resolve_rate = 0.0
    resolved_ids: list[str] = []
    fast_results: dict = {}
    stage2_time = 0.0

    if preds_local.exists() and preds_local.stat().st_size > 0:
        # Ensure remote dir exists, then scp predictions up
        _ssh(f"mkdir -p {cloud_out}", timeout=15, prefix="mkdir")
        scp_up = subprocess.run(
            ["scp", str(preds_local), f"{CLOUD_HOST}:{preds_remote}"],
            capture_output=True, text=True, timeout=60,
        )
        if scp_up.returncode != 0:
            print(f"  [eval] WARNING: scp predictions→cloud failed: {scp_up.stderr[:100]}")
        else:
            print(f"  [eval] predictions uploaded to cloud ({preds_local.stat().st_size} bytes)")

        fast_cmd = (
            f"{CLOUD_PYTHON} {CLOUD_SCRIPTS}/fast_eval.py "
            f"--predictions {preds_remote} "
            f"--instance-ids {' '.join(instances)} "
            f"--workers {min(workers, len(instances))} "
            f"--remote ''"
        )
        print(f"  [eval:stage2] fast resolve eval on cloud (Docker)...")
        t1 = time.time()
        r2 = _ssh(fast_cmd, timeout=180, prefix="stage2")
        stage2_time = time.time() - t1
        print(f"  [eval:stage2] done in {stage2_time:.0f}s")

        for line in (r2.stdout + r2.stderr).splitlines():
            if "✓ resolved" in line:
                iid = line.strip().split(":")[0].strip()
                resolved_ids.append(iid)
                fast_results[iid] = True
            elif "✗ not resolved" in line:
                iid = line.strip().split(":")[0].strip()
                fast_results[iid] = False

        resolve_rate = len(resolved_ids) / len(instances) if instances else 0.0
    else:
        print(f"  [eval:stage2] SKIPPED — no predictions file")

    # ── Final summary ─────────────────────────────────────────────────────────
    total_time = time.time() - t_total
    print(f"\n  ┌─ Eval Summary ──────────────────────────────────────")
    print(f"  │  stage1 (patch gen, local) : {stage1_time:.0f}s")
    print(f"  │  stage2 (docker eval, cloud): {stage2_time:.0f}s")
    print(f"  │  total                      : {total_time:.0f}s")
    print(f"  │  resolve_rate               : {resolve_rate:.1%}  ({len(resolved_ids)}/{len(instances)})")
    print(f"  │  resolved                   : {resolved_ids}")
    for iid in instances:
        sym = "✓" if fast_results.get(iid) else "✗"
        print(f"  │    {sym} {iid}")
    print(f"  └────────────────────────────────────────────────────\n")

    base_results = _parse_results(output_dir, instances, log_path)
    base_results["resolve_rate"]   = resolve_rate
    base_results["resolved_ids"]   = resolved_ids
    base_results["fast_results"]   = fast_results
    base_results["acceptance_rate"] = resolve_rate
    base_results["llm_summary"]    = llm_summary
    base_results["stage1_time_s"]  = round(stage1_time, 1)
    base_results["stage2_time_s"]  = round(stage2_time, 1)
    return base_results


def _parse_results(output_dir: Path, instances: list[str], log_path: Path) -> dict:
    import re
    preds_path = output_dir / "jingu-predictions.jsonl"
    preds = {}
    if preds_path.exists():
        for line in preds_path.read_text().splitlines():
            if line.strip():
                d = json.loads(line)
                preds[d["instance_id"]] = d

    failures: dict[str, list[str]] = {iid: [] for iid in instances}
    if log_path.exists():
        log_text = log_path.read_text()
        for iid in instances:
            pattern = rf"\[jingu\] {re.escape(iid)}.*?(?=\[jingu\] |\Z)"
            for m in re.findall(pattern, log_text, re.DOTALL):
                for code in ["EMPTY_PATCH", "PARSE_FAILED", "UNGROUNDED_PATCH",
                             "PATCH_APPLY_FAILED", "TESTS_NOT_IMPROVED"]:
                    if code in m:
                        failures[iid].append(code)

    accepted_ids = list(preds.keys())
    accepted     = len(accepted_ids)
    total        = len(instances)
    fail_counts: dict[str, int] = {}
    for codes in failures.values():
        for c in codes:
            fail_counts[c] = fail_counts.get(c, 0) + 1
    patch_lines = [len(preds[i]["model_patch"].splitlines()) for i in accepted_ids]

    return {
        "accepted": accepted, "total": total,
        "acceptance_rate": accepted / total if total else 0.0,
        "accepted_ids": accepted_ids,
        "fail_counts": fail_counts,
        "avg_patch_lines": sum(patch_lines) / len(patch_lines) if patch_lines else 0.0,
        "per_instance": {
            iid: {"accepted": iid in preds,
                  "fail_codes": failures.get(iid, []),
                  "patch_lines": len(preds[iid]["model_patch"].splitlines()) if iid in preds else 0}
            for iid in instances
        },
    }

# ── Main loop ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AutoResearch loop — Claude Code as agent")
    parser.add_argument("--max-rounds", type=int, default=20)
    parser.add_argument("--instances", nargs="+", default=DEFAULT_INSTANCES)
    parser.add_argument("--workers", type=int, default=2,
                        help="Workers for local eval (default 2; cloud desktop runs the real evals)")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--stagger", type=int, default=20)
    parser.add_argument("--target", type=float,
                        default=float(os.environ.get("LOOP_TARGET", "0.6")))
    parser.add_argument("--claude-timeout", type=int, default=1800,
                        help="Timeout for Claude Code agent in seconds (default 600)")
    parser.add_argument("--context-only", action="store_true",
                        help="Just build and print the context document, don't call Claude")
    parser.add_argument("--no-eval", action="store_true",
                        help="Skip baseline eval (use last round's metric as baseline)")
    args = parser.parse_args()

    print(f"""
╔══════════════════════════════════════════════════════════╗
║  jingu-swebench AutoResearch Loop                        ║
║  agent: Claude Code (claude --print)                     ║
║  eval:  cloud desktop Docker + AWS Bedrock               ║
║  target: {args.target:.0%}  max_rounds={args.max_rounds}                  ║
╚══════════════════════════════════════════════════════════╝
""")

    past_rounds = load_journal()
    round_num   = len([r for r in past_rounds if r.get("hypothesis") != "TARGET_REACHED"]) + 1

    if past_rounds:
        last      = past_rounds[-1]
        last_rate = last.get("metric", {}).get("acceptance_rate", 0)
        print(f"  Resuming from round {round_num} ({len(past_rounds)} past rounds)")
        print(f"  Last acceptance_rate: {last_rate:.1%}\n")

    for _ in range(args.max_rounds):
        print(f"\n{'='*62}")
        print(f"  ROUND {round_num}  [{datetime.now().strftime('%H:%M:%S')}]")
        print(f"{'='*62}")

        # ── Step 1: Baseline metric ────────────────────────────────────────────
        # Claude Code will run the real eval on cloud desktop.
        # Here we track the last known metric for context.
        last_metric = {}
        if past_rounds:
            last_entry = past_rounds[-1]
            # If previous round improved, use metric_after; otherwise metric
            if last_entry.get("improved"):
                last_metric = last_entry.get("metric_after", last_entry.get("metric", {}))
            else:
                last_metric = last_entry.get("metric", {})

        if not args.no_eval and not past_rounds:
            # First round: run a baseline eval locally so we have something
            print(f"  [loop] running baseline eval (round 1 only)...")
            out_dir     = REPO_ROOT / "results" / f"loop_round_{round_num:03d}_baseline"
            last_metric = run_cloud_eval(
                args.instances, out_dir, args.workers, args.max_attempts, args.stagger
            )
            rate = last_metric.get("acceptance_rate", 0)
            print(f"  Baseline: {rate:.1%} ({last_metric.get('accepted',0)}/{last_metric.get('total',0)})")
            print(f"  fail_counts: {last_metric.get('fail_counts', {})}")

            if rate >= args.target:
                print(f"\n  TARGET REACHED at baseline: {rate:.1%}")
                append_journal({
                    "round": round_num,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "metric": last_metric,
                    "hypothesis": "TARGET_REACHED",
                    "change_summary": "none",
                    "committed": False,
                })
                break

        # ── Step 2: Build context ──────────────────────────────────────────────
        context = build_context(
            round_num=round_num,
            past_rounds=past_rounds,
            instances=args.instances,
        )

        if args.context_only:
            print(f"\n  [context-only] context length: {len(context)} chars")
            print("\n" + "─"*62)
            print(context[:3000])
            if len(context) > 3000:
                print(f"\n  ... ({len(context)-3000} more chars)")
            break

        # ── Step 3: Claude Code agent ──────────────────────────────────────────
        def _phase(label: str):
            print(f"\n  ── {label}  [{datetime.now().strftime('%H:%M:%S')}] {'─'*(40-len(label))}", flush=True)

        _phase("PHASE 1/3 — agent analysis")
        hash_before = file_hash(TARGET_SCRIPT)
        result      = run_claude_agent(context, round_num, args.claude_timeout)

        print(f"\n  HYPOTHESIS:   {result.get('hypothesis','')[:120]}")
        print(f"  CHANGE:       {result.get('change_summary','')[:120]}")
        print(f"  EXPECTED:     {result.get('expected_improvement','')[:120]}")
        print(f"  NEXT STEPS:   {result.get('next_steps','')[:120]}")
        if result.get("actions_taken"):
            for a in result["actions_taken"]:
                print(f"  ACTION:       {a}")

        file_changed = result.get("file_changed", False)

        # ── Step 4: Re-eval if file changed ───────────────────────────────────
        metric_new = None
        improved   = False
        rate_new   = 0.0
        rate_old   = last_metric.get("acceptance_rate", 0.0)

        if file_changed:
            _phase("PHASE 2/3 — patch generation (local Bedrock)")
            out_dir_new = REPO_ROOT / "results" / f"loop_round_{round_num:03d}_new"
            metric_new  = run_cloud_eval(
                args.instances, out_dir_new, args.workers, args.max_attempts, args.stagger
            )
            rate_new = metric_new["acceptance_rate"]
            improved = rate_new > rate_old
            _phase("PHASE 3/3 — done")
            print(f"  BEFORE: {rate_old:.1%}   AFTER: {rate_new:.1%}   {'↑ IMPROVED' if improved else '↓ NO IMPROVEMENT'}")
        else:
            print(f"\n  [loop] no file change — skipping eval")

        # ── Step 5: Commit or rollback ─────────────────────────────────────────
        committed = False
        if file_changed:
            if improved:
                msg = (
                    f"experiment(loop-r{round_num}): {result.get('change_summary','')[:60]}\n\n"
                    f"Before: {rate_old:.1%}  After: {rate_new:.1%}\n"
                    f"Hypothesis: {result.get('hypothesis','')[:120]}"
                )
                committed = git_commit(msg)
            else:
                git_reset_target()

        # ── Step 6: Journal ────────────────────────────────────────────────────
        entry: dict = {
            "round": round_num,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "hypothesis": result.get("hypothesis", ""),
            "change_summary": result.get("change_summary", "none"),
            "expected_improvement": result.get("expected_improvement", ""),
            "next_steps": result.get("next_steps", ""),
            "actions_taken": result.get("actions_taken", []),
            "committed": committed,
        }
        if last_metric:
            entry["metric"] = last_metric
        if metric_new:
            entry["metric_after"] = metric_new
            entry["delta"]        = round(rate_new - rate_old, 4)
            entry["improved"]     = improved
        if result.get("note"):
            entry["note"] = result["note"]

        append_journal(entry)

        # ── Step 7: Next round ────────────────────────────────────────────────
        if improved:
            print(f"  Committed. New baseline: {rate_new:.1%}")
        elif file_changed:
            print(f"  Rolled back. Baseline: {rate_old:.1%}")

        round_num  += 1
        past_rounds = load_journal()

        final_rate = rate_new if improved else rate_old
        if final_rate >= args.target:
            print(f"\n  TARGET REACHED: {final_rate:.1%}")
            break

    # ── Summary ────────────────────────────────────────────────────────────────
    print(f"\n{'='*62}")
    print(f"  Loop finished. Journal: {JOURNAL_PATH}")
    all_rounds = load_journal()
    if all_rounds:
        rates = [r.get("metric", {}).get("acceptance_rate", 0) for r in all_rounds]
        print(f"  Best:   {max(rates):.1%}")
        print(f"  Rounds: {len(all_rounds)}")
    print(f"{'='*62}\n")


if __name__ == "__main__":
    main()
