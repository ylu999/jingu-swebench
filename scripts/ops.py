#!/usr/bin/env python3
"""
ops.py — jingu-swebench operational script.

Subcommands:
  build       Build + push Docker image to ECR via SSM on EC2
  run         Launch ECS batch task
  logs        Tail ECS task logs (CloudWatch)
  status      Show ECS task status

Usage:
  python scripts/ops.py build
  python scripts/ops.py run --instance-ids django__django-11039 django__django-11099 --batch-name b2-smoke --workers 3 --max-attempts 2
  python scripts/ops.py logs --task-id <ecs_task_id>
  python scripts/ops.py status --task-id <ecs_task_id>

Environment:
  AWS_DEFAULT_REGION  (default: us-west-2)
"""

import argparse
import json
import sys
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
    p_run = sub.add_parser("run", help="Launch ECS batch")
    p_run.add_argument("--instance-ids", nargs="+", required=True)
    p_run.add_argument("--batch-name", required=True)
    p_run.add_argument("--mode", default="jingu", choices=["jingu", "baseline"])
    p_run.add_argument("--max-attempts", type=int, default=2)
    p_run.add_argument("--workers", type=int, default=3)
    p_run.add_argument("--s3-upload", action="store_true", default=True)

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
    elif args.cmd == "logs":
        cmd_logs(args)
    elif args.cmd == "status":
        cmd_status(args)


if __name__ == "__main__":
    main()
