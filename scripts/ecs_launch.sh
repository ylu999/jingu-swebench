#!/bin/bash
# ecs_launch.sh — Launch a jingu-swebench batch run on ECS (EC2 via capacity provider)
#
# Usage:
#   bash scripts/ecs_launch.sh --run-id ecs_batch_04 --instances "django__django-11001 django__django-11019" [options]
#
# Required:
#   --run-id <id>        Output prefix in S3 (e.g. ecs_batch_04). Also sets --output /app/results/<id>
#   --instances "<ids>"  Space-separated instance IDs (quote the whole list)
#
# Optional:
#   --max-attempts <N>   Max retry attempts per instance (default: 2)
#   --workers <N>        Parallel workers (default: 4)
#   --stagger <N>        Stagger seconds between worker starts (default: 10)
#   --revision <N>       Task definition revision (default: latest)
#   --region <r>         AWS region (default: us-west-2)
#   --wait               Wait for the task to finish and tail CloudWatch logs
#
# After the run:
#   Results are uploaded to s3://jingu-swebench-results/<run-id>/
#   Download: aws s3 sync s3://jingu-swebench-results/<run-id>/ results/<run-id>/

set -euo pipefail

# ── Defaults ───────────────────────────────────────────────────────────────────
CLUSTER="jingu-swebench"
TASK_DEF="jingu-swebench-runner"
CONTAINER="runner"
REGION="us-west-2"
MAX_ATTEMPTS=2
WORKERS=4
STAGGER=10
REVISION=""
WAIT=false
RUN_ID=""
INSTANCES=""

# ── Arg parsing ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case $1 in
    --run-id)       RUN_ID="$2";       shift 2 ;;
    --instances)    INSTANCES="$2";    shift 2 ;;
    --max-attempts) MAX_ATTEMPTS="$2"; shift 2 ;;
    --workers)      WORKERS="$2";      shift 2 ;;
    --stagger)      STAGGER="$2";      shift 2 ;;
    --revision)     REVISION="$2";     shift 2 ;;
    --region)       REGION="$2";       shift 2 ;;
    --wait)         WAIT=true;         shift ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

# ── Validation ─────────────────────────────────────────────────────────────────
if [[ -z "$RUN_ID" ]]; then
  echo "ERROR: --run-id is required (e.g. ecs_batch_04)" >&2
  exit 1
fi
if [[ -z "$INSTANCES" ]]; then
  echo "ERROR: --instances is required (e.g. 'django__django-11001 django__django-11019')" >&2
  exit 1
fi

# ── Task definition ARN ────────────────────────────────────────────────────────
if [[ -n "$REVISION" ]]; then
  TASK_DEF_ARG="${TASK_DEF}:${REVISION}"
else
  TASK_DEF_ARG="$TASK_DEF"
fi

# ── Build run_with_jingu_gate.py args ─────────────────────────────────────────
# Passed as container command override
CMD_ARGS=(
  "--instance-ids" $INSTANCES
  "--output" "/app/results/${RUN_ID}"
  "--max-attempts" "$MAX_ATTEMPTS"
  "--workers" "$WORKERS"
  "--stagger" "$STAGGER"
)

# Build JSON array for command override
CMD_JSON=$(python3 -c "
import json, sys
args = sys.argv[1:]
print(json.dumps(args))
" -- "${CMD_ARGS[@]}")

OVERRIDES=$(python3 -c "
import json
container = '$CONTAINER'
cmd = $CMD_JSON
overrides = {
    'containerOverrides': [
        {
            'name': container,
            'command': cmd,
            'environment': [
                {'name': 'S3_RESULTS_BUCKET', 'value': 'jingu-swebench-results'},
                {'name': 'RUN_ID', 'value': '$RUN_ID'},
            ]
        }
    ]
}
print(json.dumps(overrides))
")

echo ""
echo "┌─ ECS Batch Launch ──────────────────────────────────────────────────"
echo "│  cluster:      $CLUSTER"
echo "│  task-def:     $TASK_DEF_ARG"
echo "│  run-id:       $RUN_ID"
echo "│  instances:    $INSTANCES"
echo "│  max-attempts: $MAX_ATTEMPTS  workers: $WORKERS  stagger: $STAGGER"
echo "│  results → s3://jingu-swebench-results/$RUN_ID/"
echo "└─────────────────────────────────────────────────────────────────────"
echo ""

# ── Run the task ───────────────────────────────────────────────────────────────
# Use capacity provider (jingu-swebench-ec2-cp) so ECS auto-scales the ASG.
# Do NOT use --launch-type EC2 when a capacity provider is configured — they conflict.
RESULT=$(aws ecs run-task \
  --cluster "$CLUSTER" \
  --task-definition "$TASK_DEF_ARG" \
  --capacity-provider-strategy "capacityProvider=jingu-swebench-ec2-cp,weight=1" \
  --overrides "$OVERRIDES" \
  --region "$REGION" \
  --output json)

TASK_ARN=$(echo "$RESULT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['tasks'][0]['taskArn'])" 2>/dev/null || echo "")
FAILURES=$(echo "$RESULT" | python3 -c "import json,sys; d=json.load(sys.stdin); f=d.get('failures',[]); [print(x) for x in f]" 2>/dev/null || echo "")

if [[ -z "$TASK_ARN" ]]; then
  echo "ERROR: Task failed to launch"
  echo "$RESULT" | python3 -m json.tool
  exit 1
fi

# Short task ID for display
TASK_ID="${TASK_ARN##*/}"

echo "Task launched:"
echo "  ARN:  $TASK_ARN"
echo "  ID:   $TASK_ID"
echo ""
echo "Monitor:"
echo "  aws ecs describe-tasks --cluster $CLUSTER --tasks $TASK_ARN --region $REGION --query 'tasks[0].lastStatus'"
echo ""
echo "Logs (after task starts):"
echo "  aws logs tail /ecs/jingu-swebench-runner --follow --region $REGION"
echo ""
echo "Download results (after task completes):"
echo "  aws s3 sync s3://jingu-swebench-results/$RUN_ID/ results/$RUN_ID/ --region $REGION"

# ── Optional wait ──────────────────────────────────────────────────────────────
if $WAIT; then
  echo ""
  echo "Waiting for task to complete (polling every 30s)..."
  while true; do
    STATUS=$(aws ecs describe-tasks \
      --cluster "$CLUSTER" \
      --tasks "$TASK_ARN" \
      --region "$REGION" \
      --query 'tasks[0].lastStatus' \
      --output text 2>/dev/null || echo "UNKNOWN")
    echo "  [$(date '+%H:%M:%S')] status: $STATUS"
    if [[ "$STATUS" == "STOPPED" ]]; then
      EXIT_CODE=$(aws ecs describe-tasks \
        --cluster "$CLUSTER" \
        --tasks "$TASK_ARN" \
        --region "$REGION" \
        --query 'tasks[0].containers[0].exitCode' \
        --output text 2>/dev/null || echo "?")
      echo ""
      echo "Task stopped (exit code: $EXIT_CODE)"
      echo "Downloading results..."
      mkdir -p "results/$RUN_ID"
      aws s3 sync "s3://jingu-swebench-results/$RUN_ID/" "results/$RUN_ID/" --region "$REGION"
      echo "Results → results/$RUN_ID/"
      break
    fi
    sleep 30
  done
fi
