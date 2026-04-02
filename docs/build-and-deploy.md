# Build and Deploy — jingu-swebench ECS Image

## Prerequisites

The Docker image requires two repos as build context:
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
