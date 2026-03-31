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
  Uses Modal (swerex_modal) for sandbox execution.
  Uses Bedrock (global.anthropic.claude-sonnet-4-5-20250929-v1:0) for LLM.
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

# ── Jingu gates ───────────────────────────────────────────────────────────────

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
    score = 1000.0 - files * 50 - changed * 2
    return score

# ── mini-SWE-agent runner (direct Python API) ─────────────────────────────────

MODEL = "bedrock/global.anthropic.claude-sonnet-4-5-20250929-v1:0"

BASE_CONFIG = {
    "model": {
        "model_class": "litellm",
        "model_name": MODEL,
        "model_kwargs": {
            "parallel_tool_calls": False,
        },
    },
    "environment": {
        "environment_class": "swerex_modal",
        "startup_timeout": 120,
        "runtime_timeout": 1800,
    },
    "agent": {
        "mode": "yolo",
        "confirm_exit": False,  # critical: don't wait for user input
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
) -> str | None:
    """Run mini-SWE-agent on one instance. Returns submission patch or None."""
    from minisweagent.run.benchmarks.swebench import process_instance
    from minisweagent.config import get_config_from_spec
    from minisweagent.utils.serialize import recursive_merge

    instance_id = instance["instance_id"]
    attempt_dir = output_dir / f"attempt_{attempt}"
    attempt_dir.mkdir(parents=True, exist_ok=True)

    # Start from swebench.yaml defaults (provides system_template, instance_template, etc.)
    config = get_config_from_spec("swebench.yaml")
    # Merge our overrides on top
    config = recursive_merge(config, BASE_CONFIG)
    # Inject retry hint if retrying
    if previous_failure:
        hint = f"Previous attempt failed: {previous_failure[:300]}"
        config = recursive_merge(config, {
            "agent": {"instance_template_extra": hint}
        })

    print(f"    [agent] running {instance_id} attempt={attempt}...")

    from minisweagent.run.benchmarks.swebench import RunBatchProgressManager

    preds_path = attempt_dir / "preds.json"
    progress = RunBatchProgressManager(num_instances=1)

    # Call process_instance (handles traj save + preds.json, confirm_exit=False in config)
    try:
        process_instance(instance, attempt_dir, config, progress)
    except Exception as e:
        print(f"    [agent] ERROR: {e}")
        traceback.print_exc()

    # Read submission from preds.json
    if preds_path.exists():
        preds = json.loads(preds_path.read_text())
        if instance_id in preds:
            sub = preds[instance_id].get("model_patch", "")
            if sub:
                return sub

    # Fallback: read from traj
    traj_path = attempt_dir / instance_id / f"{instance_id}.traj.json"
    if traj_path.exists():
        traj = json.loads(traj_path.read_text())
        sub = traj.get("info", {}).get("submission", "")
        if sub:
            return sub

    return None

# ── Main loop ─────────────────────────────────────────────────────────────────

def run_with_jingu(instance_id: str, output_dir: Path, max_attempts: int = 3) -> dict:
    """Run agent + Jingu gate with retry. Returns best result."""
    print(f"  [jingu] loading instance {instance_id}...")
    instance = _load_instance(instance_id)

    candidates = []
    last_failure = ""

    for attempt in range(1, max_attempts + 1):
        print(f"  [attempt {attempt}/{max_attempts}] {instance_id}")

        patch = run_agent(instance, output_dir, attempt, previous_failure=last_failure)

        if not patch:
            print(f"    [gate] EMPTY — no submission")
            last_failure = "No patch was generated"
            continue

        # Gate: structural check
        sg = jingu_structural_check(patch)
        if not sg["pass"]:
            print(f"    [gate] FAIL structural: {sg['code']} — {sg.get('message','')}")
            last_failure = f"Structural gate failed: {sg['message']}"
            continue

        score = score_patch(patch)
        print(f"    [gate] OK  score={score:.0f}  lines={len(patch.splitlines())}")

        candidates.append({
            "attempt": attempt,
            "patch": patch,
            "score": score,
        })

    if not candidates:
        return {
            "instance_id": instance_id,
            "accepted": False,
            "patch": "",
            "attempts": max_attempts,
        }

    best = max(candidates, key=lambda c: c["score"])
    print(f"  [result] ACCEPTED  best_attempt={best['attempt']}  score={best['score']:.0f}")
    return {
        "instance_id": instance_id,
        "accepted": True,
        "patch": best["patch"],
        "attempts": max_attempts,
        "best_attempt": best["attempt"],
        "score": best["score"],
    }

def write_predictions(results: list, output_path: Path):
    with open(output_path, "w") as f:
        for r in results:
            if r["accepted"]:
                f.write(json.dumps({
                    "instance_id": r["instance_id"],
                    "model_patch": r["patch"],
                    "model_name_or_path": "mini-swe-agent+jingu",
                }) + "\n")
    print(f"\n[predictions] written: {output_path}")
    accepted = sum(1 for r in results if r["accepted"])
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

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Pre-load all instances in a single dataset pass (avoids N redundant downloads)
    print(f"[jingu] loading {len(args.instance_ids)} instances from dataset...")
    _load_instances(args.instance_ids)
    print(f"[jingu] loaded. launching {args.workers} parallel workers...")

    results = [None] * len(args.instance_ids)

    def _run(idx: int, iid: str):
        # Stagger sandbox starts: idx=0 starts immediately, idx=1 waits stagger seconds, etc.
        # This avoids all workers hammering Modal image-pull simultaneously.
        delay = idx * args.stagger
        if delay > 0:
            print(f"[jingu] {iid} waiting {delay:.0f}s before start (stagger)")
            time.sleep(delay)
        print(f"\n[jingu] START {iid}")
        r = run_with_jingu(iid, output_dir, max_attempts=args.max_attempts)
        status = "ACCEPTED" if r["accepted"] else "FAILED"
        print(f"\n[jingu] {status} {iid}")
        return idx, r

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
                results[idx] = {"instance_id": iid, "accepted": False, "patch": "", "attempts": args.max_attempts}
            print(f"[progress] {done}/{len(args.instance_ids)} done")

    print(f"\n--- Summary ---")
    accepted = sum(1 for r in results if r and r["accepted"])
    print(f"Accepted: {accepted}/{len(results)}")

    write_predictions(results, output_dir / "jingu-predictions.jsonl")

if __name__ == "__main__":
    main()
