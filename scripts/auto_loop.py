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
  3. Call Claude (Bedrock) with the context document as a Claude Code-style agent:
     - Claude reads all context, identifies the best single improvement
     - Claude outputs: hypothesis + the complete new version of run_with_jingu_gate.py
     - This avoids fragile unified diff parsing
  4. If file content changed → re-run eval to measure delta
  5. If metric improved → git commit; if not → git reset
  6. Append to loop_journal.jsonl: metric, hypothesis, what changed, next_steps
  7. Repeat until budget exhausted or target reached

Usage:
  python scripts/auto_loop.py
  python scripts/auto_loop.py --instances django__django-11039 django__django-11001
  python scripts/auto_loop.py --max-rounds 10 --workers 4
  python scripts/auto_loop.py --dry-run        # plan only, no execution

Environment:
  AWS_DEFAULT_REGION (default: us-east-1)
  AWS_PROFILE for credential selection
  LOOP_TARGET_PASS_RATE (default: 0.6)
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

import boto3

# ── Constants ──────────────────────────────────────────────────────────────────

SCRIPT_DIR    = Path(__file__).parent
REPO_ROOT     = SCRIPT_DIR.parent
TARGET_SCRIPT = SCRIPT_DIR / "run_with_jingu_gate.py"
PROGRAM_MD    = SCRIPT_DIR / "program.md"
JOURNAL_PATH  = REPO_ROOT / "results" / "loop_journal.jsonl"
CONTEXT_PATH  = REPO_ROOT / "results" / "loop_context.md"

EVAL_METRIC   = "acceptance_rate"   # immutable

# Bedrock model
CLAUDE_MODEL  = "bedrock/global.anthropic.claude-sonnet-4-5-20250929-v1:0"
CLAUDE_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

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


def summarize_journal(rounds: list[dict]) -> str:
    """Compact summary of past rounds for Claude context."""
    if not rounds:
        return "No previous rounds."
    lines = []
    for r in rounds[-10:]:
        rn     = r.get("round", "?")
        metric = r.get("metric", {})
        rate   = metric.get("acceptance_rate", 0.0)
        acc    = metric.get("accepted", 0)
        tot    = metric.get("total", 0)
        hyp    = r.get("hypothesis", "")[:150]
        change = r.get("change_summary", "")[:150]
        nexts  = r.get("next_steps", "")[:150]
        commit = r.get("committed", False)
        delta  = f" delta={r['delta']:+.1%}" if "delta" in r else ""
        lines.append(
            f"Round {rn}: {acc}/{tot} ({rate:.1%}){delta} committed={commit}\n"
            f"  hyp: {hyp}\n"
            f"  change: {change}\n"
            f"  next_steps: {nexts}"
        )
    return "\n".join(lines)

# ── Runner ─────────────────────────────────────────────────────────────────────

def run_batch(instances: list[str], output_dir: Path, workers: int, max_attempts: int, stagger: int) -> dict:
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
    per_instance_lines = []
    for iid, info in current_metric.get("per_instance", {}).items():
        status = "ACCEPTED" if info["accepted"] else "REJECTED"
        codes  = ", ".join(info["fail_codes"]) if info["fail_codes"] else "no_failure_logged"
        per_instance_lines.append(
            f"  {status}  {iid}  fail_codes=[{codes}]  patch_lines={info['patch_lines']}"
        )

    past_summary = summarize_journal(past_rounds)

    return textwrap.dedent(f"""
    # AutoResearch Context — Round {round_num}

    ## Program Goals (FIXED — never modify these)
    {program_goals}

    ## Current Metric (Round {round_num} baseline — BEFORE your change)
    - acceptance_rate: {current_metric['acceptance_rate']:.1%}  ({current_metric['accepted']}/{current_metric['total']})
    - avg_patch_lines: {current_metric['avg_patch_lines']:.1f}
    - fail_counts: {json.dumps(current_metric['fail_counts'])}

    ## Per-Instance Results
    {chr(10).join(per_instance_lines)}

    ## Round History (last 10 rounds)
    {past_summary}

    ## Current run_with_jingu_gate.py (the ONLY file you may modify)
    ```python
    {current_code}
    ```

    ## Test Instances
    {", ".join(instances)}

    ---

    ## Your Task

    You are the hypothesis agent in an AutoResearch loop optimizing a SWE-bench patch agent.

    Step 1 — ANALYZE:
    Study the failure patterns and round history carefully.
    Identify the single most impactful improvement you can make.

    Step 2 — DECIDE:
    State your hypothesis: what change will improve acceptance_rate, and why.
    ONE change at a time. Do NOT compound multiple changes.

    Step 3 — IMPLEMENT:
    Output the complete new content of run_with_jingu_gate.py with your ONE change applied.
    Output the ENTIRE file — not a diff, not a partial snippet.

    Step 4 — SUMMARIZE:
    After the file, output exactly this JSON block:

    ```json
    {{
      "hypothesis": "one sentence: what you believe and why",
      "change_summary": "what specific thing you changed and where",
      "expected_improvement": "e.g. acceptance_rate +5pp by reducing PARSE_FAILED",
      "next_steps": "what to try next if this hypothesis is wrong"
    }}
    ```

    Constraints:
    - Modify ONLY run_with_jingu_gate.py
    - ONE change only (no compound changes)
    - Change must be reversible (loop will git reset if metric does not improve)
    - Do NOT modify the metric definition, auto_loop.py, or program.md
    - The file content you output will directly replace run_with_jingu_gate.py
    """).strip()

# ── Claude executor (Bedrock) ──────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an expert software engineering research agent optimizing a SWE-bench patch generation system.
You operate in an AutoResearch loop. Your job is to analyze experiment results and make ONE targeted
improvement to the system's strategy file.

You will receive context including: program goals, current metrics, per-instance results, round history,
and the current source code. You will output the complete modified source file followed by a JSON summary.

Critical rules:
- Output the ENTIRE file content (not a diff)
- Make exactly ONE change
- Never modify the eval metric definition
- Your output determines what runs next — be precise and complete
"""


def call_claude_bedrock(context_doc: str) -> str:
    """Call Claude via Bedrock. Returns raw response text."""
    # Try cross-region inference first, fall back to direct model ID
    model_ids_to_try = [
        CLAUDE_MODEL,
        "anthropic.claude-sonnet-4-5-20250929-v1:0",
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    ]

    client = boto3.client("bedrock-runtime", region_name=CLAUDE_REGION)

    for model_id in model_ids_to_try:
        try:
            body = {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 8192,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": context_doc}],
            }
            resp = client.invoke_model(
                modelId=model_id,
                body=json.dumps(body),
                contentType="application/json",
                accept="application/json",
            )
            data = json.loads(resp["body"].read())
            return data["content"][0]["text"]
        except client.exceptions.ValidationException as e:
            print(f"  [loop] model {model_id} failed: {e}. Trying next...")
            continue
        except Exception:
            raise

    raise RuntimeError(f"All model IDs failed: {model_ids_to_try}")


def extract_file_and_summary(raw: str, original_code: str) -> tuple[str | None, dict | None]:
    """
    Extract new file content and JSON summary from Claude response.
    Returns (new_file_content, summary_dict) or (None, None) on failure.
    """
    # Extract JSON summary (last ```json block)
    fenced_json = re.findall(r'```json\s*([\s\S]*?)```', raw)
    summary = None
    if fenced_json:
        for candidate in reversed(fenced_json):
            try:
                summary = json.loads(candidate.strip())
                break
            except json.JSONDecodeError:
                pass

    # Extract file content (largest ```python block that looks like the file)
    fenced_python = re.findall(r'```python\s*([\s\S]*?)```', raw)
    new_content = None
    if fenced_python:
        # Pick the largest block (the full file, not a snippet)
        candidates = sorted(fenced_python, key=len, reverse=True)
        for candidate in candidates:
            # Must contain key signatures from the original file
            if "run_with_jingu_gate" in candidate or "jingu_structural_check" in candidate:
                new_content = candidate.strip()
                break
        if not new_content:
            # Fall back to largest block if no signature match
            new_content = candidates[0].strip() if candidates else None

    # If no fenced block, try to extract between known markers
    if not new_content:
        # Look for content between "#!/usr/bin/env python3" and the JSON block
        match = re.search(r'(#!/usr/bin/env python3[\s\S]*?)```json', raw)
        if match:
            new_content = match.group(1).strip()

    return new_content, summary


def file_hash(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_claude_code_agent(context_doc: str) -> dict:
    """
    Call Claude Bedrock with context, extract new file + summary.
    Returns result dict.
    """
    print(f"  [loop] calling Claude (Bedrock)...")
    hash_before  = file_hash(TARGET_SCRIPT)
    original_code = TARGET_SCRIPT.read_text()

    try:
        raw = call_claude_bedrock(context_doc)
    except Exception as e:
        print(f"  [loop] Bedrock call failed: {e}")
        return {
            "success": False, "file_changed": False,
            "hypothesis": "", "change_summary": f"Bedrock error: {e}",
            "expected_improvement": "", "next_steps": "",
            "raw_output": str(e),
        }

    print(f"  [loop] Claude responded ({len(raw)} chars)")

    new_content, summary = extract_file_and_summary(raw, original_code)

    if new_content and new_content != original_code:
        # Write new file
        TARGET_SCRIPT.write_text(new_content)
        print(f"  [loop] wrote new {TARGET_SCRIPT.name} ({len(new_content)} chars)")
    else:
        if not new_content:
            print(f"  [loop] could not extract file content from Claude response")
        else:
            print(f"  [loop] Claude returned identical file content — no change")

    hash_after  = file_hash(TARGET_SCRIPT)
    changed     = (hash_after != hash_before)

    return {
        "success": bool(summary) and changed,
        "file_changed": changed,
        "hypothesis": summary.get("hypothesis", "") if summary else "",
        "change_summary": summary.get("change_summary", "") if summary else "",
        "expected_improvement": summary.get("expected_improvement", "") if summary else "",
        "next_steps": summary.get("next_steps", "") if summary else "",
        "raw_output": raw[:2000],
    }

# ── Git helpers ────────────────────────────────────────────────────────────────

def git_commit(message: str) -> bool:
    subprocess.run(["git", "add", str(TARGET_SCRIPT)], cwd=str(REPO_ROOT), capture_output=True)
    r = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(REPO_ROOT), capture_output=True, text=True
    )
    if r.returncode == 0:
        print(f"  [loop] committed: {message[:80]}")
        return True
    print(f"  [loop] commit failed: {r.stderr.strip()[:200]}")
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
    TARGET: >= 60% acceptance rate
    CONSTRAINT: patches must pass structural gate + apply gate
    """).strip()

# ── Main loop ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AutoResearch loop for jingu-swebench")
    parser.add_argument("--instances", nargs="+", default=DEFAULT_INSTANCES)
    parser.add_argument("--max-rounds", type=int, default=20)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--stagger", type=int, default=20)
    parser.add_argument("--target", type=float,
                        default=float(os.environ.get("LOOP_TARGET_PASS_RATE", "0.6")))
    parser.add_argument("--dry-run", action="store_true",
                        help="Run baseline eval + build context, but don't call Claude")
    args = parser.parse_args()

    print(f"""
╔══════════════════════════════════════════════════════╗
║  jingu-swebench AutoResearch Loop                    ║
║  instances={len(args.instances)}  max_rounds={args.max_rounds}  target={args.target:.0%}    ║
║  model: Claude via AWS Bedrock                       ║
╚══════════════════════════════════════════════════════╝
""")

    program_goals = load_program_goals()
    past_rounds   = load_journal()
    round_num     = len([r for r in past_rounds if r.get("hypothesis") != "TARGET_REACHED"]) + 1

    if past_rounds:
        print(f"  [loop] resuming from round {round_num} ({len(past_rounds)} past rounds)")
        last_rate = past_rounds[-1].get("metric", {}).get("acceptance_rate", 0)
        print(f"  [loop] last round: acceptance_rate={last_rate:.1%}\n")

    for _ in range(args.max_rounds):
        print(f"\n{'='*60}")
        print(f"  ROUND {round_num}  [{datetime.now().strftime('%H:%M:%S')}]")
        print(f"{'='*60}")

        # ── Step 1: Baseline eval ──────────────────────────────────────────────
        out_dir = REPO_ROOT / "results" / f"loop_round_{round_num:03d}_baseline"
        metric  = run_batch(
            instances=args.instances,
            output_dir=out_dir,
            workers=args.workers,
            max_attempts=args.max_attempts,
            stagger=args.stagger,
        )

        rate = metric["acceptance_rate"]
        print(f"\n  METRIC: {rate:.1%}  ({metric['accepted']}/{metric['total']})")
        print(f"  fail_counts: {metric['fail_counts']}")

        if rate >= args.target:
            print(f"\n  TARGET REACHED: {rate:.1%} >= {args.target:.1%}")
            append_journal({
                "round": round_num,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metric": metric,
                "hypothesis": "TARGET_REACHED",
                "change_summary": "none",
                "committed": False,
            })
            break

        # ── Step 2: Build context ─────────────────────────────────────────────
        current_code = TARGET_SCRIPT.read_text()
        context_doc  = build_context_doc(
            round_num=round_num,
            current_metric=metric,
            past_rounds=past_rounds,
            current_code=current_code,
            instances=args.instances,
            program_goals=program_goals,
        )

        # Save context for debugging
        CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONTEXT_PATH.write_text(context_doc)

        if args.dry_run:
            print(f"\n  [dry-run] context written to: {CONTEXT_PATH}")
            append_journal({
                "round": round_num,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metric": metric,
                "hypothesis": "DRY_RUN",
                "change_summary": "not applied",
                "committed": False,
            })
            print(f"  [dry-run] done.")
            break

        # ── Step 3: Claude Code-style agent makes the change ──────────────────
        result = run_claude_code_agent(context_doc)

        print(f"\n  HYPOTHESIS:   {result['hypothesis'][:120]}")
        print(f"  CHANGE:       {result['change_summary'][:120]}")
        print(f"  EXPECTED:     {result['expected_improvement'][:120]}")
        print(f"  NEXT STEPS:   {result['next_steps'][:120]}")

        if not result["file_changed"]:
            print(f"  [loop] no file change — skipping re-eval")
            append_journal({
                "round": round_num,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metric": metric,
                "hypothesis": result["hypothesis"],
                "change_summary": "no change made",
                "expected_improvement": result["expected_improvement"],
                "next_steps": result["next_steps"],
                "committed": False,
                "note": "Claude ran but file unchanged",
            })
            round_num  += 1
            past_rounds = load_journal()
            continue

        # ── Step 4: Re-eval with new code ─────────────────────────────────────
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

        # ── Step 5: Commit or rollback ────────────────────────────────────────
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

        # ── Step 6: Write journal ─────────────────────────────────────────────
        append_journal({
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
            "claude_output_preview": result["raw_output"][:500],
        })

        if improved:
            print(f"  [loop] committed. New baseline: {rate_new:.1%}")
        else:
            print(f"  [loop] rolled back. Baseline unchanged: {rate:.1%}")

        round_num  += 1
        past_rounds = load_journal()

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
        print(f"  Best:    {max(rates):.1%}")
        print(f"  Latest:  {rates[-1]:.1%}")
        print(f"  Rounds:  {len(all_rounds)}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
