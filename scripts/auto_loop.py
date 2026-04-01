#!/usr/bin/env python3
"""
AutoResearch loop for jingu-swebench.

Architecture (three-file pattern):
  program.md              — fixed goals, rules, constraints (never modified)
  run_with_jingu_gate.py  — the file the loop can modify (config, strategy, prompts)
  auto_loop.py            — this file (orchestrator + context memory)

How the loop works:
  1. Run a batch of instances via run_with_jingu_gate.py → get acceptance_rate
  2. Build a context document: program goals + round history + current code + metric
  3. Call `claude --print` (Claude Code CLI) with the context document
     Claude Code reads context, proposes ONE hypothesis, edits run_with_jingu_gate.py itself,
     and writes a brief summary of what it did and what to try next
  4. If run_with_jingu_gate.py changed → re-run eval to measure delta
  5. If metric improved → git commit; if not → git reset
  6. Append to loop_journal.jsonl: metric, hypothesis, what changed, next_steps
  7. Repeat until budget exhausted or target reached

Usage:
  python scripts/auto_loop.py
  python scripts/auto_loop.py --instances django__django-11039 django__django-11001
  python scripts/auto_loop.py --max-rounds 10 --workers 4
  python scripts/auto_loop.py --dry-run        # plan only, no execution

Environment:
  AWS_DEFAULT_REGION or AWS_PROFILE for Bedrock access (used by run_with_jingu_gate.py)
  CLAUDE_CLI (default: claude)     — path to claude CLI binary
  LOOP_TARGET_PASS_RATE (default: 0.5 = 50%)
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────────────

SCRIPT_DIR    = Path(__file__).parent
REPO_ROOT     = SCRIPT_DIR.parent
TARGET_SCRIPT = SCRIPT_DIR / "run_with_jingu_gate.py"
PROGRAM_MD    = SCRIPT_DIR / "program.md"
JOURNAL_PATH  = REPO_ROOT / "results" / "loop_journal.jsonl"
CONTEXT_PATH  = REPO_ROOT / "results" / "loop_context.md"  # temp context file for claude

# Immutable eval metric — never modify this definition
EVAL_METRIC   = "acceptance_rate"   # accepted/total

# claude CLI
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
    """Load all past rounds from loop_journal.jsonl."""
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


def summarize_journal(rounds: list[dict]) -> str:
    """Compact summary of past rounds for Claude context."""
    if not rounds:
        return "No previous rounds."
    lines = []
    for r in rounds[-10:]:  # last 10 rounds
        rn = r.get("round", "?")
        metric = r.get("metric", {})
        accepted = metric.get("accepted", 0)
        total    = metric.get("total", 0)
        rate     = metric.get("acceptance_rate", 0.0)
        hyp      = r.get("hypothesis", "")[:150]
        change   = r.get("change_summary", "")[:150]
        next_s   = r.get("next_steps", "")[:150]
        commit   = r.get("committed", False)
        delta_str = ""
        if "delta" in r:
            delta_str = f" delta={r['delta']:+.1%}"
        lines.append(
            f"Round {rn}: {accepted}/{total} ({rate:.1%}){delta_str} committed={commit}\n"
            f"  hyp: {hyp}\n"
            f"  change: {change}\n"
            f"  next_steps: {next_s}"
        )
    return "\n".join(lines)

# ── Runner ─────────────────────────────────────────────────────────────────────

def run_batch(instances: list[str], output_dir: Path, workers: int, max_attempts: int, stagger: int) -> dict:
    """Run run_with_jingu_gate.py on the given instances. Returns parsed metrics."""
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

    print(f"  [loop] running {len(instances)} instances → {output_dir.name}")
    t0 = time.time()
    with open(log_path, "w") as lf:
        proc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, cwd=str(REPO_ROOT))
    elapsed = time.time() - t0
    print(f"  [loop] done in {elapsed:.0f}s (exit={proc.returncode})")

    return parse_results(output_dir, instances, log_path)


def parse_results(output_dir: Path, instances: list[str], log_path: Path) -> dict:
    """Parse predictions + log into structured metrics."""
    preds_path = output_dir / "jingu-predictions.jsonl"
    preds = {}
    if preds_path.exists():
        for line in preds_path.read_text().splitlines():
            if line.strip():
                d = json.loads(line)
                preds[d["instance_id"]] = d

    # Parse log for per-instance failure codes
    failures: dict[str, list[str]] = {iid: [] for iid in instances}
    if log_path.exists():
        log_text = log_path.read_text()
        for iid in instances:
            pattern = rf"\[jingu\] {re.escape(iid)}.*?(?=\[jingu\] |\Z)"
            matches = re.findall(pattern, log_text, re.DOTALL)
            for m in matches:
                for code in ["EMPTY_PATCH", "PARSE_FAILED", "UNGROUNDED_PATCH",
                             "PATCH_APPLY_FAILED", "TESTS_NOT_IMPROVED"]:
                    if code in m:
                        failures[iid].append(code)

    accepted_ids = list(preds.keys())
    total    = len(instances)
    accepted = len(accepted_ids)

    fail_counts: dict[str, int] = {}
    for codes in failures.values():
        for code in codes:
            fail_counts[code] = fail_counts.get(code, 0) + 1

    patch_lines = [len(preds[iid]["model_patch"].splitlines()) for iid in accepted_ids]
    avg_lines   = sum(patch_lines) / len(patch_lines) if patch_lines else 0.0

    per_instance = {}
    for iid in instances:
        per_instance[iid] = {
            "accepted": iid in preds,
            "fail_codes": failures.get(iid, []),
            "patch_lines": len(preds[iid]["model_patch"].splitlines()) if iid in preds else 0,
        }

    return {
        "accepted": accepted,
        "total": total,
        "acceptance_rate": accepted / total if total > 0 else 0.0,
        "accepted_ids": accepted_ids,
        "fail_counts": fail_counts,
        "avg_patch_lines": avg_lines,
        "per_instance": per_instance,
    }

# ── Context builder ────────────────────────────────────────────────────────────

def build_context_doc(
    round_num: int,
    current_metric: dict,
    past_rounds: list[dict],
    current_code: str,
    instances: list[str],
    program_goals: str,
) -> str:
    """Build the full context document to pass to Claude Code."""
    per_instance_lines = []
    for iid, info in current_metric.get("per_instance", {}).items():
        status = "ACCEPTED" if info["accepted"] else "REJECTED"
        codes  = ", ".join(info["fail_codes"]) if info["fail_codes"] else "no_failure_logged"
        per_instance_lines.append(f"  {status}  {iid}  fail_codes=[{codes}]  patch_lines={info['patch_lines']}")

    past_summary = summarize_journal(past_rounds)

    return textwrap.dedent(f"""
    # AutoResearch Context — Round {round_num}

    ## Program Goals (FIXED — never modify)
    {program_goals}

    ## Current Metric (Round {round_num} baseline — BEFORE your change)
    - acceptance_rate: {current_metric['acceptance_rate']:.1%}  ({current_metric['accepted']}/{current_metric['total']})
    - avg_patch_lines: {current_metric['avg_patch_lines']:.1f}
    - fail_counts: {json.dumps(current_metric['fail_counts'])}

    ## Per-Instance Results
    {chr(10).join(per_instance_lines)}

    ## Round History (most recent first)
    {past_summary}

    ## Current run_with_jingu_gate.py (the ONLY file you may modify)
    ```python
    {current_code}
    ```

    ## Test Instances
    {", ".join(instances)}

    ---

    ## Your Task

    You are the hypothesis agent in an AutoResearch loop.

    Analyze the failure patterns and round history above.
    Propose ONE concrete, testable improvement and implement it NOW by editing
    `{TARGET_SCRIPT}`.

    Rules:
    1. Modify ONLY `{TARGET_SCRIPT}` — nothing else.
    2. ONE change at a time. Do not compound multiple hypotheses.
    3. The change must be reversible (the loop will git reset if metric does not improve).
    4. Do NOT modify the metric definition or the loop itself.
    5. Keep changes small and targeted.

    After making the change, output a JSON block (and nothing else after it) in this exact format:

    ```json
    {{
      "hypothesis": "one sentence: what you believe and why",
      "change_summary": "what you actually changed in the file",
      "expected_improvement": "e.g. acceptance_rate +5pp by reducing PARSE_FAILED",
      "next_steps": "what to try if this does not improve the metric"
    }}
    ```

    Make the edit now, then output the JSON.
    """).strip()

# ── Claude Code executor ───────────────────────────────────────────────────────

def file_hash(path: Path) -> str:
    """SHA256 hash of file content, for change detection."""
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def call_claude_code(context_doc: str, timeout_s: int = 300) -> dict:
    """
    Write context to a temp file and invoke the claude CLI.
    Claude Code reads the context, edits TARGET_SCRIPT, outputs JSON summary.
    Returns: { "success": bool, "hypothesis": str, "change_summary": str,
               "expected_improvement": str, "next_steps": str, "raw_output": str }
    """
    # Write context to file
    CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONTEXT_PATH.write_text(context_doc)

    hash_before = file_hash(TARGET_SCRIPT)

    # Build claude invocation
    # --print: non-interactive, output to stdout
    # -p: prompt text (we pass the context doc inline)
    prompt = f"Read the context below and perform the task described.\n\n{context_doc}"

    cmd = [CLAUDE_CLI, "--print", "--no-markdown", "-p", prompt]

    print(f"  [loop] calling claude CLI (timeout={timeout_s}s)...")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(REPO_ROOT),
        )
    except subprocess.TimeoutExpired:
        print(f"  [loop] claude CLI timed out after {timeout_s}s")
        return {"success": False, "hypothesis": "", "change_summary": "",
                "expected_improvement": "", "next_steps": "", "raw_output": "TIMEOUT"}
    except FileNotFoundError:
        print(f"  [loop] claude CLI not found: {CLAUDE_CLI}")
        print(f"  [loop] install: npm install -g @anthropic-ai/claude-code")
        return {"success": False, "hypothesis": "", "change_summary": "",
                "expected_improvement": "", "next_steps": "", "raw_output": "CLAUDE_NOT_FOUND"}

    raw = result.stdout
    hash_after = file_hash(TARGET_SCRIPT)
    changed = (hash_after != hash_before)

    print(f"  [loop] claude exit={result.returncode}  file_changed={changed}")
    if result.returncode != 0 and result.stderr:
        print(f"  [loop] stderr: {result.stderr[:300]}")

    # Extract JSON from claude output
    info = extract_json_from_output(raw)

    return {
        "success": bool(info) and changed,
        "file_changed": changed,
        "hypothesis": info.get("hypothesis", "") if info else "",
        "change_summary": info.get("change_summary", "") if info else "",
        "expected_improvement": info.get("expected_improvement", "") if info else "",
        "next_steps": info.get("next_steps", "") if info else "",
        "raw_output": raw[:3000],  # truncate for journal
    }


def extract_json_from_output(text: str) -> dict | None:
    """Extract the last JSON block from claude output."""
    # Try fenced JSON blocks first (last one wins — that's the summary)
    fenced = re.findall(r'```json\s*([\s\S]*?)```', text)
    if fenced:
        for candidate in reversed(fenced):
            try:
                return json.loads(candidate.strip())
            except json.JSONDecodeError:
                pass

    # Try bare JSON object (last occurrence)
    matches = list(re.finditer(r'\{[^{}]*"hypothesis"[^{}]*\}', text, re.DOTALL))
    if matches:
        try:
            return json.loads(matches[-1].group())
        except json.JSONDecodeError:
            pass

    return None

# ── Git helpers ────────────────────────────────────────────────────────────────

def git_commit(message: str) -> bool:
    r1 = subprocess.run(
        ["git", "add", str(TARGET_SCRIPT)],
        cwd=str(REPO_ROOT), capture_output=True
    )
    r2 = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(REPO_ROOT), capture_output=True, text=True
    )
    if r2.returncode == 0:
        print(f"  [loop] committed: {message[:80]}")
        return True
    else:
        print(f"  [loop] commit failed: {r2.stderr.strip()[:200]}")
        return False


def git_reset_target() -> None:
    subprocess.run(
        ["git", "checkout", "HEAD", "--", str(TARGET_SCRIPT)],
        cwd=str(REPO_ROOT), check=True
    )
    print(f"  [loop] reset {TARGET_SCRIPT.name} to HEAD")

# ── Program goals ──────────────────────────────────────────────────────────────

def load_program_goals() -> str:
    if PROGRAM_MD.exists():
        return PROGRAM_MD.read_text()
    return textwrap.dedent("""
    GOAL: Maximize acceptance_rate on SWE-bench instances.
    METRIC: acceptance_rate = accepted / total (higher is better)
    TARGET: >= 50% acceptance rate
    CONSTRAINT: patches must pass structural gate + apply gate
    """).strip()

# ── Main loop ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AutoResearch loop for jingu-swebench")
    parser.add_argument("--instances", nargs="+", default=DEFAULT_INSTANCES,
                        help="Instance IDs to test each round")
    parser.add_argument("--max-rounds", type=int, default=20,
                        help="Maximum rounds (default: 20)")
    parser.add_argument("--workers", type=int, default=4,
                        help="Parallel workers per round (default: 4)")
    parser.add_argument("--max-attempts", type=int, default=3,
                        help="Agent attempts per instance (default: 3)")
    parser.add_argument("--stagger", type=int, default=20,
                        help="Stagger between workers in seconds (default: 20)")
    parser.add_argument("--target", type=float,
                        default=float(os.environ.get("LOOP_TARGET_PASS_RATE", "0.6")),
                        help="Stop when acceptance_rate >= this (default: 0.6)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run baseline eval + show context, but don't invoke claude or apply change")
    parser.add_argument("--claude-timeout", type=int, default=300,
                        help="Claude CLI timeout in seconds (default: 300)")
    args = parser.parse_args()

    print(f"""
╔══════════════════════════════════════════════════════╗
║  jingu-swebench AutoResearch Loop                    ║
║  instances={len(args.instances)}  max_rounds={args.max_rounds}  target={args.target:.0%}    ║
║  executor: {CLAUDE_CLI:<42}║
╚══════════════════════════════════════════════════════╝
""")

    program_goals = load_program_goals()
    past_rounds = load_journal()
    round_num   = len([r for r in past_rounds if r.get("hypothesis") not in ("TARGET_REACHED",)]) + 1

    if past_rounds:
        print(f"  [loop] resuming from round {round_num} ({len(past_rounds)} past rounds)")
        last      = past_rounds[-1]
        last_rate = last.get("metric", {}).get("acceptance_rate", 0)
        print(f"  [loop] last round: acceptance_rate={last_rate:.1%}")
        print()

    for _ in range(args.max_rounds):
        print(f"\n{'='*60}")
        print(f"  ROUND {round_num}  [{datetime.now().strftime('%H:%M:%S')}]")
        print(f"{'='*60}")

        out_dir = REPO_ROOT / "results" / f"loop_round_{round_num:03d}_baseline"

        # ── Step 1: Baseline eval ──────────────────────────────────────────────
        metric = run_batch(
            instances=args.instances,
            output_dir=out_dir,
            workers=args.workers,
            max_attempts=args.max_attempts,
            stagger=args.stagger,
        )

        rate = metric["acceptance_rate"]
        print(f"\n  METRIC: acceptance_rate={rate:.1%}  ({metric['accepted']}/{metric['total']})")
        print(f"  fail_counts: {metric['fail_counts']}")

        # ── Check target ────────────────────────────────────────────────────────
        if rate >= args.target:
            print(f"\n  TARGET REACHED: {rate:.1%} >= {args.target:.1%}")
            append_journal({
                "round": round_num,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metric": metric,
                "hypothesis": "TARGET_REACHED",
                "change_summary": "none",
                "committed": False,
                "note": f"Loop terminated: target {args.target:.1%} reached",
            })
            break

        # ── Build context document ─────────────────────────────────────────────
        current_code = TARGET_SCRIPT.read_text()
        context_doc  = build_context_doc(
            round_num=round_num,
            current_metric=metric,
            past_rounds=past_rounds,
            current_code=current_code,
            instances=args.instances,
            program_goals=program_goals,
        )

        if args.dry_run:
            print(f"\n  [dry-run] context document written to: {CONTEXT_PATH}")
            CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONTEXT_PATH.write_text(context_doc)
            print(f"  [dry-run] context length: {len(context_doc)} chars")
            print(f"\n  [dry-run] not invoking claude. Done.")
            append_journal({
                "round": round_num,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metric": metric,
                "hypothesis": "DRY_RUN",
                "change_summary": "not applied",
                "committed": False,
                "note": "dry-run: context built, claude not invoked",
            })
            break

        # ── Step 2: Claude Code makes the change ───────────────────────────────
        print(f"\n  [loop] delegating to Claude Code...")
        result = call_claude_code(context_doc, timeout_s=args.claude_timeout)

        if result["raw_output"] == "CLAUDE_NOT_FOUND":
            print(f"\n  [loop] FATAL: claude CLI not found. Install with:")
            print(f"    npm install -g @anthropic-ai/claude-code")
            sys.exit(1)

        print(f"\n  HYPOTHESIS:   {result['hypothesis'][:120]}")
        print(f"  CHANGE:       {result['change_summary'][:120]}")
        print(f"  EXPECTED:     {result['expected_improvement'][:120]}")
        print(f"  NEXT STEPS:   {result['next_steps'][:120]}")

        if not result["file_changed"]:
            print(f"  [loop] no file change detected — skipping re-eval")
            append_journal({
                "round": round_num,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metric": metric,
                "hypothesis": result["hypothesis"],
                "change_summary": "no change made",
                "expected_improvement": result["expected_improvement"],
                "next_steps": result["next_steps"],
                "committed": False,
                "note": "claude ran but file unchanged",
                "claude_output": result["raw_output"],
            })
            round_num += 1
            past_rounds = load_journal()
            continue

        # ── Step 3: Re-eval with new code ──────────────────────────────────────
        print(f"\n  [loop] re-evaluating with new code...")
        out_dir_new = REPO_ROOT / "results" / f"loop_round_{round_num:03d}_new"
        metric_new  = run_batch(
            instances=args.instances,
            output_dir=out_dir_new,
            workers=args.workers,
            max_attempts=args.max_attempts,
            stagger=args.stagger,
        )
        rate_new = metric_new["acceptance_rate"]
        improved = rate_new > rate

        print(f"\n  BEFORE: {rate:.1%}   AFTER: {rate_new:.1%}   {'↑ IMPROVED' if improved else '↓ NO IMPROVEMENT'}")

        # ── Step 4: Commit or rollback ─────────────────────────────────────────
        committed = False
        if improved:
            msg = (
                f"experiment(loop-r{round_num}): {result['change_summary'][:60]}\n\n"
                f"Before: {rate:.1%}  After: {rate_new:.1%}\n"
                f"Hypothesis: {result['hypothesis'][:120]}"
            )
            committed = git_commit(msg)
        else:
            git_reset_target()

        # ── Step 5: Write journal ──────────────────────────────────────────────
        entry = {
            "round": round_num,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metric": metric,
            "metric_after": metric_new,
            "delta": round(rate_new - rate, 4),
            "improved": improved,
            "hypothesis": result["hypothesis"],
            "expected_improvement": result["expected_improvement"],
            "change_summary": result["change_summary"],
            "next_steps": result["next_steps"],
            "committed": committed,
            "claude_output": result["raw_output"][:1000],
        }
        append_journal(entry)

        if improved:
            print(f"  [loop] change committed. New baseline: {rate_new:.1%}")
        else:
            print(f"  [loop] rolled back. Baseline unchanged: {rate:.1%}")

        round_num   += 1
        past_rounds  = load_journal()

        final_rate = rate_new if improved else rate
        if final_rate >= args.target:
            print(f"\n  TARGET REACHED after round {round_num-1}: {final_rate:.1%}")
            break

    # ── Final summary ──────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  Loop finished. Journal: {JOURNAL_PATH}")
    all_rounds = load_journal()
    if all_rounds:
        rates = [r.get("metric", {}).get("acceptance_rate", 0) for r in all_rounds]
        print(f"  Best acceptance_rate: {max(rates):.1%}")
        print(f"  Latest:              {rates[-1]:.1%}")
        print(f"  Rounds completed:    {len(all_rounds)}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
