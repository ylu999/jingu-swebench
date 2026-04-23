#!/usr/bin/env bash
# reproduce_benchmark.sh — Reproduce Jingu SWE-bench benchmark results
#
# Usage:
#   ./scripts/reproduce_benchmark.sh                          # S4.6 +Jingu (default)
#   ./scripts/reproduce_benchmark.sh --model sonnet-4-5       # S4.5 +Jingu
#   ./scripts/reproduce_benchmark.sh --model opus-4-6         # Opus 4.6 +Jingu
#   ./scripts/reproduce_benchmark.sh --model sonnet-4-6 --attempts 1  # S4.6 model-only
#
# Prerequisites:
#   - AWS credentials configured (ECS + ECR + S3 access)
#   - ASG scaled to DesiredCapacity=1
#   - Docker image built: python scripts/ops.py build

set -euo pipefail

# Defaults (best_config_v1)
MODEL="sonnet-4-6"
ATTEMPTS=2
BATCH_PREFIX="reproduce"
SKIP_SMOKE=""

usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Options:
  --model MODEL       Model tier: sonnet-4-5, sonnet-4-6, opus-4-6 (default: sonnet-4-6)
  --attempts N        Max attempts: 1 = model-only, 2 = +Jingu (default: 2)
  --batch-name NAME   Custom batch name (default: reproduce-<model>-<attempts>att)
  --skip-smoke        Skip smoke test (not recommended for first run)
  -h, --help          Show this help

Examples:
  # Reproduce four-cell matrix
  $0 --model sonnet-4-5 --attempts 1    # → 16/30 (model-only)
  $0 --model sonnet-4-5 --attempts 2    # → 19/30 (+Jingu)
  $0 --model sonnet-4-6 --attempts 1    # → 19/30 (model-only)
  $0 --model sonnet-4-6 --attempts 2    # → 22/30 (+Jingu)
  $0 --model opus-4-6   --attempts 2    # → 23/30 (ceiling)

Config: configs/best_config_v1.yaml (EFR ON, all dead lines OFF)
EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)      MODEL="$2"; shift 2 ;;
        --attempts)   ATTEMPTS="$2"; shift 2 ;;
        --batch-name) BATCH_PREFIX="$2"; shift 2 ;;
        --skip-smoke) SKIP_SMOKE="--skip-smoke"; shift ;;
        -h|--help)    usage ;;
        *)            echo "Unknown option: $1"; usage ;;
    esac
done

# Map model shorthand to Bedrock model string
case "$MODEL" in
    sonnet-4-5) BEDROCK_MODEL="bedrock/global.anthropic.claude-sonnet-4-5" ;;
    sonnet-4-6) BEDROCK_MODEL="bedrock/global.anthropic.claude-sonnet-4-6" ;;
    opus-4-6)   BEDROCK_MODEL="bedrock/global.anthropic.claude-opus-4-6-v1" ;;
    *)          echo "ERROR: Unknown model '$MODEL'. Use: sonnet-4-5, sonnet-4-6, opus-4-6"; exit 1 ;;
esac

BATCH_NAME="${BATCH_PREFIX}-${MODEL}-${ATTEMPTS}att"

echo "=== Jingu SWE-bench Reproduce ==="
echo "Model:      $MODEL ($BEDROCK_MODEL)"
echo "Attempts:   $ATTEMPTS"
echo "Batch:      $BATCH_NAME"
echo "Config:     best_config_v1 (EFR ON, dead lines OFF)"
echo ""

# Verify Dockerfile has correct model
CURRENT_MODEL=$(grep "^ENV JINGU_MODEL=" Dockerfile | cut -d= -f2)
if [[ "$CURRENT_MODEL" != "$BEDROCK_MODEL" ]]; then
    echo "WARNING: Dockerfile has JINGU_MODEL=$CURRENT_MODEL"
    echo "         Expected: $BEDROCK_MODEL"
    echo ""
    echo "To fix: edit Dockerfile ENV JINGU_MODEL=$BEDROCK_MODEL"
    echo "        then run: python scripts/ops.py build"
    exit 1
fi

# Verify dead lines are OFF
for var in JINGU_CANDIDATE_SELECTION JINGU_DIRECTION_RECON JINGU_FIX_HYPOTHESIS; do
    val=$(grep "^ENV ${var}=" Dockerfile | cut -d= -f2)
    if [[ "$val" != "0" ]]; then
        echo "ERROR: $var=$val (must be 0 for best_config_v1)"
        exit 1
    fi
done

echo "Config validation: PASS"
echo ""

# Launch pipeline
PIPELINE_CMD="python scripts/ops.py pipeline --batch-name $BATCH_NAME --max-attempts $ATTEMPTS --runbook-ack"
if [[ -n "$SKIP_SMOKE" ]]; then
    PIPELINE_CMD="$PIPELINE_CMD --skip-smoke"
fi

echo "Running: $PIPELINE_CMD"
echo ""
exec $PIPELINE_CMD
