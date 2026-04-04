#!/usr/bin/env bash
# smoke-test.sh — launch a smoke test batch and tail logs until task stops.
#
# Usage:
#   ./scripts/smoke-test.sh <batch-name> [instance-ids...]
#
# Examples:
#   ./scripts/smoke-test.sh b3-smoke-$(date +%Y%m%d) django__django-11039 django__django-12470 django__django-10914
#   ./scripts/smoke-test.sh b3-quick django__django-12470   # single instance
#   DATASET=Lite ./scripts/smoke-test.sh b3-quick django__django-11039
#   DATASET=Verified MODE=baseline ./scripts/smoke-test.sh exp-baseline-v1 django__django-11099
#
# Env vars:
#   MAX_ATTEMPTS  default: 2
#   WORKERS       default: number of instances
#   DATASET       default: Verified   (Lite | Verified)
#   MODE          default: jingu      (jingu | baseline)
#
# What it does:
#   1. Launches ECS task via ops.py run
#   2. Polls task status every 10s until RUNNING (up to 3 min)
#   3. Polls log stream every 10s until it appears (up to 2 min)
#   4. Tails logs, filtering out dockerd/containerd noise
#   5. Polls status every 15s; exits when task STOPPED
#   6. Prints final status + exit code
#
# Does NOT block in a sleep loop if the task fails to start.
# Does NOT hang after the task stops.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REGION="us-west-2"
ECS_CLUSTER="jingu-swebench"
LOG_GROUP="/ecs/jingu-swebench"

# ── Args ──────────────────────────────────────────────────────────────────────

if [[ $# -lt 2 ]]; then
    echo "Usage: $0 <batch-name> <instance-id> [instance-id ...]"
    echo "Example: $0 b3-smoke-$(date +%Y%m%d) django__django-11039 django__django-12470"
    exit 1
fi

BATCH_NAME="$1"
shift
INSTANCE_IDS=("$@")
MAX_ATTEMPTS="${MAX_ATTEMPTS:-2}"
WORKERS="${WORKERS:-${#INSTANCE_IDS[@]}}"
DATASET="${DATASET:-Verified}"
MODE="${MODE:-jingu}"

echo "[smoke] batch=$BATCH_NAME instances=${INSTANCE_IDS[*]}"
echo "[smoke] dataset=$DATASET mode=$MODE max_attempts=$MAX_ATTEMPTS workers=$WORKERS"

# ── Step 1: Launch ECS task ───────────────────────────────────────────────────

LAUNCH_OUT=$(python "$SCRIPT_DIR/ops.py" run \
    --instance-ids "${INSTANCE_IDS[@]}" \
    --batch-name "$BATCH_NAME" \
    --dataset "$DATASET" \
    --mode "$MODE" \
    --max-attempts "$MAX_ATTEMPTS" \
    --workers "$WORKERS" 2>&1)

echo "$LAUNCH_OUT"

TASK_ID=$(echo "$LAUNCH_OUT" | grep "ECS task launched:" | awk '{print $NF}')
if [[ -z "$TASK_ID" ]]; then
    echo "[smoke] ERROR: could not parse task ID from launch output"
    exit 1
fi

echo "[smoke] task_id=$TASK_ID"
LOG_STREAM="runner/runner/$TASK_ID"

# ── Step 2: Wait for task RUNNING ─────────────────────────────────────────────

echo "[smoke] waiting for task to reach RUNNING..."
DEADLINE=$((SECONDS + 180))
while [[ $SECONDS -lt $DEADLINE ]]; do
    STATUS=$(aws ecs describe-tasks \
        --region "$REGION" \
        --cluster "$ECS_CLUSTER" \
        --tasks "$TASK_ID" \
        --query 'tasks[0].lastStatus' \
        --output text 2>/dev/null || echo "UNKNOWN")

    if [[ "$STATUS" == "RUNNING" ]]; then
        echo "[smoke] task RUNNING"
        break
    elif [[ "$STATUS" == "STOPPED" ]]; then
        echo "[smoke] task already STOPPED (failed to start?)"
        python "$SCRIPT_DIR/ops.py" status --task-id "$TASK_ID"
        exit 1
    fi

    echo "[smoke] status=$STATUS, waiting..."
    sleep 10
done

# ── Step 3: Wait for log stream ───────────────────────────────────────────────

echo "[smoke] waiting for log stream $LOG_GROUP/$LOG_STREAM..."
DEADLINE=$((SECONDS + 120))
while [[ $SECONDS -lt $DEADLINE ]]; do
    STREAM_EXISTS=$(aws logs describe-log-streams \
        --region "$REGION" \
        --log-group-name "$LOG_GROUP" \
        --log-stream-name-prefix "$LOG_STREAM" \
        --query 'logStreams[0].logStreamName' \
        --output text 2>/dev/null || echo "None")

    if [[ "$STREAM_EXISTS" != "None" && "$STREAM_EXISTS" != "" ]]; then
        echo "[smoke] log stream available"
        break
    fi

    # Check if task died while we were waiting
    STATUS=$(aws ecs describe-tasks \
        --region "$REGION" \
        --cluster "$ECS_CLUSTER" \
        --tasks "$TASK_ID" \
        --query 'tasks[0].lastStatus' \
        --output text 2>/dev/null || echo "UNKNOWN")
    if [[ "$STATUS" == "STOPPED" ]]; then
        echo "[smoke] task STOPPED before log stream appeared — early failure"
        python "$SCRIPT_DIR/ops.py" status --task-id "$TASK_ID"
        exit 1
    fi

    sleep 10
done

# ── Step 4+5: Tail logs, exit when task stops ─────────────────────────────────

echo "[smoke] streaming logs (filtering dockerd noise)..."
echo "------------------------------------------------------------"

NEXT_TOKEN=""
LAST_TS=0

while true; do
    # Poll log events
    if [[ -z "$NEXT_TOKEN" ]]; then
        LOG_RESP=$(aws logs get-log-events \
            --region "$REGION" \
            --log-group-name "$LOG_GROUP" \
            --log-stream-name "$LOG_STREAM" \
            --start-from-head \
            --output json 2>/dev/null || echo '{"events":[],"nextForwardToken":""}')
    else
        LOG_RESP=$(aws logs get-log-events \
            --region "$REGION" \
            --log-group-name "$LOG_GROUP" \
            --log-stream-name "$LOG_STREAM" \
            --start-from-head \
            --next-token "$NEXT_TOKEN" \
            --output json 2>/dev/null || echo '{"events":[],"nextForwardToken":""}')
    fi

    NEW_TOKEN=$(echo "$LOG_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('nextForwardToken',''))" 2>/dev/null || echo "")

    # Print non-dockerd lines
    echo "$LOG_RESP" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for ev in data.get('events', []):
    msg = ev.get('message', '')
    if not msg.startswith('time=') and 'level=' not in msg:
        print(msg)
" 2>/dev/null || true

    # Check task status
    STATUS=$(aws ecs describe-tasks \
        --region "$REGION" \
        --cluster "$ECS_CLUSTER" \
        --tasks "$TASK_ID" \
        --query 'tasks[0].lastStatus' \
        --output text 2>/dev/null || echo "UNKNOWN")

    if [[ "$STATUS" == "STOPPED" && "$NEW_TOKEN" == "$NEXT_TOKEN" ]]; then
        # Task stopped and no new log events — we're done
        break
    fi

    NEXT_TOKEN="$NEW_TOKEN"
    sleep 15
done

echo "------------------------------------------------------------"

# ── Step 6: Final status ──────────────────────────────────────────────────────

echo ""
echo "[smoke] FINAL STATUS:"
python "$SCRIPT_DIR/ops.py" status --task-id "$TASK_ID"

echo ""
echo "[smoke] CONTROL-PLANE SUMMARY:"
# Re-fetch all logs and extract control-plane lines
aws logs get-log-events \
    --region "$REGION" \
    --log-group-name "$LOG_GROUP" \
    --log-stream-name "$LOG_STREAM" \
    --start-from-head \
    --output json 2>/dev/null | python3 -c "
import sys, json
data = json.load(sys.stdin)
for ev in data.get('events', []):
    msg = ev.get('message', '')
    if '[control-plane]' in msg or '[cp-step]' in msg:
        print(msg)
" 2>/dev/null || true
