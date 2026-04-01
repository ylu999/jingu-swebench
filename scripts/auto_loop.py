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
            f"acceptance_rate={m.get('acceptance_rate', 0):.1%} "
            f"({m.get('accepted', 0)}/{m.get('total', 0)}) "
            f"fail_counts={json.dumps(m.get('fail_counts', {}))}"
        )
        if "metric_after" in last:
            ma = last["metric_after"]
            last_metric_str += (
                f"\n  After change: acceptance_rate={ma.get('acceptance_rate', 0):.1%} "
                f"({ma.get('accepted', 0)}/{ma.get('total', 0)})"
            )

    return f"""# AutoResearch Loop — Round {round_num}

You are the autonomous optimization agent for jingu-swebench.
Your goal is to improve the acceptance_rate on SWE-bench instances.

---

## System Architecture

## Four-Layer Architecture

```
Laptop (control plane)         ← you are here; auto_loop.py + claude --print
   ↓ ssh cloud
Cloud Dev Desktop               ← execution plane; 1.2TB disk, 8 CPUs
   ↓ docker run
Docker container                ← fast feedback: git apply + pytest (~30s/instance)
   ↓ after patch is good
sb-cli submit                   ← ground truth / leaderboard judge
```

**CRITICAL DISTINCTION:**
- Docker pytest = fast iteration signal (seconds, NOT ground truth)
- `swebench.harness.run_evaluation` = official Docker harness (minutes, closer to truth)
- `sb-cli submit` = final leaderboard judge (do not substitute)

## Cloud Desktop Commands

```bash
# Refresh AWS credentials (needed if Bedrock calls fail):
ssh cloud "~/.toolbox/bin/ada credentials update --account=235494812052 --provider=conduit --role=IibsAdminAccess-DO-NOT-DELETE --once"

# Python:
ssh cloud "~/.local/share/mise/shims/python ..."

# Generate patches (mini-SWE-agent + Jingu gate):
ssh cloud "~/.local/share/mise/shims/python ~/jingu-swebench/scripts/run_with_jingu_gate.py \
  --instance-ids django__django-11039 \
  --max-attempts 3 --workers 4 --output ~/jingu-swebench/results/run_X/"

# Fast feedback: apply patch + run tests in Docker (~30s):
ssh cloud "docker run --rm -v /tmp/patch.diff:/tmp/patch.diff -w /testbed \
  sweb.eval.x86_64.django__django-11039:latest bash -c \
  'git apply /tmp/patch.diff && python -m pytest tests/migrations/ -x -q 2>&1 | tail -20'"

# Official harness eval (use sparingly, ~2min/instance):
ssh cloud "~/.local/share/mise/shims/python -m swebench.harness.run_evaluation \
  --dataset_name SWE-bench/SWE-bench_Lite \
  --predictions_path ~/jingu-swebench/results/run_X/jingu-predictions.jsonl \
  --instance_ids django__django-11039 --max_workers 4 --run_id test_X"

# Available Docker images (built locally, not from registry):
# sweb.eval.x86_64.django__django-11039:latest  (and 11001, 11019, 11049, 11099)
# minisweagent naming: swebench/sweb.eval.x86_64.django_1776_django-NNNNN:latest
```

## Reference Docs (read these if needed)
- `{DOCS_DIR}/README.md` — quick reference + prediction format
- `{DOCS_DIR}/evaluation.md` — harness evaluation commands
- `{DOCS_DIR}/datasets.md` — dataset structure, FAIL_TO_PASS semantics
- `{DOCS_DIR}/harness_reference.md` — full parameter reference
- `{DOCS_DIR}/docker_setup.md` — Docker setup and caching

---

## Program Goals (FIXED — never modify)

{program_goals}

---

## Round History (most recent last)

{round_history}

## Last Known Metric
{last_metric_str}

## Instances Being Tested
{", ".join(instances)}

---

## Current run_with_jingu_gate.py

```python
{current_code}
```

---

## Your Task for Round {round_num}

You have FULL AUTONOMY to do whatever is needed to improve the acceptance_rate.

You CAN:
- Read the round history above and identify the dominant failure pattern
- SSH to cloud desktop (`ssh cloud`) to investigate, run tests, check Docker logs
- Run a dry-run or full eval batch on cloud desktop
- Read any log file, check environment state, debug root causes
- Modify `{TARGET_SCRIPT}` (the ONLY code file you may change)
- Fetch documentation from the web if needed
- Read the docs in `{DOCS_DIR}/`

You MUST NOT:
- Modify `auto_loop.py`, `program.md`, or `compare_groups.py`
- Make compound changes (ONE change at a time)
- Modify the acceptance_rate metric definition

## Workflow

1. Analyze — study round history, identify the highest-impact failure mode
2. Investigate — if you need more data, SSH to cloud and look at logs/outputs
3. Hypothesize — form ONE testable hypothesis
4. Implement — make ONE targeted change to `run_with_jingu_gate.py`
5. Record — write your result to `{REPO_ROOT}/results/loop_round_{round_num:03d}_result.json`

## Result Format

After completing your work, write this exact JSON to:
`{REPO_ROOT}/results/loop_round_{round_num:03d}_result.json`

```json
{{
  "round": {round_num},
  "hypothesis": "one sentence: what you believe will improve acceptance_rate and why",
  "change_summary": "what specific change you made to run_with_jingu_gate.py (or 'none')",
  "expected_improvement": "e.g. +5pp acceptance_rate by reducing PARSE_FAILED",
  "next_steps": "what to try next if this hypothesis is wrong or if it works",
  "actions_taken": ["list", "of", "things", "you", "did"]
}}
```

If you made no change, set change_summary to "none" and explain why in hypothesis.

Go ahead — analyze the situation and take action.
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

    print(f"  [loop] invoking Claude Code (timeout={timeout_s}s)...")
    print(f"  [loop] result will be written to: {result_path.name}")

    cmd = [CLAUDE_CLI, "--print", "--no-markdown", "-p", context]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(REPO_ROOT),
        )
    except subprocess.TimeoutExpired:
        print(f"  [loop] Claude Code timed out after {timeout_s}s")
        return {"success": False, "note": "timeout", "file_changed": False}
    except FileNotFoundError:
        print(f"  [loop] ERROR: claude CLI not found at: {CLAUDE_CLI}")
        print(f"  [loop] Install: npm install -g @anthropic-ai/claude-code")
        sys.exit(1)

    hash_after   = file_hash(TARGET_SCRIPT)
    file_changed = (hash_after != hash_before)

    print(f"  [loop] Claude exit={proc.returncode}  file_changed={file_changed}")
    if proc.returncode != 0 and proc.stderr:
        print(f"  [loop] stderr: {proc.stderr[:300]}")

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
        match = re.search(r'\{[^{}]*"hypothesis"[^{}]*\}', proc.stdout, re.DOTALL)
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
        "stdout_tail": proc.stdout[-500:] if proc.stdout else "",
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

def run_local_eval(instances: list[str], output_dir: Path, workers: int,
                   max_attempts: int, stagger: int) -> dict:
    """Run run_with_jingu_gate.py locally to measure acceptance_rate."""
    import re
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "run.log"

    cmd = [
        sys.executable, str(TARGET_SCRIPT),
        "--instance-ids", *instances,
        "--output", str(output_dir),
        "--workers", str(workers),
        "--max-attempts", str(max_attempts),
        "--stagger", str(stagger),
    ]

    print(f"  [eval] running {len(instances)} instances → {output_dir.name}")
    t0 = time.time()
    with open(log_path, "w") as lf:
        subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, cwd=str(REPO_ROOT))
    print(f"  [eval] done in {time.time() - t0:.0f}s")

    return _parse_results(output_dir, instances, log_path)


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
    parser.add_argument("--claude-timeout", type=int, default=600,
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
            last_metric = run_local_eval(
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
        hash_before = file_hash(TARGET_SCRIPT)
        result      = run_claude_agent(context, round_num, args.claude_timeout)

        print(f"\n  HYPOTHESIS:   {result.get('hypothesis','')[:120]}")
        print(f"  CHANGE:       {result.get('change_summary','')[:120]}")
        print(f"  EXPECTED:     {result.get('expected_improvement','')[:120]}")
        print(f"  NEXT STEPS:   {result.get('next_steps','')[:120]}")
        if result.get("actions_taken"):
            print(f"  ACTIONS:      {result['actions_taken']}")

        file_changed = result.get("file_changed", False)

        # ── Step 4: Re-eval if file changed ───────────────────────────────────
        metric_new = None
        improved   = False
        rate_new   = 0.0
        rate_old   = last_metric.get("acceptance_rate", 0.0)

        if file_changed:
            print(f"\n  [loop] file changed — running eval to measure delta...")
            out_dir_new = REPO_ROOT / "results" / f"loop_round_{round_num:03d}_new"
            metric_new  = run_local_eval(
                args.instances, out_dir_new, args.workers, args.max_attempts, args.stagger
            )
            rate_new = metric_new["acceptance_rate"]
            improved = rate_new > rate_old
            print(f"  BEFORE: {rate_old:.1%}   AFTER: {rate_new:.1%}   {'↑ IMPROVED' if improved else '↓ NO IMPROVEMENT'}")
        else:
            print(f"  [loop] no file change — skipping re-eval")

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
