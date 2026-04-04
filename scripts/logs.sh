#!/usr/bin/env bash
# logs.sh — read CloudWatch logs for a completed or running ECS task.
#
# Usage:
#   ./scripts/logs.sh <task-id> [filter-pattern]
#
# Examples:
#   ./scripts/logs.sh 27aba52bdc27441db709cb9baa76726b
#   ./scripts/logs.sh 27aba52b '[control-plane]'
#   ./scripts/logs.sh 27aba52b 'cp-step|control-plane|STOPPING'
#
# Filter is a grep -E pattern applied to output lines.
# Default: excludes dockerd/containerd noise (time= level= lines).

set -euo pipefail

REGION="us-west-2"
LOG_GROUP="/ecs/jingu-swebench"

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <task-id> [grep-pattern]"
    exit 1
fi

TASK_ID="$1"
FILTER="${2:-}"
LOG_STREAM="runner/runner/$TASK_ID"

# Fetch all pages
NEXT_TOKEN=""
ALL_EVENTS="[]"

while true; do
    if [[ -z "$NEXT_TOKEN" ]]; then
        RESP=$(aws logs get-log-events \
            --region "$REGION" \
            --log-group-name "$LOG_GROUP" \
            --log-stream-name "$LOG_STREAM" \
            --start-from-head \
            --limit 10000 \
            --output json 2>/dev/null)
    else
        RESP=$(aws logs get-log-events \
            --region "$REGION" \
            --log-group-name "$LOG_GROUP" \
            --log-stream-name "$LOG_STREAM" \
            --start-from-head \
            --next-token "$NEXT_TOKEN" \
            --limit 10000 \
            --output json 2>/dev/null)
    fi

    NEW_TOKEN=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('nextForwardToken',''))" 2>/dev/null || echo "")

    # Print non-dockerd lines, applying optional filter
    echo "$RESP" | python3 -c "
import sys, json, re
data = json.load(sys.stdin)
pattern = sys.argv[1] if len(sys.argv) > 1 else ''
for ev in data.get('events', []):
    msg = ev.get('message', '')
    if msg.startswith('time=') or 'level=' in msg:
        continue
    if pattern and not re.search(pattern, msg):
        continue
    print(msg)
" "$FILTER" 2>/dev/null || true

    if [[ "$NEW_TOKEN" == "$NEXT_TOKEN" || -z "$NEW_TOKEN" ]]; then
        break
    fi
    NEXT_TOKEN="$NEW_TOKEN"
done
