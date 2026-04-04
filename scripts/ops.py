#!/usr/bin/env python3
"""
ops.py — jingu-swebench operational script.

Subcommands:
  build       Build + push Docker image to ECR via SSM on EC2
  run         Launch ECS batch task (no tailing)
  smoke       Launch ECS task + live-tail ALL instance logs in real time
  logs        Tail a single ECS task log (CloudWatch)
  status      Show ECS task status

Usage:
  python scripts/ops.py build
  python scripts/ops.py smoke --batch-name b5-smoke --instance-ids django__django-11039 django__django-12470
  python scripts/ops.py run --instance-ids django__django-11039 --batch-name b2-test --workers 3
  python scripts/ops.py logs --task-id <ecs_task_id> --follow
  python scripts/ops.py status --task-id <ecs_task_id>

Environment:
  AWS_DEFAULT_REGION  (default: us-west-2)
"""

import argparse
import json
import re
import sys
import threading
import time

import boto3

# ── Constants ──────────────────────────────────────────────────────────────────

REGION = "us-west-2"
ASG_NAME = "jingu-swebench-ecs-asg"
ECR_IMAGE = "235494812052.dkr.ecr.us-west-2.amazonaws.com/jingu-swebench:latest"
ECS_CLUSTER = "jingu-swebench"
ECS_TASK_DEF = "jingu-swebench-runner"
LOG_GROUP = "/ecs/jingu-swebench"
S3_BUCKET = "jingu-swebench-results"

# ── Helpers ────────────────────────────────────────────────────────────────────

def get_running_instance_id() -> str | None:
    ec2 = boto3.client("ec2", region_name=REGION)
    resp = ec2.describe_instances(
        Filters=[
            {"Name": "tag:aws:autoscaling:groupName", "Values": [ASG_NAME]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )
    reservations = resp.get("Reservations", [])
    if not reservations or not reservations[0]["Instances"]:
        return None
    return reservations[0]["Instances"][0]["InstanceId"]


def scale_asg(desired: int) -> None:
    asg = boto3.client("autoscaling", region_name=REGION)
    asg.update_auto_scaling_group(
        AutoScalingGroupName=ASG_NAME,
        MinSize=0,
        DesiredCapacity=desired,
    )
    print(f"[ops] ASG desired={desired}")


def wait_for_instance(timeout_s: int = 90) -> str:
    print("[ops] waiting for EC2 instance...", end="", flush=True)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        iid = get_running_instance_id()
        if iid:
            print(f" {iid}")
            return iid
        print(".", end="", flush=True)
        time.sleep(5)
    raise TimeoutError("EC2 instance did not start in time")


# ── build ──────────────────────────────────────────────────────────────────────

BUILD_SCRIPT = """#!/bin/bash
set -e

if ! command -v git &>/dev/null; then
  yum install -y git -q
fi

cd /root

if [ -d jingu-swebench ]; then
  cd jingu-swebench && git pull -q && cd ..
else
  git clone https://github.com/ylu999/jingu-swebench.git -q
fi

cd jingu-swebench

if [ ! -d jingu-trust-gate/node_modules ]; then
  docker run --rm -v $(pwd)/jingu-trust-gate:/work -w /work node:18-alpine npm install --silent 2>/dev/null
fi

aws ecr get-login-password --region us-west-2 | docker login --username AWS --password-stdin 235494812052.dkr.ecr.us-west-2.amazonaws.com 2>/dev/null

GIT_COMMIT=$(git rev-parse HEAD)
BUILD_TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
echo "Building commit $GIT_COMMIT at $BUILD_TIMESTAMP"

docker build \\
  --build-arg GIT_COMMIT=$GIT_COMMIT \\
  --build-arg BUILD_TIMESTAMP=$BUILD_TIMESTAMP \\
  -t jingu-swebench:latest . 2>&1 | grep -E "^Step|error|ERROR|Successfully" | tail -20

docker tag jingu-swebench:latest 235494812052.dkr.ecr.us-west-2.amazonaws.com/jingu-swebench:latest
docker push 235494812052.dkr.ecr.us-west-2.amazonaws.com/jingu-swebench:latest 2>&1 | tail -3
echo "BUILD_DONE commit=$GIT_COMMIT ts=$BUILD_TIMESTAMP"
"""


def cmd_build(args) -> None:
    print("[ops] build: scale up ASG...")
    scale_asg(1)
    iid = wait_for_instance()

    # Wait for SSM agent to be ready
    ssm = boto3.client("ssm", region_name=REGION)
    print("[ops] waiting for SSM agent...", end="", flush=True)
    for _ in range(12):
        try:
            resp = ssm.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": [iid]}]
            )
            if resp["InstanceInformationList"]:
                print(" ready")
                break
        except Exception:
            pass
        print(".", end="", flush=True)
        time.sleep(5)
    else:
        print(" timeout — proceeding anyway")

    print(f"[ops] sending build script to {iid}...")
    resp = ssm.send_command(
        InstanceIds=[iid],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [BUILD_SCRIPT]},
        TimeoutSeconds=900,
    )
    cmd_id = resp["Command"]["CommandId"]
    print(f"[ops] SSM command: {cmd_id}")

    print("[ops] building", end="", flush=True)
    for i in range(90):
        time.sleep(10)
        r = ssm.get_command_invocation(CommandId=cmd_id, InstanceId=iid)
        status = r["Status"]
        if i % 3 == 0:
            print(".", end="", flush=True)
        if status not in ("Pending", "InProgress"):
            print(f" {status}")
            out = r.get("StandardOutputContent", "")
            # Show last meaningful lines
            lines = [l for l in out.splitlines() if l.strip()]
            for line in lines[-15:]:
                print(f"  {line}")
            if r.get("StandardErrorContent", "").strip():
                # Filter out docker login warnings
                errs = [l for l in r["StandardErrorContent"].splitlines()
                        if l.strip() and "WARNING" not in l and "npm notice" not in l]
                if errs:
                    print("[ops] stderr:", "\n".join(errs[-5:]))
            if status != "Success":
                print("[ops] BUILD FAILED")
                sys.exit(1)
            break

    # Verify ECR push
    ecr = boto3.client("ecr", region_name=REGION)
    images = ecr.describe_images(repositoryName="jingu-swebench")["imageDetails"]
    latest = sorted(images, key=lambda x: x["imagePushedAt"])[-1]
    print(f"[ops] ECR latest: pushed={latest['imagePushedAt']} digest={latest['imageDigest'][:20]}...")

    if not args.keep_instance:
        print("[ops] scaling down ASG...")
        scale_asg(0)


# ── run ────────────────────────────────────────────────────────────────────────

def cmd_run(args) -> None:
    ecs = boto3.client("ecs", region_name=REGION)

    instance_ids_str = " ".join(args.instance_ids)
    batch_name = args.batch_name
    output_path = f"/app/results/{batch_name}"

    cmd_parts = [
        "--instance-ids", *args.instance_ids,
        "--mode", args.mode,
        "--max-attempts", str(args.max_attempts),
        "--workers", str(args.workers),
        "--output", output_path,
    ]
    # Note: s3 upload is handled by docker-entrypoint.sh via S3_BUCKET env var

    print(f"[ops] launching ECS task: {batch_name}")
    print(f"[ops] instances: {instance_ids_str}")
    print(f"[ops] mode={args.mode} attempts={args.max_attempts} workers={args.workers}")

    resp = ecs.run_task(
        cluster=ECS_CLUSTER,
        taskDefinition=ECS_TASK_DEF,
        launchType="EC2",
        overrides={
            "containerOverrides": [{
                "name": "runner",
                "command": cmd_parts,
                "environment": [
                    {"name": "BATCH_NAME", "value": batch_name},
                ],
            }]
        },
    )

    failures = resp.get("failures", [])
    if failures:
        print(f"[ops] FAILED to launch: {failures}")
        sys.exit(1)

    task = resp["tasks"][0]
    task_arn = task["taskArn"]
    task_id = task_arn.split("/")[-1]
    print(f"[ops] ECS task launched: {task_id}")
    print(f"[ops] logs: python scripts/ops.py logs --task-id {task_id}")
    print(f"[ops] status: python scripts/ops.py status --task-id {task_id}")


# ── logs ───────────────────────────────────────────────────────────────────────

def cmd_logs(args) -> None:
    logs_client = boto3.client("logs", region_name=REGION)
    ecs = boto3.client("ecs", region_name=REGION)
    task_id = args.task_id
    log_stream = f"runner/runner/{task_id}"

    print(f"[ops] tailing logs: {LOG_GROUP}/{log_stream}")
    next_token = None
    last_event_time = 0
    wait_deadline = time.monotonic() + 120  # give up waiting for stream after 2 min

    while True:
        # Check if task already stopped — fail fast instead of waiting forever
        if not args.follow:
            try:
                resp = ecs.describe_tasks(cluster=ECS_CLUSTER, tasks=[task_id])
                tasks = resp.get("tasks", [])
                if tasks and tasks[0].get("lastStatus") == "STOPPED":
                    print(f"[ops] task is STOPPED (exit={tasks[0].get('containers', [{}])[0].get('exitCode', '?')})")
            except Exception:
                pass

        kwargs = {
            "logGroupName": LOG_GROUP,
            "logStreamName": log_stream,
            "startFromHead": True,
        }
        if next_token:
            kwargs["nextToken"] = next_token

        try:
            resp = logs_client.get_log_events(**kwargs)
        except logs_client.exceptions.ResourceNotFoundException:
            if time.monotonic() > wait_deadline:
                print("[ops] log stream not available after 2 min — task may have failed to start")
                sys.exit(1)
            print("[ops] log stream not yet available, waiting...", end="\r", flush=True)
            time.sleep(10)
            continue

        events = resp.get("events", [])
        for ev in events:
            if ev["timestamp"] > last_event_time:
                print(ev["message"])
                last_event_time = ev["timestamp"]

        new_token = resp.get("nextForwardToken")

        if not args.follow:
            break

        # In follow mode: stop if task is STOPPED and we've drained all events
        if new_token == next_token:
            try:
                t_resp = ecs.describe_tasks(cluster=ECS_CLUSTER, tasks=[task_id])
                tasks = t_resp.get("tasks", [])
                if tasks and tasks[0].get("lastStatus") == "STOPPED":
                    print("[ops] task STOPPED — log stream exhausted")
                    break
            except Exception:
                pass

        next_token = new_token
        time.sleep(5)


# ── smoke ──────────────────────────────────────────────────────────────────────
#
# All instances share ONE CloudWatch log stream: runner/runner/<task-id>
# Lines are interleaved by timestamp. We tail once and color-code by instance_id.

# Lines from dockerd/containerd/dataset-download that add no information
_NOISE_RE = re.compile(
    r'^time=".*?level='          # dockerd structured log
    r'|Generating \w+ split:'    # HuggingFace dataset progress
    r'|^\s*$'                    # blank lines
)

# Lines worth highlighting as alerts
_ALERT_RE = re.compile(
    r'ERROR|FAILED|Traceback|Exception|ModuleNotFoundError|'
    r'ImportError|FileNotFoundError|CRITICAL|\[jingu\] ERROR'
)

# Default filter: lines that carry actual signal (LLM calls, CP, agent output, errors)
# Suppresses docker pull / minisweagent DEBUG / LiteLLM noise
_DEFAULT_FILTER = re.compile(
    r'\[jingu\]|\[cp-step\]|\[control-plane\]|\[agent\]|'
    r'\[step \d+\]|'                          # agent step output (LLM conversation)
    r'\[attempt |'                            # attempt headers
    r'\[inner-verify\]|\[controlled-verify\]|'
    r'STOPPING|verdict|pee:|task_success|'
    r'ERROR|FAILED|Traceback|\[preflight\]|\[init\]'
)

_COLORS = [
    "\033[36m",   # cyan       — instance 0
    "\033[33m",   # yellow     — instance 1
    "\033[35m",   # magenta    — instance 2
    "\033[32m",   # green      — instance 3
    "\033[34m",   # blue       — instance 4
    "\033[91m",   # bright red — instance 5
    "\033[96m",   # bright cyan
    "\033[93m",   # bright yellow
]
_RESET = "\033[0m"


def _color_for_instance(instance_id: str, instance_ids: list[str]) -> str:
    try:
        idx = instance_ids.index(instance_id)
    except ValueError:
        idx = hash(instance_id) % len(_COLORS)
    return _COLORS[idx % len(_COLORS)]


def _tail_shared_stream(
    task_id: str,
    instance_ids: list[str],
    filter_pat: re.Pattern,
) -> None:
    """
    Tail the single shared log stream for this ECS task.
    Color-code each line by the instance_id it belongs to (extracted from log content).
    One thread, one stream — no duplication.
    """
    logs = boto3.client("logs", region_name=REGION)
    ecs = boto3.client("ecs", region_name=REGION)
    log_stream = f"runner/runner/{task_id}"

    # Pre-build color map
    color_map = {iid: _color_for_instance(iid, instance_ids) for iid in instance_ids}
    current_color = _COLORS[0]  # track which instance is "active" for un-tagged lines

    # Patterns that carry instance identity — used to switch active color context
    # 1. cp-step/control-plane: instance=<id>
    # 2. agent start:           [agent] running <id> attempt=
    # 3. attempt header:        [attempt N/M] <id>
    # 4. jingu start:           [jingu] START <id>
    _iid_re = re.compile(
        r'instance=([\w_\-\.]+)'           # cp-step / control-plane
        r'|\[agent\] running ([\w_\-\.]+)' # agent start line
        r'|\[attempt \d+/\d+\] ([\w_\-\.]+)'  # attempt header
        r'|\[jingu\] START ([\w_\-\.]+)'   # jingu start
    )

    next_token: str | None = None
    last_ts = 0
    stream_wait_start = time.monotonic()
    stream_available = False

    while True:
        try:
            kwargs: dict = dict(
                logGroupName=LOG_GROUP,
                logStreamName=log_stream,
                startFromHead=True,
                limit=1000,
            )
            if next_token:
                kwargs["nextToken"] = next_token
            resp = logs.get_log_events(**kwargs)
            stream_available = True
        except logs.exceptions.ResourceNotFoundException:
            elapsed = time.monotonic() - stream_wait_start
            try:
                t = ecs.describe_tasks(cluster=ECS_CLUSTER, tasks=[task_id])
                status = (t.get("tasks") or [{}])[0].get("lastStatus", "")
                if status == "STOPPED":
                    print("[smoke] task STOPPED before log stream appeared — early failure", flush=True)
                    return
            except Exception:
                pass
            if elapsed > 180:
                print("[smoke] log stream not available after 3 min", flush=True)
                return
            print(f"\r[smoke] waiting for log stream... {elapsed:.0f}s    ", end="", flush=True)
            time.sleep(5)
            continue

        events = resp.get("events", [])
        new_events = [e for e in events if e["timestamp"] > last_ts]

        for ev in new_events:
            msg = ev["message"]
            last_ts = ev["timestamp"]

            # Always drop pure noise
            if _NOISE_RE.search(msg):
                continue

            # Apply signal filter
            if not filter_pat.search(msg):
                continue

            # Detect which instance this line belongs to, update active color
            m = _iid_re.search(msg)
            if m:
                # Take the first non-None capture group
                iid = next((g for g in m.groups() if g), None)
                if iid:
                    current_color = color_map.get(iid, _COLORS[0])

            alert = "⚠️  " if _ALERT_RE.search(msg) else ""
            print(f"{current_color}{alert}{msg}{_RESET}", flush=True)

        new_token = resp.get("nextForwardToken")

        # Exit when stream exhausted + task stopped
        if new_token == next_token and stream_available:
            try:
                t = ecs.describe_tasks(cluster=ECS_CLUSTER, tasks=[task_id])
                status = (t.get("tasks") or [{}])[0].get("lastStatus", "")
                if status == "STOPPED":
                    print("[smoke] task STOPPED — stream exhausted, done", flush=True)
                    return
            except Exception:
                pass

        next_token = new_token
        time.sleep(3)


def _get_running_tasks() -> list[dict]:
    """Return list of currently RUNNING/PENDING tasks in the cluster with metadata."""
    ecs = boto3.client("ecs", region_name=REGION)
    result = []
    for desired in ("RUNNING", "PENDING"):
        paginator = ecs.get_paginator("list_tasks")
        for page in paginator.paginate(cluster=ECS_CLUSTER, desiredStatus=desired):
            arns = page.get("taskArns", [])
            if not arns:
                continue
            desc = ecs.describe_tasks(cluster=ECS_CLUSTER, tasks=arns)
            for t in desc.get("tasks", []):
                task_id = t["taskArn"].split("/")[-1]
                started = t.get("startedAt", t.get("createdAt", "?"))
                batch = ""
                for ov in t.get("overrides", {}).get("containerOverrides", []):
                    for env in ov.get("environment", []):
                        if env["name"] == "BATCH_NAME":
                            batch = env["value"]
                result.append({
                    "task_id": task_id,
                    "status": t.get("lastStatus", "?"),
                    "batch": batch,
                    "started": str(started),
                })
    return result


def cmd_list_tasks(args) -> None:
    """List currently RUNNING/PENDING ECS tasks."""
    tasks = _get_running_tasks()
    if not tasks:
        print("[ops] no running/pending tasks", flush=True)
        return
    print(f"{'TASK ID':<44} {'STATUS':<10} {'BATCH':<30} STARTED", flush=True)
    print("─" * 110, flush=True)
    for t in tasks:
        print(f"{t['task_id']:<44} {t['status']:<10} {t['batch']:<30} {t['started']}", flush=True)


def cmd_smoke(args) -> None:
    """
    Launch ECS task + live-tail logs. Or tail an existing task with --task-id.

    Two modes:
      Launch + tail:  --batch-name NAME --instance-ids ID [ID ...]
      Tail existing:  --task-id TASK_ID  (no launch, just attach)

    Single stream, color-coded by instance_id found in log lines.
    Default filter: [jingu] [cp-step] [control-plane] [agent] errors pee:
    Use --filter to narrow, --all for everything.
    """
    ecs = boto3.client("ecs", region_name=REGION)

    # ── Determine filter ──────────────────────────────────────────────────────
    if args.all:
        filter_pat = re.compile(r'.')
        filter_desc = "all lines"
    elif args.filter:
        filter_pat = re.compile(args.filter)
        filter_desc = f"filter: {args.filter}"
    else:
        filter_pat = _DEFAULT_FILTER
        filter_desc = "default (signal lines only)"

    # ── Mode: attach to existing task ─────────────────────────────────────────
    if args.task_id:
        task_id = args.task_id
        # Discover instance_ids from task override command args
        instance_ids: list[str] = args.instance_ids or []
        if not instance_ids:
            # Try to extract from task description
            try:
                t_resp = ecs.describe_tasks(cluster=ECS_CLUSTER, tasks=[task_id])
                tasks_list = t_resp.get("tasks", [])
                if tasks_list:
                    for ov in tasks_list[0].get("overrides", {}).get("containerOverrides", []):
                        cmd = ov.get("command", [])
                        if "--instance-ids" in cmd:
                            idx = cmd.index("--instance-ids") + 1
                            while idx < len(cmd) and not cmd[idx].startswith("--"):
                                instance_ids.append(cmd[idx])
                                idx += 1
            except Exception:
                pass
        status_str = ""
        try:
            t_resp = ecs.describe_tasks(cluster=ECS_CLUSTER, tasks=[task_id])
            t = (t_resp.get("tasks") or [{}])[0]
            status_str = t.get("lastStatus", "?")
        except Exception:
            pass
        print(f"[smoke] attaching to task={task_id}  status={status_str}", flush=True)
        print(f"[smoke] instances={instance_ids or '(unknown)'}", flush=True)
        print(f"[smoke] {filter_desc}", flush=True)
        print("─" * 70, flush=True)
        try:
            _tail_shared_stream(task_id, instance_ids, filter_pat)
        except KeyboardInterrupt:
            print("\n[smoke] interrupted", flush=True)
        print("─" * 70, flush=True)
        return

    # ── Mode: launch new task ─────────────────────────────────────────────────
    if not args.batch_name or not args.instance_ids:
        print("[smoke] ERROR: --batch-name and --instance-ids required when launching a new task", flush=True)
        print("        To tail an existing task: --task-id TASK_ID", flush=True)
        sys.exit(1)

    batch_name = args.batch_name
    instance_ids = args.instance_ids

    # Check for already-running tasks — warn user before launching duplicate
    running = _get_running_tasks()
    if running:
        print(f"[smoke] WARNING: {len(running)} task(s) already running:", flush=True)
        for t in running:
            print(f"  task={t['task_id']}  batch={t['batch'] or '(no batch)'}  status={t['status']}", flush=True)
        print("[smoke] To tail an existing task instead:  python scripts/ops.py smoke --task-id TASK_ID", flush=True)
        answer = input("[smoke] Launch a NEW task anyway? [y/N] ").strip().lower()
        if answer != "y":
            print("[smoke] aborted", flush=True)
            sys.exit(0)

    output_path = f"/app/results/{batch_name}"
    cmd_parts = [
        "--instance-ids", *instance_ids,
        "--mode", args.mode,
        "--max-attempts", str(args.max_attempts),
        "--workers", str(args.workers),
        "--output", output_path,
    ]

    print(f"[smoke] batch={batch_name}  instances={len(instance_ids)}", flush=True)
    for i, iid in enumerate(instance_ids):
        color = _COLORS[i % len(_COLORS)]
        print(f"  {color}●{_RESET} {iid}", flush=True)
    print(f"[smoke] mode={args.mode} attempts={args.max_attempts} workers={args.workers}", flush=True)
    print(f"[smoke] {filter_desc}", flush=True)

    resp = ecs.run_task(
        cluster=ECS_CLUSTER,
        taskDefinition=ECS_TASK_DEF,
        launchType="EC2",
        overrides={
            "containerOverrides": [{
                "name": "runner",
                "command": cmd_parts,
                "environment": [
                    {"name": "BATCH_NAME", "value": batch_name},
                ],
            }]
        },
    )
    failures = resp.get("failures", [])
    if failures:
        print(f"[smoke] FAILED to launch: {failures}", flush=True)
        sys.exit(1)

    task_arn = resp["tasks"][0]["taskArn"]
    task_id = task_arn.split("/")[-1]
    print(f"[smoke] ECS task: {task_id}", flush=True)

    # Wait for RUNNING (up to 3 min)
    print("[smoke] waiting for RUNNING...", end="", flush=True)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        t_resp = ecs.describe_tasks(cluster=ECS_CLUSTER, tasks=[task_id])
        tasks_list = t_resp.get("tasks", [])
        if tasks_list:
            status = tasks_list[0].get("lastStatus", "")
            if status == "RUNNING":
                print(" RUNNING", flush=True)
                break
            if status == "STOPPED":
                exit_code = tasks_list[0].get("containers", [{}])[0].get("exitCode", "?")
                reason = tasks_list[0].get("stoppedReason", "")
                print(f"\n[smoke] task STOPPED early (exit={exit_code} reason={reason})", flush=True)
                sys.exit(1)
        print(".", end="", flush=True)
        time.sleep(5)
    else:
        print("\n[smoke] timeout waiting for RUNNING", flush=True)
        sys.exit(1)

    print("─" * 70, flush=True)
    try:
        _tail_shared_stream(task_id, instance_ids, filter_pat)
    except KeyboardInterrupt:
        print("\n[smoke] interrupted", flush=True)
    print("─" * 70, flush=True)

    try:
        t_resp = ecs.describe_tasks(cluster=ECS_CLUSTER, tasks=[task_id])
        tasks_list = t_resp.get("tasks", [])
        if tasks_list:
            s = tasks_list[0]
            exit_code = s.get("containers", [{}])[0].get("exitCode", "?")
            print(f"[smoke] final status={s.get('lastStatus')} exit={exit_code}", flush=True)
    except Exception:
        pass


# ── status ─────────────────────────────────────────────────────────────────────

def cmd_status(args) -> None:
    ecs = boto3.client("ecs", region_name=REGION)
    task_id = args.task_id

    resp = ecs.describe_tasks(cluster=ECS_CLUSTER, tasks=[task_id])
    tasks = resp.get("tasks", [])
    if not tasks:
        print(f"[ops] task {task_id} not found")
        return

    task = tasks[0]
    print(f"status: {task['lastStatus']}")
    print(f"desired: {task['desiredStatus']}")
    print(f"started: {task.get('startedAt', 'not started')}")
    print(f"stopped: {task.get('stoppedAt', 'not stopped')}")
    if task.get("stopCode"):
        print(f"stopCode: {task['stopCode']}")
    if task.get("stoppedReason"):
        print(f"reason: {task['stoppedReason']}")
    for c in task.get("containers", []):
        print(f"container {c['name']}: {c.get('lastStatus')} exit={c.get('exitCode', '?')}")


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="jingu-swebench ops")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # build
    p_build = sub.add_parser("build", help="Build + push Docker image")
    p_build.add_argument("--keep-instance", action="store_true",
                         help="Don't scale down ASG after build")

    # run
    p_run = sub.add_parser("run", help="Launch ECS batch (no log tailing)")
    p_run.add_argument("--instance-ids", nargs="+", required=True)
    p_run.add_argument("--batch-name", required=True)
    p_run.add_argument("--mode", default="jingu", choices=["jingu", "baseline"])
    p_run.add_argument("--max-attempts", type=int, default=2)
    p_run.add_argument("--workers", type=int, default=3)
    p_run.add_argument("--s3-upload", action="store_true", default=True)

    # smoke — launch + live tail, OR attach to existing task
    p_smoke = sub.add_parser(
        "smoke",
        help="Launch ECS task + live-tail logs. Or attach to existing: --task-id TASK_ID",
    )
    p_smoke.add_argument("--task-id", default=None,
                         help="Attach to an already-running task (no launch)")
    p_smoke.add_argument("--instance-ids", nargs="+", default=None,
                         help="Instance IDs to run (required when launching)")
    p_smoke.add_argument("--batch-name", default=None,
                         help="Batch name (required when launching)")
    p_smoke.add_argument("--mode", default="jingu", choices=["jingu", "baseline"])
    p_smoke.add_argument("--max-attempts", type=int, default=2)
    p_smoke.add_argument("--workers", type=int, default=3)
    p_smoke.add_argument("--filter", "-f", default=None,
                         help="Regex filter (overrides default). E.g. 'cp-step|control-plane|pee'")
    p_smoke.add_argument("--all", "-a", action="store_true",
                         help="Show all lines including noise (no filter)")

    # list-tasks — show currently running/pending ECS tasks
    sub.add_parser("list-tasks", help="List currently RUNNING/PENDING ECS tasks")

    # logs
    p_logs = sub.add_parser("logs", help="Tail ECS task logs")
    p_logs.add_argument("--task-id", required=True)
    p_logs.add_argument("--follow", "-f", action="store_true")

    # status
    p_status = sub.add_parser("status", help="ECS task status")
    p_status.add_argument("--task-id", required=True)

    args = parser.parse_args()

    if args.cmd == "build":
        cmd_build(args)
    elif args.cmd == "run":
        cmd_run(args)
    elif args.cmd == "smoke":
        cmd_smoke(args)
    elif args.cmd == "list-tasks":
        cmd_list_tasks(args)
    elif args.cmd == "logs":
        cmd_logs(args)
    elif args.cmd == "status":
        cmd_status(args)


if __name__ == "__main__":
    main()
