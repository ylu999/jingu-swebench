#!/usr/bin/env python3
"""
SWE-bench evaluation infrastructure — NOT to be modified by the auto-research loop.

Contains:
  - Timer / ModelUsage / ModelUsageTracker  — timing and cost tracking
  - _load_instances / _load_instance        — dataset loading
  - run_agent()                             — mini-SWE-agent invocation
  - write_predictions()                     — output format
  - main() parallelism harness              — CLI entry point and workers

These are stable infrastructure pieces. The auto-research loop only modifies
run_with_jingu_gate.py, which contains the jingu gate logic that imports from here.
"""

import json
import os
import re
import sys
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

# ── Dataset loading ────────────────────────────────────────────────────────────

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


# ── mini-SWE-agent runner (direct Python API) ──────────────────────────────────

def run_agent(
    instance: dict,
    output_dir: Path,
    attempt: int,
    base_config: dict,
    previous_failure: str = "",
    parent_timer: "Timer | None" = None,
) -> "str | None":
    """Run mini-SWE-agent on one instance. Returns submission patch or None.

    base_config is passed in from run_with_jingu_gate.py (the agent-modifiable part).
    """
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
    config = recursive_merge(config, base_config)
    # Build instance_template_extra: tests that must pass + optional retry hint
    extra_parts = []
    fail_to_pass = instance.get("FAIL_TO_PASS", [])
    if fail_to_pass:
        tests_str = "\n".join(f"  - {t}" for t in fail_to_pass[:10])
        extra_parts.append(
            f"IMPORTANT: Your fix must make the following tests pass:\n{tests_str}\n\n"
            f"SUBMIT IMMEDIATELY once these tests pass. Do NOT add extra tests, "
            f"demonstration scripts, or comment updates after the tests pass. "
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
    if traj_path.exists():
        try:
            traj = json.loads(traj_path.read_text())
            sub_from_traj = traj.get("info", {}).get("submission", "")
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

    if sub_from_traj_diff:
        return sub_from_traj_diff

    return None


# ── Predictions output ─────────────────────────────────────────────────────────

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


# ── Parallel harness ───────────────────────────────────────────────────────────

def run_parallel(
    instance_ids: list[str],
    output_dir: Path,
    max_attempts: int,
    workers: int,
    stagger: float,
    run_with_jingu_fn,  # callable: (instance_id, output_dir, max_attempts) -> dict
) -> list[dict]:
    """Run run_with_jingu_fn in parallel across instances with stagger."""
    results = [None] * len(instance_ids)
    preds_path = output_dir / "jingu-predictions.jsonl"

    def _run(idx: int, iid: str):
        delay = idx * stagger
        if delay > 0:
            print(f"[jingu] {iid} waiting {delay:.0f}s before start (stagger)")
            time.sleep(delay)
        print(f"\n[jingu] START {iid}")
        r = run_with_jingu_fn(iid, output_dir, max_attempts)
        status = "ACCEPTED" if r["accepted"] else "FAILED"
        print(f"\n[jingu] {status} {iid}  ({r.get('elapsed_s', 0):.1f}s)")
        return idx, r

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_run, i, iid): iid
                   for i, iid in enumerate(instance_ids)}
        done = 0
        for fut in as_completed(futures):
            done += 1
            iid = futures[fut]
            try:
                idx, r = fut.result()
                results[idx] = r
            except Exception as e:
                print(f"\n[jingu] ERROR {iid}: {e}")
                idx = instance_ids.index(iid)
                results[idx] = {"instance_id": iid, "accepted": False, "patch": "",
                                "attempts": max_attempts, "elapsed_s": 0}
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
            print(f"[progress] {done}/{len(instance_ids)} done")

    return results


# ── Run report printer ─────────────────────────────────────────────────────────

def print_run_report(
    results: list[dict],
    instance_ids: list[str],
    t_ds: Timer,
    t_parallel: Timer,
    t_write: Timer,
    output_dir: Path,
    workers: int,
    step_limit: int | None,
):
    total     = _timing_root.elapsed
    totals    = _usage_tracker.totals()
    per_inst  = _usage_tracker.per_instance()
    max_elapsed = max((r.get("elapsed_s", 0) for r in results if r), default=1)
    seq_total = sum(r.get("elapsed_s", 0) for r in results if r)
    speedup   = seq_total / t_parallel.elapsed if t_parallel.elapsed > 0 else 1

    report = {
        "instances":        len(instance_ids),
        "workers":          workers,
        "step_limit":       step_limit,
        "wall_time_s":      round(total, 1),
        "status":           "completed",
        "patches_generated": sum(1 for r in results if r and r["accepted"]),
        "model_usage": {
            "total_api_calls":    totals["api_calls"],
            "total_input_tokens": totals["input_tokens"],
            "total_output_tokens":totals["output_tokens"],
            "total_cost_usd":     totals["cost_usd"],
            "avg_calls_per_instance": round(totals["api_calls"] / len(instance_ids), 1) if instance_ids else 0,
            "avg_cost_per_instance":  round(totals["cost_usd"] / len(instance_ids), 4) if instance_ids else 0,
            "per_instance": per_inst,
        },
        "parallelism": {
            "sequential_would_be_s": round(seq_total, 1),
            "actual_wall_s":         round(t_parallel.elapsed, 1),
            "speedup_x":             round(speedup, 1),
        },
    }

    report_path = output_dir / "run_report.json"
    report_path.write_text(json.dumps(report, indent=2))

    print(f"\n{'='*62}")
    print(f"  RUN REPORT")
    print(f"{'='*62}")
    print(f"  instances={report['instances']}  workers={report['workers']}  "
          f"step_limit={step_limit}  wall={total:.1f}s")
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
