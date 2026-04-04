#!/usr/bin/env python3
"""
analyze_cp_behavior.py — Control-plane behavior distribution analysis.

Reads CloudWatch logs for a batch task and produces a structured report:
  - Verdict distribution (VerdictStop/Continue/Redirect/Advance)
  - no_progress_steps terminal value distribution
  - weak_progress coverage (% steps with weak signal)
  - attempt count distribution
  - per-instance summary table

Usage:
  python scripts/analyze_cp_behavior.py --task-id <ecs-task-id>
  python scripts/analyze_cp_behavior.py --log-file <local-log.txt>
  python scripts/analyze_cp_behavior.py --task-id <id> --save logs/b4-obs.txt
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path


# ── Log fetching ──────────────────────────────────────────────────────────────

def fetch_logs(task_id: str, save_path: str | None = None) -> list[str]:
    import boto3
    logs_client = boto3.client("logs", region_name="us-west-2")
    log_group = "/ecs/jingu-swebench"
    log_stream = f"runner/runner/{task_id}"

    print(f"[analyze] fetching logs: {log_group}/{log_stream}", flush=True)
    lines = []
    next_token = None
    while True:
        kwargs = dict(
            logGroupName=log_group,
            logStreamName=log_stream,
            startFromHead=True,
            limit=10000,
        )
        if next_token:
            kwargs["nextToken"] = next_token
        resp = logs_client.get_log_events(**kwargs)
        for ev in resp["events"]:
            lines.append(ev["message"])
        new_token = resp.get("nextForwardToken")
        if new_token == next_token:
            break
        next_token = new_token

    print(f"[analyze] fetched {len(lines)} log lines", flush=True)
    if save_path:
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        Path(save_path).write_text("\n".join(lines))
        print(f"[analyze] saved to {save_path}", flush=True)
    return lines


def load_log_file(path: str) -> list[str]:
    return Path(path).read_text().splitlines()


# ── Parsing ───────────────────────────────────────────────────────────────────

# [cp-step] instance=django__django-11039 attempt=1 signals=['patch'] no_progress:0 step:31 env_noise:False actionability:1 weak_progress:True
RE_CP_STEP = re.compile(
    r"\[cp-step\] instance=(\S+) attempt=(\d+)"
    r".*?no_progress:(\d+) step:(\d+)"
    r".*?weak_progress:(True|False)"
)

# [control-plane] instance=django-11039 attempt=1 state=phase:OBSERVE step:254 no_progress:1 task_success:True
RE_CP_STATE = re.compile(
    r"\[control-plane\] instance=(\S+) attempt=(\d+) state=phase:(\S+) step:(\d+) no_progress:(\d+) task_success:(True|False)"
)

# [control-plane] instance=django-11039 attempt=1 verdict=VerdictStop(type='STOP', reason='task_success')
RE_CP_VERDICT = re.compile(
    r"\[control-plane\] instance=(\S+) attempt=(\d+) verdict=(Verdict\w+)\("
)

# [control-plane] instance=django-11039 STOPPING — reason=task_success
RE_CP_STOP = re.compile(
    r"\[control-plane\] instance=(\S+) STOPPING — reason=(\S+)"
)


def parse_logs(lines: list[str]) -> dict:
    """
    Returns per-instance data:
      {
        instance_id: {
          attempts: {
            1: {
              cp_steps: [{no_progress, step, weak_progress}, ...],
              verdict: "VerdictStop" | "VerdictContinue" | ...,
              verdict_reason: "task_success" | "no_signal" | None,
              final_no_progress: int,
              final_step: int,
              task_success: bool,
            }
          }
        }
      }
    """
    data: dict = defaultdict(lambda: {"attempts": defaultdict(lambda: {
        "cp_steps": [],
        "verdict": None,
        "verdict_reason": None,
        "final_no_progress": 0,
        "final_step": 0,
        "task_success": False,
    })})

    for line in lines:
        # cp-step
        m = RE_CP_STEP.search(line)
        if m:
            iid, attempt, no_prog, step, weak = m.groups()
            # normalize instance id (may have short form)
            iid = _normalize_iid(iid)
            data[iid]["attempts"][int(attempt)]["cp_steps"].append({
                "no_progress": int(no_prog),
                "step": int(step),
                "weak_progress": weak == "True",
            })
            continue

        # cp state (boundary)
        m = RE_CP_STATE.search(line)
        if m:
            iid, attempt, phase, step, no_prog, ts = m.groups()
            iid = _normalize_iid(iid)
            att = data[iid]["attempts"][int(attempt)]
            att["final_no_progress"] = int(no_prog)
            att["final_step"] = int(step)
            att["task_success"] = ts == "True"
            continue

        # verdict
        m = RE_CP_VERDICT.search(line)
        if m:
            iid, attempt, verdict_type = m.groups()
            iid = _normalize_iid(iid)
            data[iid]["attempts"][int(attempt)]["verdict"] = verdict_type
            # extract reason if present
            reason_m = re.search(r"reason='(\w+)'", line)
            if reason_m:
                data[iid]["attempts"][int(attempt)]["verdict_reason"] = reason_m.group(1)
            continue

    return dict(data)


def _normalize_iid(iid: str) -> str:
    """Handle both django__django-XXXXX and django-XXXXX forms."""
    if "__" not in iid:
        return f"django__{iid}"
    return iid


# ── Analysis ──────────────────────────────────────────────────────────────────

def analyze(data: dict) -> dict:
    verdict_dist: dict[str, int] = defaultdict(int)
    no_progress_dist: dict[int, int] = defaultdict(int)
    attempt_dist: dict[int, int] = defaultdict(int)
    weak_progress_steps = 0
    total_steps = 0
    success_count = 0
    instance_summaries = []

    for iid, idata in sorted(data.items()):
        attempts = idata["attempts"]
        max_attempt = max(attempts.keys()) if attempts else 0
        attempt_dist[max_attempt] += 1

        instance_success = False
        for att_num in sorted(attempts.keys()):
            att = attempts[att_num]
            verdict = att["verdict"] or "no_verdict"
            verdict_dist[verdict] += 1

            final_np = att["final_no_progress"]
            no_progress_dist[final_np] += 1

            cp_steps = att["cp_steps"]
            for s in cp_steps:
                total_steps += 1
                if s["weak_progress"]:
                    weak_progress_steps += 1

            if att["task_success"]:
                instance_success = True

        if instance_success:
            success_count += 1

        # per-instance row
        last_att = attempts.get(max_attempt, {})
        instance_summaries.append({
            "instance_id": iid,
            "attempts": max_attempt,
            "total_steps": sum(len(a["cp_steps"]) for a in attempts.values()),
            "final_no_progress": last_att.get("final_no_progress", "?"),
            "last_verdict": last_att.get("verdict", "no_verdict"),
            "task_success": last_att.get("task_success", False),
        })

    return {
        "n_instances": len(data),
        "success_count": success_count,
        "success_rate": f"{100*success_count/max(len(data),1):.1f}%",
        "verdict_distribution": dict(verdict_dist),
        "no_progress_terminal_distribution": dict(no_progress_dist),
        "attempt_distribution": dict(attempt_dist),
        "weak_progress_pct": f"{100*weak_progress_steps/max(total_steps,1):.1f}%" if total_steps else "n/a",
        "total_cp_steps_observed": total_steps,
        "instance_summaries": instance_summaries,
    }


def print_report(result: dict) -> None:
    print("\n" + "="*60)
    print("CONTROL-PLANE BEHAVIOR REPORT")
    print("="*60)
    print(f"Instances:      {result['n_instances']}")
    print(f"Success:        {result['success_count']} ({result['success_rate']})")
    print(f"CP steps seen:  {result['total_cp_steps_observed']}")
    print(f"weak_progress:  {result['weak_progress_pct']} of steps")

    print("\n-- Verdict Distribution --")
    for v, count in sorted(result["verdict_distribution"].items(), key=lambda x: -x[1]):
        print(f"  {v:<30} {count}")

    print("\n-- no_progress terminal value --")
    for np, count in sorted(result["no_progress_terminal_distribution"].items()):
        bar = "#" * count
        print(f"  no_progress={np:<3}  {count:>3}x  {bar}")

    print("\n-- Attempt Count Distribution --")
    for att, count in sorted(result["attempt_distribution"].items()):
        print(f"  attempts={att}  {count}x")

    print("\n-- Per-Instance Summary --")
    header = f"{'instance_id':<35} {'att':>3} {'steps':>5} {'np':>3} {'verdict':<20} {'success'}"
    print(header)
    print("-" * len(header))
    for row in result["instance_summaries"]:
        print(
            f"{row['instance_id']:<35} {row['attempts']:>3} {row['total_steps']:>5}"
            f" {str(row['final_no_progress']):>3} {str(row['last_verdict']):<20} {row['task_success']}"
        )
    print("="*60)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Analyze control-plane behavior from ECS logs")
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--task-id", help="ECS task ID (fetches from CloudWatch)")
    grp.add_argument("--log-file", help="Local log file path")
    parser.add_argument("--save", help="Save fetched logs to this file path")
    parser.add_argument("--json", action="store_true", help="Also dump raw JSON result")
    args = parser.parse_args()

    if args.task_id:
        lines = fetch_logs(args.task_id, save_path=args.save)
    else:
        lines = load_log_file(args.log_file)

    data = parse_logs(lines)
    if not data:
        print("[analyze] No control-plane data found in logs.")
        print("  (Check that logs contain [cp-step] and [control-plane] lines)")
        sys.exit(1)

    result = analyze(data)
    print_report(result)

    if args.json:
        print("\n-- Raw JSON --")
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
