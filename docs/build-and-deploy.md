# Build and Deploy — jingu-swebench ECS Image

## ⚠️ Important: Build on EC2, Not Local Mac

Local Docker Desktop is blocked by org policy (requires [amazonians] org login).
**Always build on EC2** using the builder AMI below.

---

## EC2 Build (Recommended)

### Builder AMI
`ami-068cfa06f1b8dd28c` — jingu-swebench-builder-20260402
Pre-installed: git, nodejs 18, docker, aws cli, ssm agent.

### Steps

```bash
# 1. Scale up ASG to get a build instance
aws autoscaling set-desired-capacity \
  --auto-scaling-group-name jingu-swebench-ecs-asg \
  --desired-capacity 1 --region us-west-2

# 2. Wait for instance + SSM to be ready (~60s)
aws ec2 describe-instances --region us-west-2 \
  --filters "Name=tag:aws:autoscaling:groupName,Values=jingu-swebench-ecs-asg" \
            "Name=instance-state-name,Values=running" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text

# 3. Send build script via SSM (Python boto3 for multiline)
python3 - <<'PYEOF'
import boto3
script = """#!/bin/bash
dnf install -y nodejs npm git 2>&1 | tail -3
rm -rf /tmp/jb /tmp/jt
git clone https://github.com/ylu999/jingu-swebench.git /tmp/jb
git clone https://github.com/ylu999/jingu-trust-gate.git /tmp/jt
cd /tmp/jt && npm install && npm run build
mkdir -p /tmp/jb/jingu-trust-gate
cp -r /tmp/jt/dist /tmp/jt/package.json /tmp/jt/node_modules /tmp/jb/jingu-trust-gate/
ECR=235494812052.dkr.ecr.us-west-2.amazonaws.com
IMAGE=$ECR/jingu-swebench:latest
GIT_COMMIT=$(git -C /tmp/jb rev-parse HEAD)
BUILD_TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
aws ecr get-login-password --region us-west-2 | docker login --username AWS --password-stdin $ECR
cd /tmp/jb && docker build --platform linux/amd64 \\
  --build-arg GIT_COMMIT=$GIT_COMMIT --build-arg BUILD_TIMESTAMP=$BUILD_TIMESTAMP -t $IMAGE .
docker push $IMAGE
echo "PUSHED: commit=$GIT_COMMIT timestamp=$BUILD_TIMESTAMP"
"""
ssm = boto3.client('ssm', region_name='us-west-2')
resp = ssm.send_command(
    InstanceIds=['<INSTANCE_ID>'],  # replace with actual instance ID
    DocumentName='AWS-RunShellScript',
    Parameters={'commands': [script]},
    TimeoutSeconds=900,
)
print(resp['Command']['CommandId'])
PYEOF

# 4. Poll for completion
aws ssm get-command-invocation \
  --command-id <COMMAND_ID> --instance-id <INSTANCE_ID> \
  --region us-west-2 --query 'Status' --output text

# 5. Scale ASG back to 0 (save cost)
aws autoscaling set-desired-capacity \
  --auto-scaling-group-name jingu-swebench-ecs-asg \
  --desired-capacity 0 --region us-west-2
```

**Note:** If builder AMI doesn't have git/nodejs yet, the script installs them via `dnf`.
To create a new builder AMI from the current instance:
```bash
aws ec2 create-image --instance-id <INSTANCE_ID> \
  --name "jingu-swebench-builder-$(date +%Y%m%d)" \
  --no-reboot --region us-west-2
```

---

## Local Build (Reference Only — usually blocked by Docker Desktop org policy)

Prerequisites:
- `jingu-swebench` — main repo (scripts, Dockerfile, entrypoint)
- `jingu-trust-gate` — TypeScript gate, must be built first

## Build Steps

```bash
# 1. Clone/update both repos side by side
cd /tmp
git clone https://github.com/ylu999/jingu-swebench.git jb
git clone https://github.com/ylu999/jingu-trust-gate.git jt

# 2. Build jingu-trust-gate (produces dist/ + node_modules/)
cd jt && npm install && npm run build && cd ..

# 3. Copy trust-gate artifacts into jingu-swebench build context
mkdir -p jb/jingu-trust-gate
cp -r jt/dist jt/package.json jt/node_modules jb/jingu-trust-gate/

# 4. Build and push (with provenance args)
GIT_COMMIT=$(git -C jb rev-parse HEAD)
BUILD_TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
ECR=235494812052.dkr.ecr.us-west-2.amazonaws.com
IMAGE=$ECR/jingu-swebench:latest

cd jb
aws ecr get-login-password --region us-west-2 | docker login --username AWS --password-stdin $ECR
docker build --platform linux/amd64 \
  --build-arg GIT_COMMIT=$GIT_COMMIT \
  --build-arg BUILD_TIMESTAMP=$BUILD_TIMESTAMP \
  -t $IMAGE .
docker push $IMAGE
echo "Pushed: commit=$GIT_COMMIT timestamp=$BUILD_TIMESTAMP"
```

## Verify

```bash
# Check ECR pushed time
aws ecr describe-images --repository-name jingu-swebench --region us-west-2 \
  --query 'sort_by(imageDetails, &imagePushedAt)[-1].{pushed:imagePushedAt,digest:imageDigest}'

# RT1 check: confirm image was pushed AFTER relevant commits
git -C jb log --oneline --since="<pushed_at>"
# Should be empty if image is up to date
```

## RT5 — Smoke Test Before Batch

Always run 1 instance before a multi-instance batch to confirm new behavior is live:

```bash
bash scripts/ecs_launch.sh \
  --run-id smoke-$(date +%Y%m%d) \
  --instances "django__django-11039" \
  --max-attempts 1 \
  --workers 1 \
  --wait
```

Check logs for `[init] git_commit=<expected_sha>` and `[init] declaration_protocol=enabled`.
