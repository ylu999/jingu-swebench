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

--- AUTO-RESEARCH LOOP BOUNDARY ---
The auto-research loop (auto_loop.py) may ONLY modify this file.
Infrastructure in swebench_infra.py is NOT to be touched.
"""

import argparse
import re
import sys
from pathlib import Path

# Infrastructure (not to be modified by auto-research loop)
from swebench_infra import (
    Timer, _timing_root, _instance_timers,
    _usage_tracker, _load_instance, _load_instances,
    run_agent, write_predictions, run_parallel, print_run_report,
)
import swebench_infra as _infra

# ── Jingu gate configuration ───────────────────────────────────────────────────
# (auto-research loop may change MODEL, BASE_CONFIG, gate logic, and scoring)

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
        "step_limit": 100,      # 60 caused LimitsExceeded on 11019 (complex Media ordering fix needs ~80-100 steps)
    },
}

# ── Jingu gates ────────────────────────────────────────────────────────────────

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
            # old_count defaults to 1 if omitted
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
            # Pad missing trailing context lines
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


# ── Main loop ──────────────────────────────────────────────────────────────────

def run_with_jingu(instance_id: str, output_dir: Path, max_attempts: int = 3) -> dict:
    """Run agent + Jingu gate with retry. Returns best result."""
    t_inst = Timer(f"instance: {instance_id}", parent=_infra._timing_root)
    _infra._instance_timers[instance_id] = t_inst

    print(f"  [jingu] loading instance {instance_id}...")
    t_load = Timer("dataset load", parent=t_inst)
    instance = _load_instance(instance_id)
    t_load.stop()

    candidates = []
    last_failure = ""

    for attempt in range(1, max_attempts + 1):
        print(f"  [attempt {attempt}/{max_attempts}] {instance_id}")

        patch = run_agent(instance, output_dir, attempt, BASE_CONFIG,
                          previous_failure=last_failure, parent_timer=t_inst)

        t_gate = Timer(f"jingu gate attempt={attempt}", parent=t_inst)
        if not patch:
            print(f"    [gate] EMPTY — no submission")
            last_failure = "No patch was generated"
            t_gate.stop()
            continue

        patch = normalize_patch(patch)
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

        candidates.append({"attempt": attempt, "patch": patch, "score": score})

        if patch_lines > 50:
            last_failure = (
                f"Previous patch was {patch_lines} lines — too large. "
                "The fix must be minimal: identify the single root cause and change only that. "
                "Do NOT add new logic, helpers, or abstractions. Target under 20 lines."
            )
        else:
            last_failure = ""

    t_inst.stop()

    inst_usage = _infra._usage_tracker.per_instance().get(instance_id, {})
    llm_calls = inst_usage.get("api_calls", 0)
    t_inst.llm_calls = llm_calls

    if not candidates:
        return {
            "instance_id": instance_id,
            "accepted": False,
            "patch": "",
            "attempts": max_attempts,
            "elapsed_s": t_inst.elapsed,
            "model_usage": inst_usage,
        }

    best = max(candidates, key=lambda c: c["score"])
    print(f"  [result] ACCEPTED  best_attempt={best['attempt']}  score={best['score']:.0f}  "
          f"elapsed={t_inst.elapsed:.1f}s  bedrock_calls={llm_calls}  "
          f"cost=${inst_usage.get('cost_usd', 0):.4f}")
    return {
        "instance_id": instance_id,
        "accepted": True,
        "patch": best["patch"],
        "attempts": max_attempts,
        "best_attempt": best["attempt"],
        "score": best["score"],
        "elapsed_s": t_inst.elapsed,
        "model_usage": inst_usage,
    }


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

    _infra._timing_root = Timer("total run")

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Pre-load all instances in a single dataset pass (avoids N redundant downloads)
    print(f"[jingu] loading {len(args.instance_ids)} instances from dataset...")
    t_ds = Timer("dataset prefetch", parent=_infra._timing_root)
    _load_instances(args.instance_ids)
    t_ds.stop()
    print(f"[jingu] loaded in {t_ds.elapsed:.1f}s. launching {args.workers} parallel workers...")

    t_parallel = Timer(f"parallel workers (×{min(args.workers, len(args.instance_ids))})", parent=_infra._timing_root)
    results = run_parallel(
        args.instance_ids, output_dir, args.max_attempts,
        args.workers, args.stagger, run_with_jingu,
    )
    t_parallel.stop()

    t_write = Timer("write predictions", parent=_infra._timing_root)
    write_predictions(results, output_dir / "jingu-predictions.jsonl")
    t_write.stop()

    _infra._timing_root.stop()

    print_run_report(
        results, args.instance_ids,
        t_ds, t_parallel, t_write,
        output_dir, args.workers,
        BASE_CONFIG["agent"].get("step_limit"),
    )


if __name__ == "__main__":
    main()
