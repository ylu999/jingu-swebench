#!/usr/bin/env python3
"""
tail-logs.py — Real-time CloudWatch log tail for ECS tasks.

Polls log stream every 5s, prints new lines immediately.
Exits automatically when task STOPPED + stream exhausted.
Filters dockerd/containerd noise by default.

Usage:
  python scripts/tail-logs.py <task-id>
  python scripts/tail-logs.py <task-id> --filter '[cp-step]|[control-plane]'
  python scripts/tail-logs.py <task-id> --all          # no filtering
  python scripts/tail-logs.py <task-id> --interval 10  # poll every 10s
"""
from __future__ import annotations

import argparse
import re
import sys
import time

import boto3

REGION = "us-west-2"
LOG_GROUP = "/ecs/jingu-swebench"
ECS_CLUSTER = "jingu-swebench"

# Lines matching these patterns are noise — skip by default
NOISE_PATTERNS = re.compile(r'^time=".*?level=|^\s*$')

# Lines that indicate early failure worth highlighting
ALERT_PATTERNS = re.compile(
    r'ERROR|FAILED|Traceback|Exception|ModuleNotFoundError|'
    r'ImportError|FileNotFoundError|CRITICAL|preflight.*FAIL|'
    r'\[jingu\] ERROR'
)


def get_task_status(ecs, task_id: str) -> str:
    try:
        resp = ecs.describe_tasks(cluster=ECS_CLUSTER, tasks=[task_id])
        tasks = resp.get("tasks", [])
        if not tasks:
            return "NOT_FOUND"
        t = tasks[0]
        status = t.get("lastStatus", "UNKNOWN")
        if status == "STOPPED":
            exit_code = t.get("containers", [{}])[0].get("exitCode", "?")
            reason = t.get("stoppedReason", "")
            return f"STOPPED exit={exit_code} reason={reason}"
        return status
    except Exception as e:
        return f"ERROR: {e}"


def tail(task_id: str, filter_pat: str | None, show_all: bool, interval: int) -> None:
    logs = boto3.client("logs", region_name=REGION)
    ecs = boto3.client("ecs", region_name=REGION)
    log_stream = f"runner/runner/{task_id}"

    print(f"[tail] task={task_id}", flush=True)
    print(f"[tail] stream={LOG_GROUP}/{log_stream}", flush=True)
    print(f"[tail] filter={'none (--all)' if show_all else filter_pat or 'no-noise'}", flush=True)
    print(f"[tail] polling every {interval}s — exits when task STOPPED + stream exhausted", flush=True)
    print("─" * 70, flush=True)

    compiled_filter = re.compile(filter_pat) if filter_pat else None
    next_token: str | None = None
    stream_wait_start = time.monotonic()
    stream_available = False
    last_event_ts = 0

    while True:
        # ── Fetch log events ──────────────────────────────────────────────
        try:
            kwargs: dict = dict(
                logGroupName=LOG_GROUP,
                logStreamName=log_stream,
                startFromHead=True,
                limit=500,
            )
            if next_token:
                kwargs["nextToken"] = next_token

            resp = logs.get_log_events(**kwargs)
            stream_available = True

        except logs.exceptions.ResourceNotFoundException:
            elapsed = time.monotonic() - stream_wait_start
            task_status = get_task_status(ecs, task_id)

            if "STOPPED" in task_status:
                print(f"\n[tail] task {task_status} before log stream appeared — early failure", flush=True)
                sys.exit(1)

            if elapsed > 180:
                print(f"\n[tail] log stream not available after 3 min (task={task_status}) — giving up", flush=True)
                sys.exit(1)

            print(f"\r[tail] waiting for log stream... {elapsed:.0f}s (task={task_status})    ", end="", flush=True)
            time.sleep(interval)
            continue

        # ── Print new events ──────────────────────────────────────────────
        events = resp.get("events", [])
        new_events = [e for e in events if e["timestamp"] > last_event_ts]

        for ev in new_events:
            msg = ev["message"]
            last_event_ts = ev["timestamp"]

            # Apply filters
            if not show_all:
                if NOISE_PATTERNS.match(msg):
                    continue
                if compiled_filter and not compiled_filter.search(msg):
                    continue

            # Highlight alerts
            if ALERT_PATTERNS.search(msg):
                print(f"⚠️  {msg}", flush=True)
            else:
                print(msg, flush=True)

        new_token = resp.get("nextForwardToken")

        # ── Exit condition ────────────────────────────────────────────────
        # Stream exhausted (token didn't advance) → check task status
        if new_token == next_token and stream_available:
            task_status = get_task_status(ecs, task_id)
            if "STOPPED" in task_status:
                print(f"\n[tail] task {task_status} — stream exhausted, done", flush=True)
                break

        next_token = new_token
        time.sleep(interval)

    print("─" * 70, flush=True)


def main():
    parser = argparse.ArgumentParser(description="Real-time CloudWatch log tail for ECS tasks")
    parser.add_argument("task_id", help="ECS task ID")
    parser.add_argument("--filter", "-f", default=None,
                        help="Regex filter (default: suppress dockerd noise). E.g. '[cp-step]|[control-plane]'")
    parser.add_argument("--all", "-a", action="store_true",
                        help="Show all lines including dockerd/containerd noise")
    parser.add_argument("--interval", "-i", type=int, default=5,
                        help="Poll interval in seconds (default: 5)")
    args = parser.parse_args()

    try:
        tail(args.task_id, args.filter, args.all, args.interval)
    except KeyboardInterrupt:
        print("\n[tail] interrupted", flush=True)


if __name__ == "__main__":
    main()
