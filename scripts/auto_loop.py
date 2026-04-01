#!/usr/bin/env python3
"""
AutoResearch-style self-improving loop for jingu-swebench.

Architecture (three-file pattern):
  program.md              — fixed goals, rules, constraints (never modified)
  run_with_jingu_gate.py  — the file the loop can modify (config, strategy, prompts)
  auto_loop.py            — this file (orchestrator + context memory)

The loop:
  1. Run a batch of instances via run_with_jingu_gate.py
  2. Analyze results: acceptance rate, failure patterns, per-instance breakdown
  3. Call Claude to generate a hypothesis + concrete code change
  4. Apply the change to run_with_jingu_gate.py
  5. If metric improves → git commit; if not → git reset
  6. Write loop_journal.jsonl with full context for next iteration
  7. Repeat until budget exhausted or target reached

Usage:
  python scripts/auto_loop.py
  python scripts/auto_loop.py --instances django__django-11039 django__django-11001
  python scripts/auto_loop.py --max-rounds 10 --workers 4
  python scripts/auto_loop.py --dry-run        # plan only, no execution

Environment:
  AWS_DEFAULT_REGION or AWS_PROFILE for Bedrock access
  LOOP_TARGET_PASS_RATE (default: 0.5 = 50%)
"""

import argparse
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

SCRIPT_DIR   = Path(__file__).parent
REPO_ROOT    = SCRIPT_DIR.parent
TARGET_SCRIPT = SCRIPT_DIR / "run_with_jingu_gate.py"
PROGRAM_MD   = SCRIPT_DIR / "program.md"
JOURNAL_PATH = REPO_ROOT / "results" / "loop_journal.jsonl"

# Immutable eval: we measure this but never modify the metric definition
EVAL_METRIC  = "acceptance_rate"   # accepted/total

# Bedrock model for the hypothesis generator
CLAUDE_MODEL  = "us.anthropic.claude-sonnet-4-6-0"
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
    """Compact summary of past rounds for LLM context."""
    if not rounds:
        return "No previous rounds."
    lines = []
    for r in rounds[-8:]:  # last 8 rounds to fit context
        rn = r.get("round", "?")
        metric = r.get("metric", {})
        accepted = metric.get("accepted", 0)
        total    = metric.get("total", 0)
        rate     = metric.get("acceptance_rate", 0.0)
        hyp      = r.get("hypothesis", "")[:120]
        change   = r.get("change_summary", "")[:120]
        commit   = r.get("committed", False)
        lines.append(
            f"Round {rn}: {accepted}/{total} ({rate:.1%}) | "
            f"committed={commit} | hyp='{hyp}' | change='{change}'"
        )
    return "\n".join(lines)

# ── Runner ─────────────────────────────────────────────────────────────────────

def run_batch(instances: list[str], output_dir: Path, workers: int, max_attempts: int, stagger: int) -> dict:
    """Run run_with_jingu_gate.py on the given instances. Returns parsed results."""
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

    print(f"  [loop] running {len(instances)} instances → {output_dir}")
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

    # Parse log for per-instance failure reasons
    failures: dict[str, list[str]] = {iid: [] for iid in instances}
    if log_path.exists():
        log_text = log_path.read_text()
        for iid in instances:
            # Look for gate failure lines after each instance mention
            pattern = rf"\[jingu\] {re.escape(iid)}.*?(?=\[jingu\] |\Z)"
            matches = re.findall(pattern, log_text, re.DOTALL)
            for m in matches:
                for code in ["EMPTY_PATCH", "PARSE_FAILED", "UNGROUNDED_PATCH",
                             "PATCH_APPLY_FAILED", "TESTS_NOT_IMPROVED"]:
                    if code in m:
                        failures[iid].append(code)

    accepted_ids = list(preds.keys())
    total = len(instances)
    accepted = len(accepted_ids)

    # Failure pattern counts
    fail_counts: dict[str, int] = {}
    for codes in failures.values():
        for code in codes:
            fail_counts[code] = fail_counts.get(code, 0) + 1

    # Patch stats for accepted instances
    patch_lines = [len(preds[iid]["model_patch"].splitlines()) for iid in accepted_ids]
    avg_lines = sum(patch_lines) / len(patch_lines) if patch_lines else 0.0

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

# ── Hypothesis generator (Claude via Bedrock) ──────────────────────────────────

SYSTEM_PROMPT = """\
You are an autonomous research agent optimizing a software patch generation system (jingu-swebench).

Your job: analyze experiment results and propose ONE concrete, testable improvement.

Rules:
1. You may ONLY suggest changes to the file `run_with_jingu_gate.py`.
2. You must NOT suggest changes to the eval metric or the loop itself.
3. Each hypothesis must be falsifiable: state what metric you expect to improve and by how much.
4. Prefer small, targeted changes. One variable at a time.
5. Your output must be valid JSON (no markdown, no prose outside JSON).

Output format:
{
  "hypothesis": "one sentence describing what you believe and why",
  "expected_improvement": "e.g. acceptance_rate +5pp by reducing PARSE_FAILED",
  "change_summary": "short description of the code change",
  "target_section": "BASE_CONFIG | retry_hint | jingu_structural_check | other:<name>",
  "diff": "unified diff of the exact change to apply to run_with_jingu_gate.py"
}
"""


def call_claude(user_message: str) -> str:
    """Call Claude via Bedrock. Returns raw response text."""
    client = boto3.client("bedrock-runtime", region_name=CLAUDE_REGION)
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 2048,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_message}],
    }
    resp = client.invoke_model(
        modelId=CLAUDE_MODEL,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    data = json.loads(resp["body"].read())
    return data["content"][0]["text"]


def build_hypothesis_prompt(
    round_num: int,
    current_metric: dict,
    past_rounds: list[dict],
    current_code: str,
    instances: list[str],
    program_goals: str,
) -> str:
    past_summary = summarize_journal(past_rounds)

    per_instance_lines = []
    for iid, info in current_metric.get("per_instance", {}).items():
        status = "✓" if info["accepted"] else "✗"
        codes = ",".join(info["fail_codes"]) if info["fail_codes"] else "no_failure_logged"
        per_instance_lines.append(f"  {status} {iid}: fail_codes=[{codes}] patch_lines={info['patch_lines']}")

    return f"""
=== AutoResearch Loop: Round {round_num} ===

PROGRAM GOALS (fixed, never modify):
{program_goals}

CURRENT METRIC (round {round_num}):
  acceptance_rate: {current_metric['acceptance_rate']:.1%}  ({current_metric['accepted']}/{current_metric['total']})
  avg_patch_lines: {current_metric['avg_patch_lines']:.1f}
  fail_counts: {json.dumps(current_metric['fail_counts'])}

PER-INSTANCE RESULTS:
{chr(10).join(per_instance_lines)}

PAST ROUNDS (most recent first):
{past_summary}

CURRENT run_with_jingu_gate.py (the file you may modify):
```python
{current_code}
```

INSTANCES BEING TESTED:
{', '.join(instances)}

Based on the failure patterns and past round history, propose the single most promising change.
Remember: output ONLY valid JSON matching the format in your system prompt.
""".strip()


def parse_hypothesis_response(raw: str) -> dict | None:
    """Extract JSON from Claude response, tolerating markdown fences."""
    raw = raw.strip()
    # Strip markdown code fences
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"```\s*$", "", raw, flags=re.MULTILINE)
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError:
        # Try to find JSON object in the response
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return None

# ── Patch application ──────────────────────────────────────────────────────────

def apply_diff(diff_text: str, target: Path) -> bool:
    """Apply a unified diff to the target file. Returns True if successful."""
    if not diff_text or not diff_text.strip():
        print("  [loop] empty diff — nothing to apply")
        return False

    # Write diff to temp file
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".patch", delete=False) as f:
        f.write(diff_text)
        patch_file = f.name

    try:
        result = subprocess.run(
            ["patch", "--forward", "--strip=0", str(target)],
            input=diff_text,
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        if result.returncode == 0:
            print(f"  [loop] patch applied successfully")
            return True
        else:
            print(f"  [loop] patch failed: {result.stderr.strip()[:300]}")
            return False
    finally:
        Path(patch_file).unlink(missing_ok=True)


def git_commit(message: str) -> bool:
    """Stage run_with_jingu_gate.py and commit."""
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
    """Reset run_with_jingu_gate.py to HEAD (undo failed change)."""
    subprocess.run(
        ["git", "checkout", "HEAD", "--", str(TARGET_SCRIPT)],
        cwd=str(REPO_ROOT), check=True
    )
    print(f"  [loop] reset {TARGET_SCRIPT.name} to HEAD")

# ── Main loop ──────────────────────────────────────────────────────────────────

def load_program_goals() -> str:
    if PROGRAM_MD.exists():
        return PROGRAM_MD.read_text()
    return textwrap.dedent("""
    GOAL: Maximize acceptance_rate on SWE-bench instances.
    METRIC: acceptance_rate = accepted / total (higher is better)
    TARGET: >= 50% acceptance rate
    CONSTRAINT: patches must pass structural gate + apply gate
    """).strip()


def main():
    parser = argparse.ArgumentParser(description="AutoResearch loop for jingu-swebench")
    parser.add_argument("--instances", nargs="+", default=DEFAULT_INSTANCES,
                        help="Instance IDs to test each round")
    parser.add_argument("--max-rounds", type=int, default=20,
                        help="Maximum rounds (default: 20)")
    parser.add_argument("--workers", type=int, default=4,
                        help="Parallel workers per round (default: 4)")
    parser.add_argument("--max-attempts", type=int, default=3,
                        help="Agent attempts per instance per round (default: 3)")
    parser.add_argument("--stagger", type=int, default=20,
                        help="Stagger between workers in seconds (default: 20)")
    parser.add_argument("--target", type=float,
                        default=float(os.environ.get("LOOP_TARGET_PASS_RATE", "0.5")),
                        help="Stop when acceptance_rate >= this (default: 0.5)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run round 0, generate hypothesis, but don't apply change")
    args = parser.parse_args()

    print(f"""
╔══════════════════════════════════════════════════════╗
║  jingu-swebench AutoResearch Loop                    ║
║  instances={len(args.instances)}  max_rounds={args.max_rounds}  target={args.target:.0%}    ║
╚══════════════════════════════════════════════════════╝
""")

    program_goals = load_program_goals()
    past_rounds = load_journal()
    round_num = len(past_rounds) + 1

    # If journal has rounds, show recent history
    if past_rounds:
        print(f"  [loop] resuming from round {round_num} ({len(past_rounds)} past rounds)")
        last = past_rounds[-1]
        last_rate = last.get("metric", {}).get("acceptance_rate", 0)
        print(f"  [loop] last round: acceptance_rate={last_rate:.1%}")
        print()

    for _ in range(args.max_rounds):
        print(f"\n{'='*60}")
        print(f"  ROUND {round_num}  [{datetime.now().strftime('%H:%M:%S')}]")
        print(f"{'='*60}")

        out_dir = REPO_ROOT / "results" / f"loop_round_{round_num:03d}"

        # ── Step 1: Run batch ──────────────────────────────────────────────────
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

        # ── Check target ───────────────────────────────────────────────────────
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

        # ── Step 2: Generate hypothesis ────────────────────────────────────────
        print(f"\n  [loop] calling Claude for hypothesis...")
        current_code = TARGET_SCRIPT.read_text()

        prompt = build_hypothesis_prompt(
            round_num=round_num,
            current_metric=metric,
            past_rounds=past_rounds,
            current_code=current_code,
            instances=args.instances,
            program_goals=program_goals,
        )

        try:
            raw_response = call_claude(prompt)
            hypothesis = parse_hypothesis_response(raw_response)
        except Exception as e:
            print(f"  [loop] Claude call failed: {e}")
            hypothesis = None

        if not hypothesis:
            print(f"  [loop] no valid hypothesis — writing journal entry and continuing")
            append_journal({
                "round": round_num,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metric": metric,
                "hypothesis": "HYPOTHESIS_FAILED",
                "change_summary": "Claude returned invalid JSON",
                "committed": False,
            })
            round_num += 1
            past_rounds = load_journal()
            continue

        print(f"\n  HYPOTHESIS: {hypothesis.get('hypothesis', '')}")
        print(f"  EXPECTED:   {hypothesis.get('expected_improvement', '')}")
        print(f"  CHANGE:     {hypothesis.get('change_summary', '')}")
        print(f"  TARGET:     {hypothesis.get('target_section', '')}")

        if args.dry_run:
            print(f"\n  [dry-run] not applying change")
            append_journal({
                "round": round_num,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metric": metric,
                "hypothesis": hypothesis.get("hypothesis", ""),
                "expected_improvement": hypothesis.get("expected_improvement", ""),
                "change_summary": hypothesis.get("change_summary", ""),
                "target_section": hypothesis.get("target_section", ""),
                "committed": False,
                "note": "dry-run: change not applied",
            })
            print(f"\n  [dry-run] done. Journal written.")
            break

        # ── Step 3: Apply change ───────────────────────────────────────────────
        diff = hypothesis.get("diff", "")
        applied = apply_diff(diff, TARGET_SCRIPT) if diff else False

        if not applied:
            print(f"  [loop] patch not applied — recording hypothesis only")
            append_journal({
                "round": round_num,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metric": metric,
                "hypothesis": hypothesis.get("hypothesis", ""),
                "expected_improvement": hypothesis.get("expected_improvement", ""),
                "change_summary": hypothesis.get("change_summary", ""),
                "diff": diff[:500] if diff else "",
                "committed": False,
                "note": "diff apply failed",
            })
            round_num += 1
            past_rounds = load_journal()
            continue

        # ── Step 4: Evaluate new code ──────────────────────────────────────────
        print(f"\n  [loop] evaluating new code...")
        out_dir_new = REPO_ROOT / "results" / f"loop_round_{round_num:03d}_new"
        metric_new = run_batch(
            instances=args.instances,
            output_dir=out_dir_new,
            workers=args.workers,
            max_attempts=args.max_attempts,
            stagger=args.stagger,
        )
        rate_new = metric_new["acceptance_rate"]
        improved = rate_new > rate

        print(f"\n  BEFORE: {rate:.1%}   AFTER: {rate_new:.1%}   {'↑ IMPROVED' if improved else '↓ NO IMPROVEMENT'}")

        # ── Step 5: Commit or rollback ─────────────────────────────────────────
        committed = False
        if improved:
            msg = (
                f"experiment(loop-r{round_num}): {hypothesis.get('change_summary', '')[:60]}\n\n"
                f"Before: {rate:.1%}  After: {rate_new:.1%}\n"
                f"Hypothesis: {hypothesis.get('hypothesis', '')[:120]}"
            )
            committed = git_commit(msg)
        else:
            git_reset_target()

        # ── Step 6: Write journal ──────────────────────────────────────────────
        entry = {
            "round": round_num,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metric": metric,
            "metric_after": metric_new,
            "delta": round(rate_new - rate, 4),
            "improved": improved,
            "hypothesis": hypothesis.get("hypothesis", ""),
            "expected_improvement": hypothesis.get("expected_improvement", ""),
            "change_summary": hypothesis.get("change_summary", ""),
            "target_section": hypothesis.get("target_section", ""),
            "diff": diff[:2000],  # truncate for journal
            "committed": committed,
        }
        append_journal(entry)

        # Update for next round
        if improved:
            print(f"  [loop] change committed. New baseline: {rate_new:.1%}")
        else:
            print(f"  [loop] rolled back. Baseline unchanged: {rate:.1%}")

        round_num += 1
        past_rounds = load_journal()

        # Check target again after applying change
        final_rate = rate_new if improved else rate
        if final_rate >= args.target:
            print(f"\n  TARGET REACHED after round {round_num-1}: {final_rate:.1%}")
            break

    print(f"\n{'='*60}")
    print(f"  Loop finished. Journal: {JOURNAL_PATH}")
    print(f"  Rounds completed: {round_num - len(load_journal()) + len(load_journal()) - 1}")

    # Final summary
    all_rounds = load_journal()
    if all_rounds:
        rates = [r.get("metric", {}).get("acceptance_rate", 0) for r in all_rounds]
        print(f"  Best acceptance_rate: {max(rates):.1%}")
        print(f"  Latest:              {rates[-1]:.1%}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
