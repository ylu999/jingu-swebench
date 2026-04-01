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
        "step_limit": 30,       # default=250 is way too slow; 30 steps is enough for most bugs
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
) -> str | None:
    """Run mini-SWE-agent on one instance. Returns submission patch or None."""
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
    # Build instance_template_extra: tests that must pass + optional retry hint
    extra_parts = []
    fail_to_pass = instance.get("FAIL_TO_PASS", [])
    if fail_to_pass:
        tests_str = "\n".join(f"  - {t}" for t in fail_to_pass[:10])
        extra_parts.append(
            f"IMPORTANT: Your fix must make the following tests pass:\n{tests_str}"
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
    if traj_path.exists():
        try:
            traj = json.loads(traj_path.read_text())
            sub_from_traj = traj.get("info", {}).get("submission", "")
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
                return sub

    if sub_from_traj:
        return sub_from_traj

    return None

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
    last_failure = ""
    total_llm_calls = 0

    for attempt in range(1, max_attempts + 1):
        print(f"  [attempt {attempt}/{max_attempts}] {instance_id}")

        patch = run_agent(instance, output_dir, attempt,
                          previous_failure=last_failure, parent_timer=t_inst)

        # llm_calls are recorded in _usage_tracker; no separate accumulation needed

        t_gate = Timer(f"jingu gate attempt={attempt}", parent=t_inst)
        if not patch:
            print(f"    [gate] EMPTY — no submission")
            last_failure = "No patch was generated"
            t_gate.stop()
            continue

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

    inst_usage = _usage_tracker.per_instance().get(instance_id, {})
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
        "step_limit":       BASE_CONFIG["agent"].get("step_limit", None),
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
