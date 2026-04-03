#!/usr/bin/env python3
"""
run_smoke_test.py — launch smoke test on jingu EC2 + stream phase checkpoints locally

Usage:
  python3 scripts/run_smoke_test.py [--instance-id i-xxx] [--instance django__django-11019]

Steps:
  1. Scale up ASG if no instance running
  2. Wait for SSM ready
  3. Launch batch (1 instance, 1 attempt, 1 worker)
  4. Stream phase checkpoints from log via SSM tail loop
  5. Scale down ASG when done
"""
import argparse, boto3, re, sys, time

ECR = "235494812052.dkr.ecr.us-west-2.amazonaws.com/jingu-swebench:latest"
ASG_NAME = "jingu-swebench-ecs-asg"
REGION = "us-west-2"
LOG_PATH = "/root/results/smoke-p179.log"
OUTPUT_DIR = "/app/results/smoke-p179"

CHECKPOINTS = [
    ("A1", "infra",    r"\[preflight\] ALL CHECKS PASSED"),
    ("A2", "infra",    r"\[jingu\] START"),
    ("A3", "infra",    r"\[inner-verify\] container ready"),
    ("B1", "agent",    r"\[step 1\]"),
    ("B2", "agent",    r"\[step [5-9]\]|\[step [12][0-9]\]"),
    ("C1", "progress", r"\[inner-verify\] triggering verify at step="),
    ("D1", "verify",   r"tests_passed=[0-9]+"),
    ("D2", "verify",   r"delta=[+-]?[0-9]+"),
    ("E1", "done",     r"report saved"),
]


def ssm_run(ssm, instance_id, script, timeout=120):
    resp = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName='AWS-RunShellScript',
        Parameters={'commands': [script]},
    )
    cmd_id = resp['Command']['CommandId']
    for _ in range(timeout // 5 + 5):
        time.sleep(5)
        try:
            inv = ssm.get_command_invocation(CommandId=cmd_id, InstanceId=instance_id)
            if inv['Status'] not in ('Pending', 'InProgress'):
                return inv['Status'], inv['StandardOutputContent'], inv['StandardErrorContent']
        except:
            pass
    return 'Timeout', '', ''


def get_or_start_instance():
    ec2 = boto3.client('ec2', region_name=REGION)
    resp = ec2.describe_instances(
        Filters=[
            {'Name': 'tag:aws:autoscaling:groupName', 'Values': [ASG_NAME]},
            {'Name': 'instance-state-name', 'Values': ['running']}
        ]
    )
    instances = [i for r in resp['Reservations'] for i in r['Instances']]
    if instances:
        iid = instances[0]['InstanceId']
        print(f"[smoke] reusing instance {iid}")
        return iid, False

    print("[smoke] no running instance, scaling up ASG...")
    asg = boto3.client('autoscaling', region_name=REGION)
    asg.set_desired_capacity(AutoScalingGroupName=ASG_NAME, DesiredCapacity=1)
    for _ in range(30):
        time.sleep(10)
        resp = ec2.describe_instances(
            Filters=[
                {'Name': 'tag:aws:autoscaling:groupName', 'Values': [ASG_NAME]},
                {'Name': 'instance-state-name', 'Values': ['running']}
            ]
        )
        instances = [i for r in resp['Reservations'] for i in r['Instances']]
        if instances:
            iid = instances[0]['InstanceId']
            print(f"[smoke]   instance ready: {iid}")
            return iid, True
        print("[smoke]   waiting for instance...")
    raise RuntimeError("Timed out waiting for EC2 instance")


def wait_ssm_ready(ssm, instance_id, retries=10):
    print("[smoke] waiting for SSM agent...")
    for i in range(retries):
        try:
            status, out, _ = ssm_run(ssm, instance_id, "echo SSM_OK", timeout=30)
            if "SSM_OK" in out:
                print("[smoke]   SSM ready")
                return
        except:
            pass
        time.sleep(10)
    raise RuntimeError("SSM agent not ready")


def stream_checkpoints(ssm, instance_id, timeout_s=900):
    seen = set()
    start = time.time()
    print("[smoke] streaming checkpoints...\n")

    while time.time() - start < timeout_s:
        elapsed = int(time.time() - start)
        status, content, _ = ssm_run(ssm, instance_id, f"cat {LOG_PATH} 2>/dev/null || echo ''", timeout=30)

        for cp_id, phase, pattern in CHECKPOINTS:
            if cp_id not in seen and re.search(pattern, content):
                seen.add(cp_id)
                ts = int(time.time() - start)
                print(f"[smoke] {ts:4d}s  phase={phase:<10}  PASS  {cp_id}", flush=True)

        if "E1" in seen:
            elapsed = int(time.time() - start)
            print(f"\n[smoke] ALL CHECKPOINTS PASSED in {elapsed}s")
            return True, seen

        if elapsed > 60 and "A1" not in seen:
            print(f"[smoke] FAIL: preflight not seen after {elapsed}s")
            print(content[-300:] if content else "(empty log)")
            return False, seen

        time.sleep(15)

    elapsed = int(time.time() - start)
    print(f"\n[smoke] TIMEOUT after {elapsed}s")
    missing = [cp for cp, _, _ in CHECKPOINTS if cp not in seen]
    print(f"[smoke] reached={sorted(seen)}  missing={missing}")
    return False, seen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance-id", help="EC2 instance ID (skip ASG lookup)")
    parser.add_argument("--swebench-instance", default="django__django-11019")
    parser.add_argument("--scale-down", action="store_true", help="Scale down ASG when done")
    args = parser.parse_args()

    ssm = boto3.client('ssm', region_name=REGION)

    if args.instance_id:
        instance_id = args.instance_id
        launched = False
    else:
        instance_id, launched = get_or_start_instance()

    wait_ssm_ready(ssm, instance_id)

    # Pull latest image and launch
    launch_script = f"""
mkdir -p /root/results/smoke-p179
aws ecr get-login-password --region {REGION} | docker login --username AWS --password-stdin {ECR.split('/')[0]} 2>/dev/null
docker pull {ECR} 2>&1 | tail -2
nohup docker run --rm --privileged \\
  -v /root/results:/app/results \\
  -e STRATEGY_LOG_PATH=/app/results/strategy_log.jsonl \\
  -e STRATEGY_TABLE_PATH=/app/results/strategy_table.json \\
  {ECR} \\
  --instance-ids {args.swebench_instance} \\
  --max-attempts 1 \\
  --workers 1 \\
  --output {OUTPUT_DIR} \\
  > {LOG_PATH} 2>&1 &
echo "PID: $!"
"""
    print("[smoke] launching batch...")
    status, out, _ = ssm_run(ssm, instance_id, launch_script, timeout=60)
    print(f"[smoke]   {out.strip().splitlines()[-1] if out.strip() else 'launched'}")

    ok, seen = stream_checkpoints(ssm, instance_id)

    if args.scale_down or launched:
        print("[smoke] scaling down ASG...")
        asg = boto3.client('autoscaling', region_name=REGION)
        asg.set_desired_capacity(AutoScalingGroupName=ASG_NAME, DesiredCapacity=0)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
