# Build and Deploy — jingu-swebench

## Important: Build on EC2, Not Local Mac

Local Docker Desktop is blocked by org policy.
**Always build on EC2 via SSM** using the builder AMI.

---

## Builder AMI

`ami-068cfa06f1b8dd28c` — jingu-swebench-builder-20260402
Pre-installed: git, nodejs 18, npm, docker, aws cli, ssm agent.

---

## Build Steps

### 1. Scale up ASG

```bash
aws autoscaling set-desired-capacity \
  --auto-scaling-group-name jingu-swebench-ecs-asg \
  --desired-capacity 1 --region us-west-2
```

### 2. Get instance ID (wait ~30s)

```bash
aws ec2 describe-instances --region us-west-2 \
  --filters "Name=tag:aws:autoscaling:groupName,Values=jingu-swebench-ecs-asg" \
            "Name=instance-state-name,Values=running" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text
```

### 3. Send build + push script via Python boto3

**Use Python boto3, not AWS CLI** — multiline scripts mangle with `--parameters commands=[...]`.

```python
import boto3, time

INSTANCE_ID = 'i-XXXXXXXXXXXXXXXXX'  # from step 2
ssm = boto3.client('ssm', region_name='us-west-2')

script = """
cd /root/jingu-swebench
git pull origin main
git log --oneline -3

# npm install for jingu-trust-gate (uses node:18-alpine container for glibc compat)
docker run --rm \
  -v /root/jingu-swebench/jingu-trust-gate:/work \
  -w /work node:18-alpine \
  npm install --silent 2>&1 | tail -3

# docker build
GIT_COMMIT=$(git rev-parse HEAD)
BUILD_TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
ECR=235494812052.dkr.ecr.us-west-2.amazonaws.com
IMAGE=$ECR/jingu-swebench:latest

aws ecr get-login-password --region us-west-2 | docker login --username AWS --password-stdin $ECR
docker build --build-arg GIT_COMMIT=$GIT_COMMIT --build-arg BUILD_TIMESTAMP=$BUILD_TIMESTAMP -t $IMAGE . 2>&1 | tail -8
docker push $IMAGE
echo "PUSHED: commit=$GIT_COMMIT timestamp=$BUILD_TIMESTAMP"
"""

resp = ssm.send_command(
    InstanceIds=[INSTANCE_ID],
    DocumentName='AWS-RunShellScript',
    Parameters={'commands': [script]},
    TimeoutSeconds=900,
)
cmd_id = resp['Command']['CommandId']
print('CommandId:', cmd_id)

# Poll for result
for _ in range(60):
    time.sleep(10)
    try:
        inv = ssm.get_command_invocation(CommandId=cmd_id, InstanceId=INSTANCE_ID)
        if inv['Status'] not in ('Pending', 'InProgress'):
            print('Status:', inv['Status'])
            print(inv['StandardOutputContent'][-2000:])
            break
    except Exception:
        pass
```

### 4. Verify image was pushed

```bash
aws ecr describe-images --repository-name jingu-swebench --region us-west-2 \
  --query 'sort_by(imageDetails, &imagePushedAt)[-1].{pushed:imagePushedAt,digest:imageDigest}'
```

The `pushed` timestamp must be after your last relevant git commit.

### 5. Scale ASG back to 0

```bash
aws autoscaling set-desired-capacity \
  --auto-scaling-group-name jingu-swebench-ecs-asg \
  --desired-capacity 0 --region us-west-2
```

---

## Smoke Test Before Any Batch

Always run 1 instance to confirm the image is correct before launching a full batch:

```python
import boto3, time

INSTANCE_ID = 'i-XXXXXXXXXXXXXXXXX'
ssm = boto3.client('ssm', region_name='us-west-2')

script = """
nohup docker run --rm --privileged \
  -v /root/results:/app/results \
  jingu-swebench:latest \
  --instance-ids django__django-11039 \
  --mode jingu --max-attempts 1 --workers 1 \
  --output /app/results/smoke-$(date +%Y%m%d) \
  > /root/results/smoke.log 2>&1 &
echo "PID: $!"
sleep 15
head -20 /root/results/smoke.log
"""

resp = ssm.send_command(
    InstanceIds=[INSTANCE_ID],
    DocumentName='AWS-RunShellScript',
    Parameters={'commands': [script]},
)
```

Check smoke log for: `[preflight] ALL CHECKS PASSED` and `[jingu] START django__django-11039`.

---

## Updating Builder AMI

After installing new system deps on the EC2 instance:

```bash
aws ec2 create-image --instance-id <INSTANCE_ID> \
  --name "jingu-swebench-builder-$(date +%Y%m%d)" \
  --no-reboot --region us-west-2
# Then update memory/MEMORY.md with the new AMI ID
```

---

## Launching a Batch

See [memory/runner-ops.md](../../.claude/projects/-Users-ysl-jingu/memory/runner-ops.md) for the full batch launch template.
