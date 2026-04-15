#!/usr/bin/env python3
"""
ops.py — jingu-swebench operational script.

Subcommands:
  build       Build + push Docker image to ECR via SSM on EC2
  run         Launch ECS batch task (no tailing)
  smoke       Launch ECS task + live-tail ALL instance logs in real time
  watch       Real-time log tail for a batch or single instance (attach anytime)
  logs        Tail a single ECS task log (CloudWatch)
  status      Show ECS task status
  pipeline    Run full pipeline: smoke → batch → eval → store results
  history     Show pipeline run history (resolved rates)

Usage:
  python scripts/ops.py build
  python scripts/ops.py smoke --batch-name b5-smoke --instance-ids django__django-11039 django__django-12470
  python scripts/ops.py run --instance-ids django__django-11039 --batch-name b2-test --workers 3
  python scripts/ops.py watch --batch-name p12-cv-fallback
  python scripts/ops.py watch --batch-name p12-cv-fallback --instance-id django__django-11095
  python scripts/ops.py logs --task-id <ecs_task_id> --follow
  python scripts/ops.py status --task-id <ecs_task_id>
  python scripts/ops.py pipeline --batch-name p12-cv-fallback --smoke-instance django__django-11095
  python scripts/ops.py history

Environment:
  AWS_DEFAULT_REGION  (default: us-west-2)
"""

import argparse
import datetime
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

# Clone/update jingu-bundle-loader and copy Python package into build context
cd /root
if [ -d jingu-bundle-loader ]; then
  cd jingu-bundle-loader && git pull -q && cd ..
else
  git clone https://github.com/ylu999/jingu-bundle-loader.git -q
fi
mkdir -p /root/jingu-swebench/python
cp -r /root/jingu-bundle-loader/python/jingu_loader /root/jingu-swebench/python/
cd /root/jingu-swebench

if [ ! -d jingu-trust-gate/node_modules ]; then
  docker run --rm -v $(pwd)/jingu-trust-gate:/work -w /work node:18-alpine npm install --silent 2>/dev/null
fi

aws ecr get-login-password --region us-west-2 | docker login --username AWS --password-stdin 235494812052.dkr.ecr.us-west-2.amazonaws.com 2>/dev/null

GIT_COMMIT=$(git rev-parse HEAD)
BUILD_TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
echo "Building commit $GIT_COMMIT at $BUILD_TIMESTAMP"

docker build --no-cache \\
  --build-arg GIT_COMMIT=$GIT_COMMIT \\
  --build-arg BUILD_TIMESTAMP=$BUILD_TIMESTAMP \\
  -t jingu-swebench:latest . 2>&1 | grep -E "^Step|#|error|ERROR|Successfully|DONE" | tail -30

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

BATCH_GUARD_THRESHOLD = 3  # more than this many instances requires --confirmed

_RUNBOOK_PATH = ".claude/smoke-test-runbook.md"

def _parse_env_args(args) -> list[dict]:
    """Parse --env KEY=VALUE pairs into ECS environment override dicts."""
    extra_env = []
    for item in getattr(args, "env", None) or []:
        if "=" not in item:
            print(f"[ops] ERROR: --env value must be KEY=VALUE, got: {item}")
            sys.exit(1)
        k, v = item.split("=", 1)
        extra_env.append({"name": k, "value": v})
    return extra_env


def _check_runbook_ack(args) -> None:
    """P1 enforcement: runbook must be explicitly acknowledged before any launch.
    Passing --runbook-ack is the structural proof that the runbook was read this session.
    Documentation alone (CLAUDE.md) is not sufficient — this flag makes it machine-checked.
    """
    if not getattr(args, "runbook_ack", False):
        print(f"[ops] ERROR: --runbook-ack flag is required to launch any ECS task.")
        print(f"[ops] Rule: read the runbook first, then pass --runbook-ack to confirm.")
        print(f"[ops] Runbook: {_RUNBOOK_PATH}")
        sys.exit(1)


_PIPELINE_ONLY_MSG = """[ops] BLOCKED: '{cmd}' is disabled. All runs must go through 'pipeline'.

  Pipeline covers all use cases:
    smoke test:   python scripts/ops.py pipeline --batch-name NAME --runbook-ack
    batch + eval: python scripts/ops.py pipeline --batch-name NAME --runbook-ack
    eval only:    python scripts/ops.py pipeline --batch-name NAME --eval-only --runbook-ack

  Why: 'pipeline' guarantees eval runs + results are tracked. Other paths skip eval.
"""


def cmd_run(args) -> None:
    print(_PIPELINE_ONLY_MSG.format(cmd="run"), flush=True)
    sys.exit(1)


def _cmd_run_impl(args) -> None:
    """Internal: original run implementation, kept for reference but unreachable."""
    _check_runbook_ack(args)
    # Batch guard: more than BATCH_GUARD_THRESHOLD instances requires explicit --confirmed flag.
    # This prevents accidental large batch launches without user approval.
    if len(args.instance_ids) > BATCH_GUARD_THRESHOLD and not args.confirmed:
        print(f"[ops] ERROR: {len(args.instance_ids)} instances > {BATCH_GUARD_THRESHOLD} (batch guard)")
        print(f"[ops] Rule: smoke test 1 instance first, get user approval, then add --confirmed to launch batch.")
        print(f"[ops] To proceed after user approval: add --confirmed to your command.")
        sys.exit(1)

    print("[ops] WARNING: 'run' does not auto-eval or track results in pipeline history.", flush=True)
    print("[ops] WARNING: For tracked runs, use: python scripts/ops.py pipeline --batch-name NAME", flush=True)

    ecs = boto3.client("ecs", region_name=REGION)

    instance_ids_str = " ".join(args.instance_ids)
    batch_name = args.batch_name
    output_path = f"/app/results/{batch_name}"

    cmd_parts = [
        "--instance-ids", *args.instance_ids,
        "--dataset", args.dataset,
        "--mode", args.mode,
        "--max-attempts", str(args.max_attempts),
        "--workers", str(args.workers),
        "--output", output_path,
    ]
    # Note: s3 upload is handled by docker-entrypoint.sh via S3_BUCKET env var

    print(f"[ops] launching ECS task: {batch_name}")
    print(f"[ops] instances: {instance_ids_str}")
    print(f"[ops] mode={args.mode} attempts={args.max_attempts} workers={args.workers}")

    extra_env = _parse_env_args(args)
    if extra_env:
        print(f"[ops] env overrides: {' '.join(f'{e['name']}={e['value']}' for e in extra_env)}")

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
                    *(
                        [{"name": "JINGU_MODEL", "value": args.model}]
                        if getattr(args, "model", None) else []
                    ),
                    *extra_env,
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

    # Store task_id → batch mapping in S3 so `watch` can look it up by batch name
    try:
        s3 = boto3.client("s3", region_name=REGION)
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=f"{batch_name}/task_id.txt",
            Body=task_id.encode(),
        )
    except Exception as e:
        print(f"[ops] warning: could not store task_id in S3: {e}")

    print(f"[ops] ECS task launched: {task_id}")
    print(f"[ops] watch: python scripts/ops.py watch --batch-name {batch_name}")
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


def cmd_eval(args) -> None:
    print(_PIPELINE_ONLY_MSG.format(cmd="eval"), flush=True)
    sys.exit(1)


def _cmd_eval_impl(args) -> None:
    """Internal: original eval implementation, kept for reference but unreachable."""
    _check_runbook_ack(args)
    ecs = boto3.client("ecs", region_name=REGION)

    # predictions_path is either a full s3://bucket/key or just the S3 key
    pred_path = args.predictions_path
    if pred_path.startswith("s3://"):
        pred_path = pred_path[len(f"s3://{S3_BUCKET}/"):]

    eval_name = args.run_id
    output_path = f"/app/results/{eval_name}"

    cmd_parts = [
        "--eval",
        "--predictions-s3", pred_path,
        "--run-id", args.run_id,
        "--workers", str(args.workers),
        "--dataset", args.dataset,
        "--output", output_path,
    ]

    print(f"[ops] launching eval task: {eval_name}")
    print(f"[ops] predictions-s3: {pred_path}")
    print(f"[ops] workers={args.workers} dataset={args.dataset}")

    resp = ecs.run_task(
        cluster=ECS_CLUSTER,
        taskDefinition=ECS_TASK_DEF,
        launchType="EC2",
        overrides={
            "containerOverrides": [{
                "name": "runner",
                "command": cmd_parts,
                "environment": [
                    {"name": "BATCH_NAME", "value": eval_name},
                ],
            }]
        },
    )

    failures = resp.get("failures", [])
    if failures:
        print(f"[ops] FAILED to launch: {failures}")
        sys.exit(1)

    task = resp["tasks"][0]
    task_id = task["taskArn"].split("/")[-1]
    print(f"[ops] ECS eval task launched: {task_id}")
    print(f"[ops] status: python scripts/ops.py status --task-id {task_id}")
    print(f"[ops] logs: python scripts/ops.py logs --task-id {task_id}")


def _get_task_progress(task_id: str) -> str:
    """Extract latest progress signal from CloudWatch logs tail."""
    logs = boto3.client("logs", region_name=REGION)
    stream = f"runner/runner/{task_id}"
    try:
        resp = logs.get_log_events(
            logGroupName=LOG_GROUP, logStreamName=stream,
            limit=200, startFromHead=False,
        )
        events = resp.get("events", [])
        # Scan backwards for progress signals
        progress_signals = []
        for e in reversed(events):
            msg = e["message"]
            # Eval progress: instance completion
            for pat in [
                r"(\d+)\s+instances?\s+completed",
                r"Resolved\s+(\d+)\s+instances",
                r'"resolved_instances":\s*(\d+)',
                r"(\d+)/(\d+)\s+resolved",
                r"\[eval\].*?(\d+)/(\d+)",
                r"Instance\s+\S+\s+(PASSED|FAILED)",
                r"Gold.*?(PASSED|FAILED)",
            ]:
                import re as _re
                m = _re.search(pat, msg)
                if m:
                    # Return the matching line (trimmed)
                    clean = msg.strip()[:120]
                    return clean
            # Agent run progress
            for pat in [
                r"\[jingu\]\s+(DONE|FAILED|ACCEPTED|REJECTED)",
                r"result\]\s+(ACCEPTED|REJECTED|FAILED)",
                r"\[attempt\s+\d+/\d+\]",
            ]:
                m = _re.search(pat, msg)
                if m:
                    return msg.strip()[:120]
        # Fallback: last non-empty, non-noise line
        for e in reversed(events):
            line = e["message"].strip()
            if (line and len(line) > 5
                    and 'level=info msg="' not in line
                    and 'Generating ' not in line
                    and not line.startswith('time="')):
                return line[:120]
        return "(no logs yet)"
    except Exception:
        return "(logs unavailable)"


def cmd_list_tasks(args) -> None:
    """List currently RUNNING/PENDING ECS tasks with progress."""
    tasks = _get_running_tasks()
    if not tasks:
        print("[ops] no running/pending tasks", flush=True)
        return

    show_progress = getattr(args, "progress", True)

    print(f"{'TASK ID':<40} {'STATUS':<10} {'BATCH':<30} {'ELAPSED':>8} STARTED", flush=True)
    print("─" * 110, flush=True)
    for t in tasks:
        # Calculate elapsed time
        started = t.get("started", "")
        elapsed_str = ""
        try:
            if hasattr(started, "timestamp"):
                # datetime object
                elapsed = time.time() - started.timestamp()
            else:
                from datetime import datetime as _dt, timezone as _tz
                dt = _dt.fromisoformat(str(started).replace("+00:00", "+00:00"))
                elapsed = time.time() - dt.timestamp()
            mins = int(elapsed // 60)
            elapsed_str = f"{mins}m"
        except Exception:
            elapsed_str = "?"

        print(f"{t['task_id']:<40} {t['status']:<10} {t['batch']:<30} {elapsed_str:>8} {str(started)[:19]}", flush=True)

        if show_progress:
            progress = _get_task_progress(t["task_id"])
            print(f"  └─ {progress}", flush=True)


def cmd_smoke(args) -> None:
    # Allow attach-only mode (--task-id) — this is just log tailing, not a launch
    if args.task_id:
        pass  # fall through to _cmd_smoke_impl
    else:
        print(_PIPELINE_ONLY_MSG.format(cmd="smoke"), flush=True)
        sys.exit(1)
    _cmd_smoke_impl(args)


def _cmd_smoke_impl(args) -> None:
    """
    Launch ECS task + live-tail logs. Or tail an existing task with --task-id.

    Two modes:
      Launch + tail:  --batch-name NAME --instance-ids ID [ID ...]
      Tail existing:  --task-id TASK_ID  (no launch, just attach)

    Single stream, color-coded by instance_id found in log lines.
    Default filter: [jingu] [cp-step] [control-plane] [agent] errors pee:
    Use --filter to narrow, --all for everything.
    """
    # Only require --runbook-ack when launching (not when attaching to existing task)
    if not args.task_id:
        _check_runbook_ack(args)
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

    # Batch guard: more than BATCH_GUARD_THRESHOLD instances requires explicit --confirmed flag.
    if len(instance_ids) > BATCH_GUARD_THRESHOLD and not args.confirmed:
        print(f"[smoke] ERROR: {len(instance_ids)} instances > {BATCH_GUARD_THRESHOLD} (batch guard)", flush=True)
        print(f"[smoke] Rule: smoke test 1 instance first, get user approval, then add --confirmed to launch batch.", flush=True)
        print(f"[smoke] To proceed after user approval: add --confirmed to your command.", flush=True)
        sys.exit(1)

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
        "--dataset", args.dataset,
        "--mode", args.mode,
        "--max-attempts", str(args.max_attempts),
        "--workers", str(args.workers),
        "--output", output_path,
    ]

    print(f"[smoke] batch={batch_name}  instances={len(instance_ids)}", flush=True)
    for i, iid in enumerate(instance_ids):
        color = _COLORS[i % len(_COLORS)]
        print(f"  {color}●{_RESET} {iid}", flush=True)
    print(f"[smoke] dataset={args.dataset} mode={args.mode} attempts={args.max_attempts} workers={args.workers}", flush=True)
    print(f"[smoke] {filter_desc}", flush=True)

    extra_env = _parse_env_args(args)
    if extra_env:
        print(f"[smoke] env overrides: {' '.join(f'{e['name']}={e['value']}' for e in extra_env)}", flush=True)

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
                    *(
                        [{"name": "JINGU_MODEL", "value": args.model}]
                        if getattr(args, "model", None) else []
                    ),
                    *extra_env,
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

    exit_code = "?"
    try:
        t_resp = ecs.describe_tasks(cluster=ECS_CLUSTER, tasks=[task_id])
        tasks_list = t_resp.get("tasks", [])
        if tasks_list:
            s = tasks_list[0]
            exit_code = s.get("containers", [{}])[0].get("exitCode", "?")
            print(f"[smoke] final status={s.get('lastStatus')} exit={exit_code}", flush=True)
    except Exception:
        pass

    # ── Auto-eval: launch SWE-bench eval if task succeeded ──────────────────
    skip_eval = getattr(args, "skip_eval", False)
    if exit_code == 0 and not skip_eval:
        predictions_key = f"{batch_name}/jingu-predictions.jsonl"
        s3_client = boto3.client("s3", region_name=REGION)
        try:
            s3_client.head_object(Bucket=S3_BUCKET, Key=predictions_key)
        except Exception:
            print(f"[smoke] predictions not found at s3://{S3_BUCKET}/{predictions_key}, skipping eval", flush=True)
            return

        eval_run_id = f"eval-{batch_name}"
        print(f"\n[smoke] ── auto-eval ──────────────────────────────────────────", flush=True)
        print(f"[smoke] launching eval: {eval_run_id}", flush=True)
        print(f"[smoke] predictions: {predictions_key}", flush=True)
        try:
            eval_task_id = _launch_eval_task(predictions_key, eval_run_id, workers=4)
            print(f"[smoke] eval task: {eval_task_id}", flush=True)
            eval_task = _wait_for_task(eval_task_id, "smoke-eval", poll_interval=20, timeout_s=3600)
            eval_exit = eval_task.get("containers", [{}])[0].get("exitCode", 1)
            if eval_exit == 0:
                eval_data = _read_eval_results_from_s3(eval_run_id)
                if eval_data:
                    resolved_ids = eval_data.get("resolved_ids", [])
                    unresolved_ids = eval_data.get("unresolved_ids", [])
                    print(f"[smoke] eval result: {len(resolved_ids)}/{len(resolved_ids)+len(unresolved_ids)} resolved", flush=True)
                    for rid in resolved_ids:
                        print(f"  ✓ {rid}", flush=True)
                    for uid in unresolved_ids:
                        print(f"  ✗ {uid}", flush=True)
                else:
                    resolved, total = _parse_resolved_from_cw(eval_task_id)
                    print(f"[smoke] eval result (from CW): {resolved}/{total} resolved", flush=True)
            else:
                print(f"[smoke] eval task failed (exit={eval_exit})", flush=True)
        except Exception as e:
            print(f"[smoke] eval failed: {e}", flush=True)
    elif exit_code != 0:
        print(f"[smoke] task failed (exit={exit_code}), skipping eval", flush=True)
    elif skip_eval:
        print(f"[smoke] --skip-eval specified, skipping eval", flush=True)


# ── watch ──────────────────────────────────────────────────────────────────────

def _resolve_task_id(batch_name: str | None, task_id: str | None) -> tuple[str, list[str]]:
    """
    Resolve task_id and instance_ids from either --task-id or --batch-name.
    Returns (task_id, instance_ids).
    """
    if task_id:
        # Try to discover instance_ids from task description
        ecs = boto3.client("ecs", region_name=REGION)
        instance_ids = []
        try:
            resp = ecs.describe_tasks(cluster=ECS_CLUSTER, tasks=[task_id])
            tasks = resp.get("tasks", [])
            if tasks:
                for ov in tasks[0].get("overrides", {}).get("containerOverrides", []):
                    cmd = ov.get("command", [])
                    if "--instance-ids" in cmd:
                        idx = cmd.index("--instance-ids") + 1
                        while idx < len(cmd) and not cmd[idx].startswith("--"):
                            instance_ids.append(cmd[idx])
                            idx += 1
        except Exception:
            pass
        return task_id, instance_ids

    if batch_name:
        s3 = boto3.client("s3", region_name=REGION)
        # Read task_id from S3
        try:
            resp = s3.get_object(Bucket=S3_BUCKET, Key=f"{batch_name}/task_id.txt")
            tid = resp["Body"].read().decode().strip()
        except Exception:
            print(f"[watch] no task_id found for batch={batch_name}")
            print(f"[watch] either the batch hasn't started yet, or use --task-id directly")
            sys.exit(1)

        # Read instance_ids from run_report if available, else from ECS task
        instance_ids = []
        try:
            resp2 = s3.get_object(Bucket=S3_BUCKET, Key=f"{batch_name}/run_report.json")
            report = json.loads(resp2["Body"].read())
            instance_ids = list(report.get("model_usage", {}).get("per_instance", {}).keys())
        except Exception:
            pass

        if not instance_ids:
            # Fall back to ECS task description
            ecs = boto3.client("ecs", region_name=REGION)
            try:
                resp3 = ecs.describe_tasks(cluster=ECS_CLUSTER, tasks=[tid])
                tasks = resp3.get("tasks", [])
                if tasks:
                    for ov in tasks[0].get("overrides", {}).get("containerOverrides", []):
                        cmd = ov.get("command", [])
                        if "--instance-ids" in cmd:
                            idx = cmd.index("--instance-ids") + 1
                            while idx < len(cmd) and not cmd[idx].startswith("--"):
                                instance_ids.append(cmd[idx])
                                idx += 1
            except Exception:
                pass

        return tid, instance_ids

    print("[watch] must provide --batch-name or --task-id")
    sys.exit(1)


def cmd_watch(args) -> None:
    """
    Real-time log tail for a batch or single instance.

    --batch-name NAME              tail all instances in the batch (color-coded)
    --batch-name NAME --instance-id ID   tail only one instance
    --task-id TASK_ID              attach directly by task ID
    --all                          show all lines (no filter)
    --filter REGEX                 custom filter
    """
    task_id, instance_ids = _resolve_task_id(
        getattr(args, "batch_name", None),
        getattr(args, "task_id", None),
    )

    # If filtering to a single instance, narrow the instance_ids list so color
    # assignment is stable, and add the instance_id as an extra filter term.
    focus = getattr(args, "instance_id", None)
    if focus:
        if focus not in instance_ids:
            instance_ids = [focus]
        else:
            instance_ids = [focus]

    # Build filter pattern
    if getattr(args, "all", False):
        filter_pat = re.compile(r".")
        filter_desc = "all lines"
    elif getattr(args, "filter", None):
        base = args.filter
        if focus:
            base = f"({base}).*{re.escape(focus)}|{re.escape(focus)}.*({base})"
        filter_pat = re.compile(base)
        filter_desc = f"filter: {args.filter}"
    else:
        # Default: signal lines only; if focus, also require instance_id in line
        if focus:
            filter_pat = re.compile(
                f"({_DEFAULT_FILTER.pattern}).*{re.escape(focus)}"
                f"|{re.escape(focus)}.*({_DEFAULT_FILTER.pattern})"
                f"|\\[jingu\\].*{re.escape(focus)}"
                f"|instance={re.escape(focus)}"
            )
        else:
            filter_pat = _DEFAULT_FILTER
        filter_desc = f"default signals{' for ' + focus if focus else ''}"

    print(f"[watch] task={task_id}", flush=True)
    print(f"[watch] instances={len(instance_ids)}{' focus=' + focus if focus else ''}", flush=True)
    print(f"[watch] {filter_desc}", flush=True)
    print(f"[watch] log stream: runner/runner/{task_id}", flush=True)
    print("─" * 70, flush=True)

    try:
        _tail_shared_stream(task_id, instance_ids, filter_pat)
    except KeyboardInterrupt:
        print("\n[watch] interrupted", flush=True)

    print("─" * 70, flush=True)

    # Show final task status
    try:
        ecs = boto3.client("ecs", region_name=REGION)
        resp = ecs.describe_tasks(cluster=ECS_CLUSTER, tasks=[task_id])
        tasks = resp.get("tasks", [])
        if tasks:
            t = tasks[0]
            exit_code = t.get("containers", [{}])[0].get("exitCode", "?")
            print(f"[watch] final status={t.get('lastStatus')}  exit={exit_code}", flush=True)
    except Exception:
        pass


# ── pipeline ───────────────────────────────────────────────────────────────────

PIPELINE_HISTORY_KEY = "pipeline-results/history.json"
INSTANCE_RECORDS_PREFIX = "pipeline-results/instances"

# 30 standard django instances used for all pipeline runs
PIPELINE_DEFAULT_INSTANCES = [
    "django__django-10097", "django__django-10554", "django__django-10880",
    "django__django-10914", "django__django-10973", "django__django-10999",
    "django__django-11066", "django__django-11087", "django__django-11095",
    "django__django-11099", "django__django-11119", "django__django-11133",
    "django__django-11138", "django__django-11141", "django__django-11149",
    "django__django-11163", "django__django-11179", "django__django-11206",
    "django__django-11211", "django__django-11239", "django__django-11265",
    "django__django-11276", "django__django-11292", "django__django-11299",
    "django__django-11333", "django__django-11400", "django__django-11433",
    "django__django-11451", "django__django-11477", "django__django-11490",
]


def _wait_for_task(task_id: str, label: str, poll_interval: int = 30, timeout_s: int = 7200) -> dict:
    """Poll ECS until task STOPPED. Returns final task dict."""
    ecs = boto3.client("ecs", region_name=REGION)
    deadline = time.monotonic() + timeout_s
    dots = 0
    while time.monotonic() < deadline:
        resp = ecs.describe_tasks(cluster=ECS_CLUSTER, tasks=[task_id])
        tasks = resp.get("tasks", [])
        if not tasks:
            print(f"\n[pipeline] {label}: task {task_id} not found", flush=True)
            sys.exit(1)
        task = tasks[0]
        status = task.get("lastStatus", "?")
        if status == "STOPPED":
            exit_code = task.get("containers", [{}])[0].get("exitCode", "?")
            elapsed = int(time.monotonic() - (deadline - timeout_s))
            print(f"\n[pipeline] {label}: STOPPED exit={exit_code} elapsed={elapsed}s", flush=True)
            return task
        dots += 1
        if dots % 4 == 0:
            elapsed = int(time.monotonic() - (deadline - timeout_s))
            print(f"\r[pipeline] {label}: {status} ... {elapsed}s", end="", flush=True)
        time.sleep(poll_interval)
    raise TimeoutError(f"{label} did not complete within {timeout_s}s")


def _launch_ecs_task(batch_name: str, instance_ids: list[str], max_attempts: int,
                     workers: int, mode: str = "jingu", model: str | None = None,
                     max_retries: int = 10, retry_interval: int = 120) -> str:
    """Launch an ECS run task and return task_id. Retries on resource failures."""
    ecs = boto3.client("ecs", region_name=REGION)
    output_path = f"/app/results/{batch_name}"
    cmd_parts = [
        "--instance-ids", *instance_ids,
        "--dataset", "Verified",
        "--mode", mode,
        "--max-attempts", str(max_attempts),
        "--workers", str(workers),
        "--output", output_path,
    ]
    env = [
        {"name": "BATCH_NAME", "value": batch_name},
        {"name": "STRATEGY_LOG_PATH", "value": f"/app/results/{batch_name}/strategy_log.jsonl"},
        {"name": "STRATEGY_TABLE_PATH", "value": f"/app/results/{batch_name}/strategy_table.json"},
    ]
    if model:
        env.append({"name": "JINGU_MODEL", "value": model})
    overrides = {"containerOverrides": [{"name": "runner", "command": cmd_parts, "environment": env}]}

    for attempt in range(max_retries + 1):
        resp = ecs.run_task(
            cluster=ECS_CLUSTER,
            taskDefinition=ECS_TASK_DEF,
            launchType="EC2",
            overrides=overrides,
        )
        failures = resp.get("failures", [])
        if not failures:
            return resp["tasks"][0]["taskArn"].split("/")[-1]
        if _is_resource_failure(failures) and attempt < max_retries:
            print(f"[pipeline] resource unavailable (attempt {attempt+1}/{max_retries+1}), "
                  f"retrying in {retry_interval}s...", flush=True)
            time.sleep(retry_interval)
            continue
        print(f"[pipeline] FAILED to launch: {failures}", flush=True)
        sys.exit(1)
    # unreachable
    sys.exit(1)


def _is_resource_failure(failures: list[dict]) -> bool:
    """Check if ECS run_task failures are resource-related (retryable)."""
    return any("RESOURCE:" in f.get("reason", "") for f in failures)


def _launch_eval_task(predictions_key: str, run_id: str, workers: int = 4,
                      max_retries: int = 10, retry_interval: int = 120) -> str:
    """Launch an ECS eval task and return task_id. Retries on resource failures."""
    ecs = boto3.client("ecs", region_name=REGION)
    output_path = f"/app/results/{run_id}"
    cmd_parts = [
        "--eval",
        "--predictions-s3", predictions_key,
        "--run-id", run_id,
        "--workers", str(workers),
        "--dataset", "SWE-bench/SWE-bench_Verified",
        "--output", output_path,
    ]
    overrides = {"containerOverrides": [{
        "name": "runner",
        "command": cmd_parts,
        "environment": [{"name": "BATCH_NAME", "value": run_id}],
    }]}

    for attempt in range(max_retries + 1):
        resp = ecs.run_task(
            cluster=ECS_CLUSTER,
            taskDefinition=ECS_TASK_DEF,
            launchType="EC2",
            overrides=overrides,
        )
        failures = resp.get("failures", [])
        if not failures:
            return resp["tasks"][0]["taskArn"].split("/")[-1]
        if _is_resource_failure(failures) and attempt < max_retries:
            print(f"[pipeline] resource unavailable (attempt {attempt+1}/{max_retries+1}), "
                  f"retrying in {retry_interval}s...", flush=True)
            time.sleep(retry_interval)
            continue
        print(f"[pipeline] FAILED to launch eval: {failures}", flush=True)
        sys.exit(1)
    # unreachable
    sys.exit(1)


def _parse_resolved_from_cw(task_id: str) -> tuple[int, int]:
    """
    Parse resolved/total from CloudWatch logs of an eval task.
    Returns (resolved, total). Returns (-1, -1) if not found.
    """
    logs = boto3.client("logs", region_name=REGION)
    stream = f"runner/runner/{task_id}"
    try:
        all_events = []
        token = None
        while True:
            kwargs = dict(logGroupName=LOG_GROUP, logStreamName=stream, limit=500, startFromHead=True)
            if token:
                kwargs["nextToken"] = token
            resp = logs.get_log_events(**kwargs)
            new = resp["events"]
            if not new:
                break
            all_events.extend(new)
            new_token = resp.get("nextForwardToken")
            if new_token == token:
                break
            token = new_token

        # Look for lines like: "Resolved 17 instances" or "resolved_instances: 17"
        resolved = total = -1
        for e in all_events:
            msg = e["message"]
            # SWE-bench eval output patterns
            m = re.search(r"Resolved\s+(\d+)\s+instances", msg)
            if m:
                resolved = int(m.group(1))
            m2 = re.search(r'"resolved_instances":\s*(\d+)', msg)
            if m2:
                resolved = int(m2.group(1))
            m3 = re.search(r'"total_instances":\s*(\d+)', msg)
            if m3:
                total = int(m3.group(1))
            m4 = re.search(r'(\d+)/(\d+)\s+resolved', msg, re.IGNORECASE)
            if m4:
                resolved, total = int(m4.group(1)), int(m4.group(2))
        return resolved, total
    except Exception as e:
        print(f"[pipeline] warning: could not parse eval logs: {e}", flush=True)
        return -1, -1


def _read_eval_results_from_s3(eval_run_id: str) -> dict:
    """
    Read eval_results.json from S3 for a given eval run.
    Returns dict with 'resolved_ids' and 'unresolved_ids' lists.
    Returns {} if not found.
    """
    s3 = boto3.client("s3", region_name=REGION)
    # Try multiple path patterns:
    # 1. <eval_run_id>/eval_results.json (eval task output dir)
    # 2. eval-<batch>/eval_results.json
    candidates = [
        f"{eval_run_id}/eval_results.json",
    ]
    # If eval_run_id doesn't start with "eval-", also try with prefix
    if not eval_run_id.startswith("eval-"):
        candidates.append(f"eval-{eval_run_id}/eval_results.json")

    for key in candidates:
        try:
            resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
            data = json.loads(resp["Body"].read())
            resolved = data.get("resolved_ids", [])
            unresolved = data.get("unresolved_ids", [])
            print(f"[eval] loaded eval_results from s3://{S3_BUCKET}/{key}: "
                  f"{len(resolved)} resolved, {len(unresolved)} unresolved", flush=True)
            return data
        except Exception:
            continue
    return {}


def _get_git_commit_from_s3(batch_name: str) -> str:
    """Read git commit from run_report.json in S3."""
    s3 = boto3.client("s3", region_name=REGION)
    try:
        resp = s3.get_object(Bucket=S3_BUCKET, Key=f"{batch_name}/run_report.json")
        report = json.loads(resp["Body"].read())
        return report.get("execution_identity", {}).get("git_commit", "unknown")[:12]
    except Exception:
        return "unknown"


def _get_cost_from_s3(batch_name: str) -> float:
    """Read total cost from run_report.json in S3."""
    s3 = boto3.client("s3", region_name=REGION)
    try:
        resp = s3.get_object(Bucket=S3_BUCKET, Key=f"{batch_name}/run_report.json")
        report = json.loads(resp["Body"].read())
        return report.get("model_usage", {}).get("total_cost_usd", 0.0)
    except Exception:
        return 0.0


def _append_pipeline_history(record: dict) -> None:
    """Append a record to the pipeline history JSON in S3."""
    s3 = boto3.client("s3", region_name=REGION)
    history = []
    try:
        resp = s3.get_object(Bucket=S3_BUCKET, Key=PIPELINE_HISTORY_KEY)
        history = json.loads(resp["Body"].read())
    except s3.exceptions.NoSuchKey:
        pass
    except Exception:
        pass
    history.append(record)
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=PIPELINE_HISTORY_KEY,
        Body=json.dumps(history, indent=2).encode(),
        ContentType="application/json",
    )


def _run_replay_gate() -> bool:
    """Run replay gate tests (SST projection chain verification).

    Returns True if all tests pass, False otherwise.
    Must pass before any pipeline launch — catches contract drift locally.
    """
    import subprocess
    print("[pipeline] STEP 0: replay gate (SST projection chain verification)", flush=True)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_replay_gate.py", "-v", "--tb=short"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("[pipeline] REPLAY GATE FAILED — contract drift detected", flush=True)
        print(result.stdout[-2000:] if len(result.stdout) > 2000 else result.stdout, flush=True)
        if result.stderr:
            print(result.stderr[-500:], flush=True)
        return False
    # Show summary line
    for line in result.stdout.splitlines():
        if "passed" in line or "failed" in line:
            print(f"[pipeline] replay gate: {line.strip()}", flush=True)
    return True


def _diagnose_unresolved(s3_client, batch_name: str, unresolved_ids: list[str]) -> dict[str, dict]:
    """Diagnose root cause for each unresolved instance.

    Reads step_events.jsonl and decisions.jsonl from S3 for the last attempt,
    classifies the failure, and returns a dict of {instance_id: diagnosis}.

    Diagnosis keys:
        cause: str — failure category
        detail: str — human-readable explanation
        last_phase: str — phase when agent stopped
        total_steps: int — steps taken
        patch_generated: bool — whether a non-empty patch existed
        phases_reached: list[str] — phases entered during the run
    """
    diagnoses: dict[str, dict] = {}
    for iid in unresolved_ids:
        diag = {"cause": "unknown", "detail": "", "last_phase": "?",
                "total_steps": 0, "patch_generated": False, "phases_reached": []}
        try:
            # Find last attempt by listing attempt dirs
            prefix = f"{batch_name}/{iid}/attempt_"
            resp = s3_client.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix, Delimiter="/")
            attempt_prefixes = sorted(
                [p["Prefix"] for p in resp.get("CommonPrefixes", [])],
                key=lambda p: int(p.rstrip("/").rsplit("_", 1)[-1]),
            )
            if not attempt_prefixes:
                diag["cause"] = "no_attempt_data"
                diag["detail"] = "no attempt directories found in S3"
                diagnoses[iid] = diag
                continue

            last_attempt_prefix = attempt_prefixes[-1]

            # Read step_events.jsonl
            events = []
            try:
                obj = s3_client.get_object(Bucket=S3_BUCKET, Key=f"{last_attempt_prefix}step_events.jsonl")
                for line in obj["Body"].read().decode().strip().split("\n"):
                    if line.strip():
                        events.append(json.loads(line))
            except Exception:
                pass

            # Read decisions.jsonl
            decisions = []
            try:
                obj = s3_client.get_object(Bucket=S3_BUCKET, Key=f"{last_attempt_prefix}decisions.jsonl")
                for line in obj["Body"].read().decode().strip().split("\n"):
                    if line.strip():
                        decisions.append(json.loads(line))
            except Exception:
                pass

            if not events:
                diag["cause"] = "no_step_events"
                diag["detail"] = "step_events.jsonl empty or missing"
                diagnoses[iid] = diag
                continue

            # Extract signals
            phases_seen = []
            for e in events:
                p = e.get("phase", "")
                if p and (not phases_seen or phases_seen[-1] != p):
                    phases_seen.append(p)
            last_event = events[-1]
            last_phase = last_event.get("phase", "?")
            total_steps = last_event.get("step_n", len(events))
            patch_non_empty = any(e.get("patch_non_empty", False) for e in events)
            env_errors = [e for e in events if e.get("env_error", False)]

            diag["last_phase"] = last_phase
            diag["total_steps"] = total_steps
            diag["patch_generated"] = patch_non_empty
            diag["phases_reached"] = phases_seen

            # Classify
            if env_errors:
                diag["cause"] = "env_failure"
                diag["detail"] = f"{len(env_errors)} steps with env_error; last_phase={last_phase}"
            elif not patch_non_empty:
                diag["cause"] = "no_patch"
                diag["detail"] = f"no patch generated; stuck in {last_phase} after {total_steps} steps"
            elif last_phase in ("OBSERVE", "ANALYZE", "DECIDE", "DESIGN"):
                diag["cause"] = f"phase_stuck_{last_phase}"
                diag["detail"] = f"patch exists but never reached EXECUTE; stuck in {last_phase}"
            elif last_phase == "EXECUTE":
                # Patch exists, reached EXECUTE — probably wrong patch
                cp = last_event.get("cp_state_snapshot", {})
                phase_steps = cp.get("step", 0)
                diag["cause"] = "wrong_patch"
                diag["detail"] = f"patch generated, EXECUTE reached ({phase_steps} steps in phase), eval failed"
            elif last_phase == "JUDGE":
                diag["cause"] = "wrong_patch"
                diag["detail"] = f"reached JUDGE but eval failed — patch incorrect"
            else:
                diag["cause"] = "unknown"
                diag["detail"] = f"last_phase={last_phase} steps={total_steps} patch={'yes' if patch_non_empty else 'no'}"

        except Exception as exc:
            diag["cause"] = "diagnosis_error"
            diag["detail"] = str(exc)[:200]

        diagnoses[iid] = diag
    return diagnoses


def cmd_pipeline(args) -> None:
    """
    Full automated pipeline:
      0. Replay gate — SST projection chain verification (local, no LLM)
      1. Smoke test (1 instance) — verify new behavior present
      2. Batch run (30 instances)
      3. Eval (SWE-bench official)
      3.5. Root cause analysis (unresolved instances)
      4. Store results to S3 pipeline-results/history.json
    """
    batch_name = args.batch_name
    smoke_instance = args.smoke_instance
    instance_ids = args.instance_ids or PIPELINE_DEFAULT_INSTANCES
    max_attempts = args.max_attempts
    workers = args.workers
    model = getattr(args, "model", None)
    skip_smoke = args.skip_smoke
    eval_only = getattr(args, "eval_only", False)

    # ── Step 0: Replay gate (always runs, even in eval-only mode) ──────────
    if not _run_replay_gate():
        print("[pipeline] ABORTED — fix contract drift before deploying", flush=True)
        sys.exit(1)
    print("[pipeline] replay gate PASSED\n", flush=True)

    timestamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    smoke_batch = f"smoke-{batch_name}"
    eval_run_id = f"eval-{batch_name}"

    # ── eval-only mode: skip smoke + batch, go straight to eval ────────────
    if eval_only:
        predictions_key = f"{batch_name}/jingu-predictions.jsonl"
        # Verify predictions exist in S3
        s3_check = boto3.client("s3", region_name=REGION)
        try:
            head = s3_check.head_object(Bucket=S3_BUCKET, Key=predictions_key)
            pred_size = head["ContentLength"]
        except Exception:
            print(f"[pipeline] ERROR: predictions not found at s3://{S3_BUCKET}/{predictions_key}", flush=True)
            sys.exit(1)

        # Count instances in predictions
        try:
            resp = s3_check.get_object(Bucket=S3_BUCKET, Key=predictions_key)
            pred_lines = resp["Body"].read().decode().strip().split("\n")
            pred_instances = [json.loads(l).get("instance_id", "") for l in pred_lines if l.strip()]
            instance_ids = pred_instances
        except Exception as e:
            print(f"[pipeline] ERROR: could not read predictions: {e}", flush=True)
            sys.exit(1)

        git_commit = _get_git_commit_from_s3(batch_name)
        cost_usd = _get_cost_from_s3(batch_name)

        print(f"[pipeline] ══════════════════════════════════════════", flush=True)
        print(f"[pipeline] EVAL-ONLY mode", flush=True)
        print(f"[pipeline] batch={batch_name}  instances={len(instance_ids)}", flush=True)
        print(f"[pipeline] predictions={predictions_key} ({pred_size} bytes)", flush=True)
        print(f"[pipeline] commit={git_commit}  cost=${cost_usd:.2f}", flush=True)
        print(f"[pipeline] ══════════════════════════════════════════", flush=True)

        # Jump straight to eval (Step 3 below)
        # We'll fall through to Step 3 by setting skip_smoke=True and
        # skipping batch launch — use a goto-like structure
        print(f"\n[pipeline] STEP 1/1: eval (run_id={eval_run_id})", flush=True)
        eval_task_id = _launch_eval_task(predictions_key, eval_run_id, workers=args.eval_workers)
        print(f"[pipeline] eval task_id={eval_task_id}", flush=True)
        eval_task = _wait_for_task(eval_task_id, "eval", poll_interval=30, timeout_s=7200)
        eval_exit = eval_task.get("containers", [{}])[0].get("exitCode", 1)

        resolved = total = -1
        resolved_ids: list[str] = []
        unresolved_ids: list[str] = []

        if eval_exit == 0:
            eval_data = _read_eval_results_from_s3(eval_run_id)
            if eval_data:
                resolved_ids = eval_data.get("resolved_ids", [])
                unresolved_ids = eval_data.get("unresolved_ids", [])
                resolved = len(resolved_ids)
                total = resolved + len(unresolved_ids)
            else:
                print(f"[pipeline] eval_results.json not found in S3, falling back to CW logs", flush=True)
                resolved, total = _parse_resolved_from_cw(eval_task_id)
        else:
            print(f"[pipeline] EVAL task exit={eval_exit}", flush=True)

        if total <= 0:
            total = len(instance_ids)

        # Write per-instance records
        s3_client = boto3.client("s3", region_name=REGION)
        resolved_set = set(resolved_ids)
        unresolved_set = set(unresolved_ids)

        print(f"[pipeline] writing per-instance records for {len(instance_ids)} instances...", flush=True)
        for iid in instance_ids:
            existing = _read_instance_record(s3_client, iid)
            if iid in resolved_set:
                eval_resolved = True
            elif iid in unresolved_set:
                eval_resolved = False
            else:
                eval_resolved = None

            run_entry = {
                "batch": batch_name,
                "git_commit": git_commit,
                "accepted": True,
                "eval_resolved": eval_resolved,
            }

            if existing:
                existing.setdefault("runs", []).append(run_entry)
                existing["last_batch"] = batch_name
                existing["last_commit"] = git_commit
                existing["accepted"] = True
                if eval_resolved is not None:
                    existing["eval_resolved"] = eval_resolved
                _write_instance_record(s3_client, iid, existing)
            else:
                record_data = {
                    "instance_id": iid,
                    "last_batch": batch_name,
                    "last_commit": git_commit,
                    "accepted": True,
                    "eval_resolved": eval_resolved,
                    "runs": [run_entry],
                }
                _write_instance_record(s3_client, iid, record_data)

        # Root cause analysis for unresolved
        unresolved_diagnoses_eo: dict[str, dict] = {}
        if unresolved_ids:
            print(f"\n[pipeline] root cause analysis ({len(unresolved_ids)} unresolved)", flush=True)
            unresolved_diagnoses_eo = _diagnose_unresolved(s3_client, batch_name, unresolved_ids)
            cause_counts_eo: dict[str, int] = {}
            print(f"\n{'Instance':<45} {'Cause':<22} {'Phase':<10} {'Steps':>5} {'Patch':>5}  Detail", flush=True)
            print("─" * 130, flush=True)
            for iid in unresolved_ids:
                d = unresolved_diagnoses_eo.get(iid, {})
                cause = d.get("cause", "unknown")
                cause_counts_eo[cause] = cause_counts_eo.get(cause, 0) + 1
                print(
                    f"{iid:<45} {cause:<22} {d.get('last_phase', '?'):<10} "
                    f"{d.get('total_steps', 0):>5} {'yes' if d.get('patch_generated') else 'no':>5}  "
                    f"{d.get('detail', '')[:60]}",
                    flush=True,
                )
            print(f"\n[pipeline] cause breakdown: {dict(sorted(cause_counts_eo.items(), key=lambda x: -x[1]))}", flush=True)

        # Store pipeline history
        record = {
            "timestamp": timestamp,
            "batch_name": batch_name,
            "git_commit": git_commit,
            "resolved": resolved,
            "total": total,
            "resolve_rate": round(resolved / total, 4) if resolved >= 0 and total > 0 else None,
            "cost_usd": cost_usd,
            "max_attempts": max_attempts,
            "smoke_task_id": None,
            "batch_task_id": None,
            "eval_task_id": eval_task_id,
            "eval_exit": eval_exit,
            "resolved_ids": resolved_ids,
            "eval_only": True,
            "unresolved_causes": {},
        }
        for d in unresolved_diagnoses_eo.values():
            c = d.get("cause", "unknown")
            record["unresolved_causes"][c] = record["unresolved_causes"].get(c, 0) + 1
        _append_pipeline_history(record)

        rate_str = f"{resolved}/{total} ({100*resolved/total:.1f}%)" if resolved >= 0 and total > 0 else "unknown"
        print(f"\n[pipeline] ══════════════════════════════════════════", flush=True)
        print(f"[pipeline] DONE (eval-only)  batch={batch_name}  commit={git_commit}", flush=True)
        print(f"[pipeline] resolved={rate_str}  cost=${cost_usd:.2f}", flush=True)
        print(f"[pipeline] stored in s3://{S3_BUCKET}/{PIPELINE_HISTORY_KEY}", flush=True)
        print(f"[pipeline] ══════════════════════════════════════════", flush=True)
        return

    print(f"[pipeline] ══════════════════════════════════════════", flush=True)
    print(f"[pipeline] batch={batch_name}  instances={len(instance_ids)}", flush=True)
    print(f"[pipeline] smoke_instance={smoke_instance}", flush=True)
    print(f"[pipeline] max_attempts={max_attempts}  workers={workers}", flush=True)
    print(f"[pipeline] timestamp={timestamp}", flush=True)
    print(f"[pipeline] ══════════════════════════════════════════", flush=True)

    # ── Step 1: Smoke test ────────────────────────────────────────────────────
    if not skip_smoke:
        print(f"\n[pipeline] STEP 1/3: smoke test ({smoke_instance})", flush=True)
        smoke_task_id = _launch_ecs_task(
            smoke_batch, [smoke_instance], max_attempts=2, workers=3, model=model
        )
        print(f"[pipeline] smoke task_id={smoke_task_id}", flush=True)
        smoke_task = _wait_for_task(smoke_task_id, "smoke", poll_interval=20, timeout_s=1800)
        smoke_exit = smoke_task.get("containers", [{}])[0].get("exitCode", 1)

        if smoke_exit != 0:
            print(f"[pipeline] SMOKE FAILED (exit={smoke_exit}) — aborting pipeline", flush=True)
            sys.exit(1)

        # Check smoke logs for ACCEPTED signal using filter (searches all events, not just last 500)
        logs_client = boto3.client("logs", region_name=REGION)
        stream = f"runner/runner/{smoke_task_id}"
        accepted = False
        try:
            resp = logs_client.filter_log_events(
                logGroupName=LOG_GROUP,
                logStreamNames=[stream],
                filterPattern='"ACCEPTED"',
                limit=5,
            )
            for e in resp.get("events", []):
                if "result] ACCEPTED" in e["message"]:
                    accepted = True
                    break
        except Exception:
            pass

        if not accepted:
            print(f"[pipeline] SMOKE WARNING: no ACCEPTED signal in logs (task may have run but not resolved)", flush=True)
            print(f"[pipeline] Check manually: python scripts/ops.py peek --task-id {smoke_task_id}", flush=True)
            if sys.stdin.isatty():
                answer = input("[pipeline] Continue to batch anyway? [y/N] ").strip().lower()
                if answer != "y":
                    print("[pipeline] aborted", flush=True)
                    sys.exit(0)
            else:
                print("[pipeline] non-interactive mode — continuing despite missing ACCEPTED signal", flush=True)
        else:
            print(f"[pipeline] smoke PASSED (ACCEPTED signal found)", flush=True)
    else:
        print(f"\n[pipeline] STEP 1/3: smoke test SKIPPED (--skip-smoke)", flush=True)
        smoke_task_id = None

    # ── Step 2: Batch run ─────────────────────────────────────────────────────
    print(f"\n[pipeline] STEP 2/3: batch run ({len(instance_ids)} instances)", flush=True)
    batch_task_id = _launch_ecs_task(
        batch_name, instance_ids, max_attempts=max_attempts, workers=workers, model=model
    )
    print(f"[pipeline] batch task_id={batch_task_id}", flush=True)
    print(f"[pipeline] monitor: python scripts/ops.py logs --task-id {batch_task_id}", flush=True)
    batch_task = _wait_for_task(batch_task_id, "batch", poll_interval=30, timeout_s=14400)
    batch_exit = batch_task.get("containers", [{}])[0].get("exitCode", 1)

    if batch_exit != 0:
        print(f"[pipeline] BATCH FAILED (exit={batch_exit}) — skipping eval", flush=True)
        sys.exit(1)

    # Read run stats from S3
    git_commit = _get_git_commit_from_s3(batch_name)
    cost_usd = _get_cost_from_s3(batch_name)
    predictions_key = f"{batch_name}/jingu-predictions.jsonl"
    print(f"[pipeline] batch done. commit={git_commit} cost=${cost_usd:.2f}", flush=True)

    # ── Step 3: Eval ──────────────────────────────────────────────────────────
    print(f"\n[pipeline] STEP 3/3: eval (run_id={eval_run_id})", flush=True)
    eval_task_id = _launch_eval_task(predictions_key, eval_run_id, workers=4)
    print(f"[pipeline] eval task_id={eval_task_id}", flush=True)
    eval_task = _wait_for_task(eval_task_id, "eval", poll_interval=30, timeout_s=7200)
    eval_exit = eval_task.get("containers", [{}])[0].get("exitCode", 1)

    resolved = total = -1
    resolved_ids: list[str] = []
    unresolved_ids: list[str] = []

    if eval_exit == 0:
        # Primary: read eval_results.json from S3 (written by docker-entrypoint.sh)
        eval_data = _read_eval_results_from_s3(eval_run_id)
        if eval_data:
            resolved_ids = eval_data.get("resolved_ids", [])
            unresolved_ids = eval_data.get("unresolved_ids", [])
            resolved = len(resolved_ids)
            total = resolved + len(unresolved_ids)
        else:
            # Fallback: parse from CloudWatch logs (batch-level totals only)
            print(f"[pipeline] eval_results.json not found in S3, falling back to CW logs", flush=True)
            resolved, total = _parse_resolved_from_cw(eval_task_id)
    else:
        print(f"[pipeline] EVAL task exit={eval_exit}", flush=True)

    if total <= 0:
        total = len(instance_ids)

    # ── Write per-instance records ─────────────────────────────────────────────
    s3_client = boto3.client("s3", region_name=REGION)
    resolved_set = set(resolved_ids)
    unresolved_set = set(unresolved_ids)

    print(f"[pipeline] writing per-instance records for {len(instance_ids)} instances...", flush=True)
    for iid in instance_ids:
        existing = _read_instance_record(s3_client, iid)
        # Determine eval_resolved per instance
        if iid in resolved_set:
            eval_resolved = True
        elif iid in unresolved_set:
            eval_resolved = False
        else:
            eval_resolved = None  # not in eval output (e.g., not submitted)

        run_entry = {
            "batch": batch_name,
            "git_commit": git_commit,
            "accepted": True,  # pipeline only evals accepted predictions
            "eval_resolved": eval_resolved,
        }

        if existing:
            existing.setdefault("runs", []).append(run_entry)
            existing["last_batch"] = batch_name
            existing["last_commit"] = git_commit
            existing["accepted"] = True
            if eval_resolved is not None:
                existing["eval_resolved"] = eval_resolved
            _write_instance_record(s3_client, iid, existing)
        else:
            record_data = {
                "instance_id": iid,
                "last_batch": batch_name,
                "last_commit": git_commit,
                "accepted": True,
                "eval_resolved": eval_resolved,
                "runs": [run_entry],
            }
            _write_instance_record(s3_client, iid, record_data)

    print(f"[pipeline] per-instance records written ({len(resolved_ids)} resolved, {len(unresolved_ids)} unresolved)", flush=True)

    # ── Step 3.5: Root cause analysis for unresolved instances ────────────────
    unresolved_diagnoses: dict[str, dict] = {}
    if unresolved_ids:
        print(f"\n[pipeline] STEP 3.5: root cause analysis ({len(unresolved_ids)} unresolved)", flush=True)
        unresolved_diagnoses = _diagnose_unresolved(s3_client, batch_name, unresolved_ids)

        # Print summary table
        cause_counts: dict[str, int] = {}
        print(f"\n{'Instance':<45} {'Cause':<22} {'Phase':<10} {'Steps':>5} {'Patch':>5}  Detail", flush=True)
        print("─" * 130, flush=True)
        for iid in unresolved_ids:
            d = unresolved_diagnoses.get(iid, {})
            cause = d.get("cause", "unknown")
            cause_counts[cause] = cause_counts.get(cause, 0) + 1
            print(
                f"{iid:<45} {cause:<22} {d.get('last_phase', '?'):<10} "
                f"{d.get('total_steps', 0):>5} {'yes' if d.get('patch_generated') else 'no':>5}  "
                f"{d.get('detail', '')[:60]}",
                flush=True,
            )
        print(f"\n[pipeline] cause breakdown: {dict(sorted(cause_counts.items(), key=lambda x: -x[1]))}", flush=True)

        # Update per-instance records with diagnosis
        for iid, diag in unresolved_diagnoses.items():
            existing = _read_instance_record(s3_client, iid)
            if existing and existing.get("runs"):
                existing["runs"][-1]["unresolved_cause"] = diag.get("cause")
                existing["runs"][-1]["unresolved_detail"] = diag.get("detail")
                existing["runs"][-1]["last_phase"] = diag.get("last_phase")
                existing["runs"][-1]["total_steps"] = diag.get("total_steps")
                existing["runs"][-1]["patch_generated"] = diag.get("patch_generated")
                _write_instance_record(s3_client, iid, existing)

    # ── Store pipeline history ─────────────────────────────────────────────────
    record = {
        "timestamp": timestamp,
        "batch_name": batch_name,
        "git_commit": git_commit,
        "resolved": resolved,
        "total": total,
        "resolve_rate": round(resolved / total, 4) if resolved >= 0 and total > 0 else None,
        "cost_usd": cost_usd,
        "max_attempts": max_attempts,
        "smoke_task_id": smoke_task_id,
        "batch_task_id": batch_task_id,
        "eval_task_id": eval_task_id,
        "eval_exit": eval_exit,
        "resolved_ids": resolved_ids,
        "unresolved_causes": {d.get("cause", "unknown"): 0 for d in unresolved_diagnoses.values()},
    }
    # Count causes properly
    for d in unresolved_diagnoses.values():
        c = d.get("cause", "unknown")
        record["unresolved_causes"][c] = record["unresolved_causes"].get(c, 0) + 1
    _append_pipeline_history(record)

    rate_str = f"{resolved}/{total} ({100*resolved/total:.1f}%)" if resolved >= 0 and total > 0 else "unknown"
    print(f"\n[pipeline] ══════════════════════════════════════════", flush=True)
    print(f"[pipeline] DONE  batch={batch_name}  commit={git_commit}", flush=True)
    print(f"[pipeline] resolved={rate_str}  cost=${cost_usd:.2f}", flush=True)
    if unresolved_diagnoses:
        cause_summary = ", ".join(f"{c}={n}" for c, n in sorted(
            record["unresolved_causes"].items(), key=lambda x: -x[1]) if n > 0)
        print(f"[pipeline] unresolved causes: {cause_summary}", flush=True)
    print(f"[pipeline] stored in s3://{S3_BUCKET}/{PIPELINE_HISTORY_KEY}", flush=True)
    print(f"[pipeline] ══════════════════════════════════════════", flush=True)


# ── history ────────────────────────────────────────────────────────────────────

def cmd_history(args) -> None:
    """Show pipeline run history from S3."""
    s3 = boto3.client("s3", region_name=REGION)
    try:
        resp = s3.get_object(Bucket=S3_BUCKET, Key=PIPELINE_HISTORY_KEY)
        history = json.loads(resp["Body"].read())
    except Exception:
        print("[history] no pipeline history found yet")
        print(f"[history] (expected at s3://{S3_BUCKET}/{PIPELINE_HISTORY_KEY})")
        return

    if not history:
        print("[history] history is empty")
        return

    print(f"\n{'Timestamp':<20} {'Batch':<35} {'Commit':<14} {'Resolved':>10} {'Rate':>7} {'Cost':>8}")
    print("─" * 100)
    for r in history:
        ts = r.get("timestamp", "?")[:16]
        batch = r.get("batch_name", "?")[:34]
        commit = r.get("git_commit", "?")[:12]
        resolved = r.get("resolved", -1)
        total = r.get("total", -1)
        rate = r.get("resolve_rate")
        cost = r.get("cost_usd", 0)
        res_str = f"{resolved}/{total}" if resolved >= 0 else "?"
        rate_str = f"{rate*100:.1f}%" if rate is not None else "?"
        cost_str = f"${cost:.2f}"
        print(f"{ts:<20} {batch:<35} {commit:<14} {res_str:>10} {rate_str:>7} {cost_str:>8}")
    print()


# ── backfill ───────────────────────────────────────────────────────────────────

# All production batch prefixes to backfill (chronological order)
_BACKFILL_BATCHES = [
    "batch-p8-verified-b1",
    "batch-p9-admission-b1",
    "batch-p10-scan-fixed",
    "batch-p11-gd8",
    "batch-p11-gov-pack",
    "batch-p12-gd9",
    "batch-p13-gd9b",
    "batch-p14-gd10",
    "batch-p15-ylite",
    "batch-p16-ylite",
    "batch-p18-attempt-terminal",
    "batch-p19-bugABC",
    "batch-p20",
    "batch-p21",
    "batch-p22",
    "batch-p25-b2",
    "batch-p25-b3",
    "batch-p25-b4",
    "batch-p25-b5",
    "batch-p25-b6",
    "batch-p25-b7",
    "batch-p25-b8",
    "batch-p25-b10",
]

# Eval results known from CloudWatch (batch → resolved/total)
_KNOWN_EVAL_RESULTS: dict[str, tuple[int, int]] = {
    "batch-p11-gov-pack": (17, 30),
    "batch-p25-b10": (17, 30),
}


def _parse_traj_for_accepted(traj: dict) -> tuple[bool, int, float, int]:
    """
    Parse a traj.json to determine (accepted, attempt_count, cost_usd, api_calls).
    accepted = True if exit_status is 'submitted' (case-insensitive).
    attempt_count = number of attempt_* keys in traj or info section.
    cost_usd = from info.model_stats.instance_cost.
    api_calls = from info.model_stats.api_calls.
    """
    exit_status = traj.get("exit_status", "")
    # Also check nested info dict
    if not exit_status:
        exit_status = traj.get("info", {}).get("exit_status", "")
    accepted = str(exit_status).lower() == "submitted"

    # Count attempts from traj keys like attempt_1, attempt_2 ...
    attempt_count = sum(1 for k in traj if re.match(r"^attempt_\d+$", k))
    if attempt_count == 0:
        attempt_count = 1  # at minimum 1 attempt ran if traj exists

    # Cost and API calls from model_stats
    ms = traj.get("info", {}).get("model_stats", {})
    cost_usd = ms.get("instance_cost", 0.0)
    api_calls = ms.get("api_calls", 0)

    return accepted, attempt_count, cost_usd, api_calls


def _get_run_report_meta(s3, batch_name: str) -> dict:
    """Read commit + cost from run_report.json for a batch. Returns {} on miss."""
    try:
        resp = s3.get_object(Bucket=S3_BUCKET, Key=f"{batch_name}/run_report.json")
        report = json.loads(resp["Body"].read())
        commit = report.get("execution_identity", {}).get("git_commit", "unknown")[:12]
        cost = report.get("model_usage", {}).get("total_cost_usd", 0.0)
        return {"git_commit": commit, "cost_usd": cost}
    except Exception:
        return {}


def _write_instance_record(s3, instance_id: str, record: dict) -> None:
    key = f"{INSTANCE_RECORDS_PREFIX}/{instance_id}.json"
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=json.dumps(record, indent=2).encode(),
        ContentType="application/json",
    )


def _read_instance_record(s3, instance_id: str) -> dict:
    key = f"{INSTANCE_RECORDS_PREFIX}/{instance_id}.json"
    try:
        resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
        return json.loads(resp["Body"].read())
    except Exception:
        return {}


def cmd_backfill(args) -> None:
    """
    Backfill per-instance records from all historical batches.

    For each batch, reads traj files to determine accepted/attempt_count,
    then writes/merges into pipeline-results/instances/<instance_id>.json.
    Eval resolved info is applied from known results (_KNOWN_EVAL_RESULTS).
    """
    s3 = boto3.client("s3", region_name=REGION)
    dry_run = getattr(args, "dry_run", False)

    batches = getattr(args, "batches", None) or _BACKFILL_BATCHES
    print(f"[backfill] processing {len(batches)} batches...", flush=True)
    if dry_run:
        print("[backfill] DRY RUN — no writes", flush=True)

    # instance_id → merged record (built across all batches)
    records: dict[str, dict] = {}

    for batch_name in batches:
        meta = _get_run_report_meta(s3, batch_name)
        git_commit = meta.get("git_commit", "unknown")
        cost_usd = meta.get("cost_usd", 0.0)

        # Load predictions.jsonl to get the instance list for this batch
        try:
            obj = s3.get_object(Bucket=S3_BUCKET, Key=f"{batch_name}/jingu-predictions.jsonl")
            pred_lines = obj["Body"].read().decode().strip().split("\n")
            batch_instances = []
            for line in pred_lines:
                if line.strip():
                    d = json.loads(line)
                    iid = d.get("instance_id", "")
                    if iid:
                        batch_instances.append(iid)
        except Exception as e:
            print(f"[backfill] {batch_name}: no predictions.jsonl ({e})", flush=True)
            continue

        print(f"[backfill] {batch_name}: {len(batch_instances)} instances  commit={git_commit}", flush=True)

        # Determine per-instance resolved status for this batch
        eval_resolved_set: set[str] = set()
        eval_unresolved_set: set[str] = set()
        eval_known = _KNOWN_EVAL_RESULTS.get(batch_name)

        # Try reading eval_results.json from S3 (per-instance resolved data)
        for eval_prefix in [f"eval-{batch_name}", batch_name]:
            eval_data = _read_eval_results_from_s3(eval_prefix)
            if eval_data:
                eval_resolved_set = set(eval_data.get("resolved_ids", []))
                eval_unresolved_set = set(eval_data.get("unresolved_ids", []))
                print(f"[backfill]   eval_results: {len(eval_resolved_set)} resolved, "
                      f"{len(eval_unresolved_set)} unresolved", flush=True)
                break

        # Load traj files for this batch
        traj_data: dict[str, tuple[bool, int, float, int]] = {}  # iid → (accepted, attempts, cost, calls)
        paginator = s3.get_paginator("list_objects_v2")
        for attempt_prefix_page in paginator.paginate(
            Bucket=S3_BUCKET, Prefix=f"{batch_name}/attempt_1/"
        ):
            for obj_meta in attempt_prefix_page.get("Contents", []):
                key = obj_meta["Key"]
                if not key.endswith(".traj.json"):
                    continue
                # Path: <batch>/attempt_1/<instance_id>/<instance_id>.traj.json
                parts = key.split("/")
                if len(parts) < 4:
                    continue
                iid = parts[2]  # <instance_id>
                try:
                    traj_obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
                    traj = json.loads(traj_obj["Body"].read())
                    accepted, attempts, cost, calls = _parse_traj_for_accepted(traj)
                    traj_data[iid] = (accepted, attempts, cost, calls)
                except Exception as e:
                    print(f"[backfill]   warning: could not read traj for {iid}: {e}", flush=True)

        # Merge into per-instance records
        for iid in batch_instances:
            accepted, attempts, inst_cost, inst_calls = traj_data.get(iid, (False, 1, 0.0, 0))
            if eval_resolved_set or eval_unresolved_set:
                if iid in eval_resolved_set:
                    eval_resolved = True
                elif iid in eval_unresolved_set:
                    eval_resolved = False
                else:
                    eval_resolved = None  # not in eval output
            else:
                eval_resolved = None

            run_entry = {
                "batch": batch_name,
                "git_commit": git_commit,
                "accepted": accepted,
                "attempts": attempts,
                "eval_resolved": eval_resolved,
                "cost_usd": round(inst_cost, 4),
                "api_calls": inst_calls,
            }

            if iid not in records:
                records[iid] = {
                    "instance_id": iid,
                    "last_batch": batch_name,
                    "last_commit": git_commit,
                    "accepted": accepted,
                    "eval_resolved": eval_resolved,
                    "runs": [run_entry],
                }
            else:
                # Merge: add this run, update last_batch
                records[iid]["runs"].append(run_entry)
                records[iid]["last_batch"] = batch_name
                records[iid]["last_commit"] = git_commit
                # accepted = True if any run was accepted
                if accepted:
                    records[iid]["accepted"] = True
                # eval_resolved: use latest known value
                if eval_resolved is not None:
                    records[iid]["eval_resolved"] = eval_resolved

    # Apply known eval results (batch-level → per-instance, approximate)
    # For batches where we know resolved/total, mark accepted instances as resolved
    # Note: without per-instance eval data we can only say "batch resolved N/30"
    # We store batch-level eval on the record for known batches
    for batch_name, (resolved, total) in _KNOWN_EVAL_RESULTS.items():
        for iid, record in records.items():
            for run in record["runs"]:
                if run["batch"] == batch_name:
                    run["batch_eval_resolved"] = resolved
                    run["batch_eval_total"] = total
            # Store on top-level for last known eval
            if record["last_batch"] == batch_name:
                record["batch_eval_resolved"] = resolved
                record["batch_eval_total"] = total

    # Write to S3
    print(f"\n[backfill] writing {len(records)} instance records...", flush=True)
    written = 0
    for iid, record in records.items():
        if not dry_run:
            _write_instance_record(s3, iid, record)
        written += 1
        if written % 10 == 0:
            print(f"[backfill]   {written}/{len(records)}...", flush=True)

    print(f"[backfill] done. wrote {written} records to s3://{S3_BUCKET}/{INSTANCE_RECORDS_PREFIX}/", flush=True)

    # Print summary
    total_accepted = sum(1 for r in records.values() if r.get("accepted"))
    total_runs = sum(len(r["runs"]) for r in records.values())
    print(f"\n[backfill] Summary:", flush=True)
    print(f"  unique instances: {len(records)}", flush=True)
    print(f"  total runs:       {total_runs}", flush=True)
    print(f"  ever accepted:    {total_accepted}/{len(records)}", flush=True)


def cmd_summary(args) -> None:
    """
    Show per-instance summary table grouped by repo.
    Reads from pipeline-results/instances/ in S3.
    """
    s3 = boto3.client("s3", region_name=REGION)

    # Load all instance records
    records = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=f"{INSTANCE_RECORDS_PREFIX}/"):
        for obj_meta in page.get("Contents", []):
            key = obj_meta["Key"]
            if not key.endswith(".json"):
                continue
            try:
                resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
                r = json.loads(resp["Body"].read())
                records[r["instance_id"]] = r
            except Exception:
                pass

    if not records:
        print("[summary] no instance records found. Run: python scripts/ops.py backfill")
        return

    # Group by repo prefix (e.g. django__django-10097 → django__django)
    from collections import defaultdict
    repo_stats: dict[str, dict] = defaultdict(lambda: {
        "ran": 0, "accepted": 0, "not_accepted": 0,
        "eval_resolved": 0, "eval_not_resolved": 0, "eval_unknown": 0,
        "total_runs": 0, "total_cost": 0.0,
    })

    for iid, r in records.items():
        # repo = everything before the last hyphen+digits
        repo = re.sub(r"-\d+$", "", iid)
        stats = repo_stats[repo]
        stats["ran"] += 1
        runs = r.get("runs", [])
        stats["total_runs"] += len(runs)
        for run in runs:
            stats["total_cost"] += run.get("cost_usd", 0.0)
        if r.get("accepted"):
            stats["accepted"] += 1
        else:
            stats["not_accepted"] += 1
        eval_r = r.get("eval_resolved")
        if eval_r is True:
            stats["eval_resolved"] += 1
        elif eval_r is False:
            stats["eval_not_resolved"] += 1
        else:
            stats["eval_unknown"] += 1

    print(f"\n{'Repo':<35} {'Inst':>5} {'Runs':>5} {'Acc':>5} {'!Acc':>5} "
          f"{'Res':>5} {'!Res':>5} {'?':>3} {'Cost':>8}", flush=True)
    print("─" * 82, flush=True)
    total_ran = total_acc = total_not = total_res = total_nres = total_unk = 0
    total_runs = 0
    total_cost = 0.0
    for repo, s in sorted(repo_stats.items(), key=lambda x: -x[1]["ran"]):
        print(f"{repo:<35} {s['ran']:>5} {s['total_runs']:>5} {s['accepted']:>5} {s['not_accepted']:>5} "
              f"{s['eval_resolved']:>5} {s['eval_not_resolved']:>5} {s['eval_unknown']:>3} "
              f"${s['total_cost']:>7.1f}")
        total_ran += s["ran"]
        total_runs += s["total_runs"]
        total_acc += s["accepted"]
        total_not += s["not_accepted"]
        total_res += s["eval_resolved"]
        total_nres += s["eval_not_resolved"]
        total_unk += s["eval_unknown"]
        total_cost += s["total_cost"]
    print("─" * 82)
    print(f"{'TOTAL':<35} {total_ran:>5} {total_runs:>5} {total_acc:>5} {total_not:>5} "
          f"{total_res:>5} {total_nres:>5} {total_unk:>3} "
          f"${total_cost:>7.1f}")
    print(f"\n[summary] {len(records)} unique instances across {len(repo_stats)} repos, "
          f"{total_runs} total runs, ${total_cost:.2f} total cost")
    print(f"[summary] columns: Inst=unique instances, Runs=total runs, Acc=accepted, !Acc=not accepted, "
          f"Res=eval resolved, !Res=not resolved, ?=unknown, Cost=total USD")
    print()


# ── discover ──────────────────────────────────────────────────────────────────

def cmd_discover(args) -> None:
    """
    Scan S3 bucket for all batches that have jingu-predictions.jsonl.
    Show which ones already have eval results and which ones don't.
    Filter by min size (to skip empty/single-instance smoke tests).
    """
    s3 = boto3.client("s3", region_name=REGION)
    min_size = args.min_size
    only_unevaluated = args.unevaluated

    # 1. Find all predictions files
    paginator = s3.get_paginator("list_objects_v2")
    predictions: list[dict] = []
    for page in paginator.paginate(Bucket=S3_BUCKET):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("predictions.jsonl"):
                predictions.append({
                    "key": key,
                    "size": obj["Size"],
                    "modified": str(obj["LastModified"]),
                    "batch": key.split("/")[0],
                })

    # 2. Filter by size
    predictions = [p for p in predictions if p["size"] >= min_size]
    predictions.sort(key=lambda p: p["modified"])

    # 3. Check which have eval_results.json
    eval_done: set[str] = set()
    for page in paginator.paginate(Bucket=S3_BUCKET):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("eval_results.json"):
                # eval-<batch>/eval_results.json → batch
                batch = key.split("/")[0]
                eval_done.add(batch)
                if batch.startswith("eval-"):
                    eval_done.add(batch[5:])  # also mark the batch itself

    # 4. Count instances per predictions file
    print(f"\n{'Batch':<50} {'Inst':>5} {'Size':>8} {'Eval':>6} {'Date':<20}")
    print("─" * 95)
    unevaluated = []
    for p in predictions:
        batch = p["batch"]
        # Count instances by reading the file
        try:
            resp = s3.get_object(Bucket=S3_BUCKET, Key=p["key"])
            lines = resp["Body"].read().decode().strip().split("\n")
            inst_count = sum(1 for l in lines if l.strip())
        except Exception:
            inst_count = -1

        has_eval = batch in eval_done or f"eval-{batch}" in eval_done
        eval_str = "yes" if has_eval else "NO"
        date_str = p["modified"][:16]

        if only_unevaluated and has_eval:
            continue

        if not has_eval:
            unevaluated.append({"batch": batch, "key": p["key"], "instances": inst_count})

        print(f"{batch:<50} {inst_count:>5} {p['size']:>8} {eval_str:>6} {date_str:<20}")

    print(f"\n[discover] {len(predictions)} predictions files (>= {min_size} bytes)")
    print(f"[discover] {len(unevaluated)} unevaluated batches")

    if unevaluated and not only_unevaluated:
        print(f"\n[discover] unevaluated batch names (for --eval-only pipeline):")
        for u in unevaluated:
            if u["instances"] >= 3:  # skip tiny smoke tests
                print(f"  {u['batch']}  ({u['instances']} instances)")


# ── eval-all ──────────────────────────────────────────────────────────────────

def _discover_unevaluated(min_size: int = 2000) -> list[dict]:
    """Return list of unevaluated batches: [{batch, key, instances, size, modified}]."""
    s3 = boto3.client("s3", region_name=REGION)
    paginator = s3.get_paginator("list_objects_v2")

    predictions: list[dict] = []
    for page in paginator.paginate(Bucket=S3_BUCKET):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("predictions.jsonl"):
                predictions.append({
                    "key": key, "size": obj["Size"],
                    "modified": str(obj["LastModified"]),
                    "batch": key.split("/")[0],
                })

    predictions = [p for p in predictions if p["size"] >= min_size]
    predictions.sort(key=lambda p: p["modified"])

    eval_done: set[str] = set()
    for page in paginator.paginate(Bucket=S3_BUCKET):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("eval_results.json"):
                batch = key.split("/")[0]
                eval_done.add(batch)
                if batch.startswith("eval-"):
                    eval_done.add(batch[5:])

    unevaluated = []
    for p in predictions:
        batch = p["batch"]
        has_eval = batch in eval_done or f"eval-{batch}" in eval_done
        if has_eval:
            continue
        try:
            resp = s3.get_object(Bucket=S3_BUCKET, Key=p["key"])
            lines = resp["Body"].read().decode().strip().split("\n")
            inst_count = sum(1 for l in lines if l.strip())
        except Exception:
            inst_count = -1
        if inst_count >= 3:
            unevaluated.append({
                "batch": batch, "key": p["key"], "instances": inst_count,
                "size": p["size"], "modified": p["modified"],
            })
    return unevaluated


def _run_single_eval(batch_name: str, eval_workers: int = 4) -> dict:
    """Run eval for a single batch. Returns result dict with resolved/total/error."""
    eval_run_id = f"eval-{batch_name}"
    predictions_key = f"{batch_name}/jingu-predictions.jsonl"
    timestamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    s3_check = boto3.client("s3", region_name=REGION)
    try:
        resp = s3_check.get_object(Bucket=S3_BUCKET, Key=predictions_key)
        pred_lines = resp["Body"].read().decode().strip().split("\n")
        instance_ids = [json.loads(l).get("instance_id", "") for l in pred_lines if l.strip()]
    except Exception as e:
        return {"batch": batch_name, "error": str(e)}

    git_commit = _get_git_commit_from_s3(batch_name)
    cost_usd = _get_cost_from_s3(batch_name)

    print(f"\n[eval-all] ── {batch_name} ({len(instance_ids)} instances) ──", flush=True)

    try:
        eval_task_id = _launch_eval_task(predictions_key, eval_run_id, workers=eval_workers)
    except SystemExit:
        return {"batch": batch_name, "error": "failed to launch ECS task"}

    print(f"[eval-all] task_id={eval_task_id}", flush=True)
    eval_task = _wait_for_task(eval_task_id, "eval", poll_interval=30, timeout_s=7200)
    eval_exit = eval_task.get("containers", [{}])[0].get("exitCode", 1)

    resolved = total = -1
    resolved_ids: list[str] = []
    unresolved_ids: list[str] = []

    if eval_exit == 0:
        eval_data = _read_eval_results_from_s3(eval_run_id)
        if eval_data:
            resolved_ids = eval_data.get("resolved_ids", [])
            unresolved_ids = eval_data.get("unresolved_ids", [])
            resolved = len(resolved_ids)
            total = resolved + len(unresolved_ids)
        else:
            resolved, total = _parse_resolved_from_cw(eval_task_id)

    if total <= 0:
        total = len(instance_ids)

    # Write per-instance records
    s3_client = boto3.client("s3", region_name=REGION)
    resolved_set = set(resolved_ids)
    unresolved_set = set(unresolved_ids)

    for iid in instance_ids:
        existing = _read_instance_record(s3_client, iid)
        if iid in resolved_set:
            eval_resolved = True
        elif iid in unresolved_set:
            eval_resolved = False
        else:
            eval_resolved = None

        run_entry = {
            "batch": batch_name, "git_commit": git_commit,
            "accepted": True, "eval_resolved": eval_resolved,
        }
        if existing:
            existing.setdefault("runs", []).append(run_entry)
            existing["last_batch"] = batch_name
            existing["last_commit"] = git_commit
            existing["accepted"] = True
            if eval_resolved is not None:
                existing["eval_resolved"] = eval_resolved
            _write_instance_record(s3_client, iid, existing)
        else:
            _write_instance_record(s3_client, iid, {
                "instance_id": iid, "last_batch": batch_name,
                "last_commit": git_commit, "accepted": True,
                "eval_resolved": eval_resolved, "runs": [run_entry],
            })

    # Store pipeline history
    record = {
        "timestamp": timestamp, "batch_name": batch_name,
        "git_commit": git_commit, "resolved": resolved, "total": total,
        "resolve_rate": round(resolved / total, 4) if resolved >= 0 and total > 0 else None,
        "cost_usd": cost_usd, "max_attempts": 2,
        "smoke_task_id": None, "batch_task_id": None,
        "eval_task_id": eval_task_id, "eval_exit": eval_exit,
        "resolved_ids": resolved_ids, "eval_only": True,
    }
    _append_pipeline_history(record)

    rate_str = f"{resolved}/{total} ({100*resolved/total:.1f}%)" if resolved >= 0 and total > 0 else "unknown"
    print(f"[eval-all] {batch_name}: {rate_str}", flush=True)

    return {
        "batch": batch_name, "resolved": resolved, "total": total,
        "rate": rate_str, "eval_exit": eval_exit,
    }


def cmd_eval_all(args) -> None:
    """Discover all unevaluated batches and run eval-only pipeline for each sequentially."""
    min_size = args.min_size
    eval_workers = args.eval_workers
    max_batches = args.max_batches

    print("[eval-all] discovering unevaluated batches...", flush=True)
    batches = _discover_unevaluated(min_size=min_size)

    if not batches:
        print("[eval-all] no unevaluated batches found", flush=True)
        return

    if max_batches and len(batches) > max_batches:
        print(f"[eval-all] found {len(batches)} batches, limiting to {max_batches}", flush=True)
        batches = batches[:max_batches]

    print(f"[eval-all] {len(batches)} batches to eval:", flush=True)
    for b in batches:
        print(f"  {b['batch']}  ({b['instances']} instances)", flush=True)
    print(flush=True)

    results = []
    for i, b in enumerate(batches):
        print(f"\n[eval-all] ═══ [{i+1}/{len(batches)}] {b['batch']} ═══", flush=True)
        result = _run_single_eval(b["batch"], eval_workers=eval_workers)
        results.append(result)

    # Summary
    print(f"\n[eval-all] ══════════════════════════════════════════", flush=True)
    print(f"[eval-all] SUMMARY ({len(results)} batches evaluated):", flush=True)
    for r in results:
        if "error" in r:
            print(f"  {r['batch']}: ERROR — {r['error']}", flush=True)
        else:
            print(f"  {r['batch']}: {r.get('rate', '?')}", flush=True)
    print(f"[eval-all] ══════════════════════════════════════════", flush=True)


# ── peek ──────────────────────────────────────────────────────────────────────

_PEEK_SIGNALS = [
    "[phase_record]", "[principal_gate]", "[principal_inference]", "[phase_injection]",
    "[cp-step]", "[cp] ", "[inner-verify]", "DONE", "FAILED", "ACCEPTED", "REJECTED",
    "[step ", "[jingu]", "pee:True", "[verify_gate]", "[init]", "Traceback", "Error",
    "STOPPED", "[attempt ", "[preflight]", "ModuleNotFoundError", "[smoke]",
    "[pipeline]", "resolved", "BUILD_DONE", "[eval-heartbeat]", "[entrypoint]",
    "[limit-triggered]", "[BUNDLE_ACTIVATED]", "[BUNDLE_LOAD_FAILURE]", "FORCE_PASS",
    "[quick-judge]",
]


def cmd_peek(args) -> None:
    """
    Auto-polling CloudWatch signal log viewer.

    Polls every --interval seconds, prints only signal lines (jingu events,
    errors, phase records, etc.), stops when task reaches STOPPED.

    Use --once for a single snapshot instead of polling.
    """
    task_id = args.task_id
    interval = args.interval
    once = args.once
    show_all = args.all
    max_rounds = args.max_rounds

    logs_client = boto3.client("logs", region_name=REGION)
    ecs = boto3.client("ecs", region_name=REGION)
    stream = f"runner/runner/{task_id}"

    # Check task exists
    try:
        t_resp = ecs.describe_tasks(cluster=ECS_CLUSTER, tasks=[task_id])
        tasks = t_resp.get("tasks", [])
        if tasks:
            status = tasks[0].get("lastStatus", "?")
            print(f"[peek] task={task_id}  status={status}", flush=True)
        else:
            print(f"[peek] task={task_id}  (not found in ECS — may have expired)", flush=True)
    except Exception:
        print(f"[peek] task={task_id}", flush=True)

    print(f"[peek] interval={interval}s  max_rounds={max_rounds}  {'once' if once else 'polling'}", flush=True)
    print("─" * 70, flush=True)

    # Initial read: get all events from start
    token = None
    try:
        resp = logs_client.get_log_events(
            logGroupName=LOG_GROUP, logStreamName=stream,
            limit=500, startFromHead=True,
        )
        events = resp.get("events", [])
        token = resp.get("nextForwardToken")

        for e in events:
            for line in e["message"].split("\t"):
                line = line.strip()
                if not line:
                    continue
                if show_all or any(s in line for s in _PEEK_SIGNALS):
                    print(line[:200], flush=True)

        print(f"─── initial: {len(events)} events ───", flush=True)
    except Exception as e:
        print(f"[peek] log stream not available yet: {e}", flush=True)

    if once:
        return

    # Polling loop
    for rnd in range(max_rounds):
        time.sleep(interval)

        # Check task status
        task_status = "?"
        try:
            t_resp = ecs.describe_tasks(cluster=ECS_CLUSTER, tasks=[task_id])
            tasks = t_resp.get("tasks", [])
            if tasks:
                task_status = tasks[0].get("lastStatus", "?")
        except Exception:
            pass

        # Read new events
        new_count = 0
        try:
            kwargs = dict(
                logGroupName=LOG_GROUP, logStreamName=stream,
                limit=500, startFromHead=False,
            )
            if token:
                kwargs["nextToken"] = token
            resp = logs_client.get_log_events(**kwargs)
            new_events = resp.get("events", [])
            new_token = resp.get("nextForwardToken")

            for e in new_events:
                for line in e["message"].split("\t"):
                    line = line.strip()
                    if not line:
                        continue
                    if show_all or any(s in line for s in _PEEK_SIGNALS):
                        print(line[:200], flush=True)
                        new_count += 1

            if new_token:
                token = new_token
        except Exception:
            pass

        print(f"─── round {rnd + 1}/{max_rounds}: +{new_count} signals, status={task_status} ───", flush=True)

        if task_status == "STOPPED":
            # Print final exit code
            try:
                t = tasks[0] if tasks else {}
                exit_code = t.get("containers", [{}])[0].get("exitCode", "?")
                reason = t.get("stoppedReason", "")
                print(f"[peek] STOPPED  exit={exit_code}  reason={reason}", flush=True)
            except Exception:
                pass
            break

    print("─" * 70, flush=True)


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
    p_run.add_argument("--max-attempts", type=int, default=1)
    p_run.add_argument("--workers", type=int, default=3)
    p_run.add_argument("--dataset", default="Verified", choices=["Lite", "Verified"])
    p_run.add_argument("--s3-upload", action="store_true", default=True)
    p_run.add_argument("--model", default=None,
                       help="Override JINGU_MODEL env var (e.g. bedrock/global.anthropic.claude-opus-4-5-20251101-v1:0)")
    p_run.add_argument("--confirmed", action="store_true",
                       help=f"Required when launching more than {BATCH_GUARD_THRESHOLD} instances (batch guard)")
    p_run.add_argument("--runbook-ack", action="store_true",
                       help=f"Required for all launches: confirms runbook ({_RUNBOOK_PATH}) was read this session")
    p_run.add_argument("--env", nargs="+", default=None,
                       help="Extra env vars as KEY=VALUE (e.g. --env STRUCTURED_OUTPUT_ENABLED=true)")

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
    p_smoke.add_argument("--max-attempts", type=int, default=1)
    p_smoke.add_argument("--workers", type=int, default=3)
    p_smoke.add_argument("--dataset", default="Verified", choices=["Lite", "Verified"])
    p_smoke.add_argument("--filter", "-f", default=None,
                         help="Regex filter (overrides default). E.g. 'cp-step|control-plane|pee'")
    p_smoke.add_argument("--all", "-a", action="store_true",
                         help="Show all lines including noise (no filter)")
    p_smoke.add_argument("--model", default=None,
                         help="Override JINGU_MODEL env var (e.g. bedrock/global.anthropic.claude-opus-4-5-20251101-v1:0)")
    p_smoke.add_argument("--confirmed", action="store_true",
                         help=f"Required when launching more than {BATCH_GUARD_THRESHOLD} instances (batch guard)")
    p_smoke.add_argument("--runbook-ack", action="store_true",
                         help=f"Required for all launches: confirms runbook ({_RUNBOOK_PATH}) was read this session")
    p_smoke.add_argument("--env", nargs="+", default=None,
                         help="Extra env vars as KEY=VALUE (e.g. --env STRUCTURED_OUTPUT_ENABLED=true)")
    p_smoke.add_argument("--skip-eval", action="store_true",
                         help="Skip auto-eval after smoke task completes")

    # eval — run SWE-bench evaluation on predictions via ECS
    p_eval = sub.add_parser("eval", help="Run SWE-bench eval on S3 predictions via ECS")
    p_eval.add_argument("--predictions-path", required=True,
                        help="S3 key or s3://bucket/key of predictions.jsonl")
    p_eval.add_argument("--run-id", required=True,
                        help="Eval run ID (used for report naming)")
    p_eval.add_argument("--workers", type=int, default=4)
    p_eval.add_argument("--dataset", default="SWE-bench/SWE-bench_Verified")
    p_eval.add_argument("--runbook-ack", action="store_true",
                        help="Required: confirms runbook was read this session")

    # watch — real-time log tail for a batch or single instance
    p_watch = sub.add_parser("watch", help="Real-time log tail for a batch or instance")
    p_watch.add_argument("--batch-name", default=None, help="Batch name (looks up task_id from S3)")
    p_watch.add_argument("--task-id", default=None, help="ECS task ID (direct attach)")
    p_watch.add_argument("--instance-id", default=None, help="Focus on a single instance")
    p_watch.add_argument("--filter", "-f", default=None, help="Custom regex filter")
    p_watch.add_argument("--all", "-a", action="store_true", help="Show all lines (no filter)")

    # pipeline — full automated pipeline: smoke → batch → eval → store results
    p_pipeline = sub.add_parser("pipeline", help="Full pipeline: smoke → batch → eval → store results")
    p_pipeline.add_argument("--batch-name", required=True,
                            help="Name for this pipeline run (used for batch and eval IDs)")
    p_pipeline.add_argument("--smoke-instance", default="django__django-11095",
                            help="Instance to use for smoke test (default: django__django-11095)")
    p_pipeline.add_argument("--instance-ids", nargs="+", default=None,
                            help="Instances for batch run (default: 30 standard django instances)")
    p_pipeline.add_argument("--max-attempts", type=int, default=2)
    p_pipeline.add_argument("--workers", type=int, default=10)
    p_pipeline.add_argument("--model", default=None)
    p_pipeline.add_argument("--skip-smoke", action="store_true",
                            help="Skip smoke test and go directly to batch")
    p_pipeline.add_argument("--eval-only", action="store_true",
                            help="Skip smoke + batch; eval existing S3 predictions only")
    p_pipeline.add_argument("--eval-workers", type=int, default=4,
                            help="Number of workers for eval task (default: 4)")
    p_pipeline.add_argument("--runbook-ack", action="store_true",
                            help="Required: confirms runbook was read this session")

    # history — show pipeline run history
    sub.add_parser("history", help="Show pipeline run history (resolved rates)")

    # backfill — populate per-instance records from all historical batches
    p_backfill = sub.add_parser("backfill", help="Backfill per-instance records from historical batches")
    p_backfill.add_argument("--dry-run", action="store_true", help="Parse only, don't write to S3")
    p_backfill.add_argument("--batches", nargs="+", default=None, help="Specific batch prefixes to process")

    # summary — per-instance summary table grouped by repo
    sub.add_parser("summary", help="Show per-instance summary table grouped by repo")

    # eval-all — discover + eval all unevaluated batches sequentially
    p_eval_all = sub.add_parser("eval-all", help="Eval all unevaluated batches sequentially")
    p_eval_all.add_argument("--min-size", type=int, default=2000,
                            help="Min predictions file size in bytes (default: 2000)")
    p_eval_all.add_argument("--eval-workers", type=int, default=4,
                            help="Number of eval workers per batch (default: 4)")
    p_eval_all.add_argument("--max-batches", type=int, default=None,
                            help="Max number of batches to eval (default: all)")
    p_eval_all.add_argument("--runbook-ack", action="store_true",
                            help="Required: confirms runbook was read this session")

    # discover — scan S3 for all predictions and eval status
    p_discover = sub.add_parser("discover", help="Scan S3 for all predictions and eval status")
    p_discover.add_argument("--min-size", type=int, default=2000,
                            help="Min predictions file size in bytes (default: 2000, filters out empty smoke tests)")
    p_discover.add_argument("--unevaluated", "-u", action="store_true",
                            help="Show only batches without eval results")

    # list-tasks — show currently running/pending ECS tasks
    sub.add_parser("list-tasks", help="List currently RUNNING/PENDING ECS tasks")

    # logs
    p_logs = sub.add_parser("logs", help="Tail ECS task logs")
    p_logs.add_argument("--task-id", required=True)
    p_logs.add_argument("--follow", "-f", action="store_true")

    # status
    p_status = sub.add_parser("status", help="ECS task status")
    p_status.add_argument("--task-id", required=True)

    # peek — auto-polling CloudWatch signal log viewer
    p_peek = sub.add_parser("peek", help="Auto-polling CloudWatch signal logs (replaces jingu_logs.py)")
    p_peek.add_argument("--task-id", required=True, help="ECS task ID")
    p_peek.add_argument("--interval", type=int, default=30,
                        help="Poll interval in seconds (default: 30)")
    p_peek.add_argument("--max-rounds", type=int, default=60,
                        help="Max polling rounds before exit (default: 60 = 30min at 30s interval)")
    p_peek.add_argument("--once", action="store_true",
                        help="Single snapshot, no polling")
    p_peek.add_argument("--all", "-a", action="store_true",
                        help="Show all lines, not just signals")

    args = parser.parse_args()

    if args.cmd == "build":
        cmd_build(args)
    elif args.cmd == "run":
        cmd_run(args)
    elif args.cmd == "smoke":
        cmd_smoke(args)
    elif args.cmd == "eval":
        cmd_eval(args)
    elif args.cmd == "watch":
        cmd_watch(args)
    elif args.cmd == "pipeline":
        _check_runbook_ack(args)
        cmd_pipeline(args)
    elif args.cmd == "history":
        cmd_history(args)
    elif args.cmd == "list-tasks":
        cmd_list_tasks(args)
    elif args.cmd == "logs":
        cmd_logs(args)
    elif args.cmd == "status":
        cmd_status(args)
    elif args.cmd == "backfill":
        cmd_backfill(args)
    elif args.cmd == "summary":
        cmd_summary(args)
    elif args.cmd == "discover":
        cmd_discover(args)
    elif args.cmd == "eval-all":
        _check_runbook_ack(args)
        cmd_eval_all(args)
    elif args.cmd == "peek":
        cmd_peek(args)


if __name__ == "__main__":
    main()
