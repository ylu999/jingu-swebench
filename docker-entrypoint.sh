#!/bin/bash
set -e

# Start Docker daemon (DinD mode for SWE-bench eval containers)
# ECS task must have privileged: true
# Use vfs storage driver for nested Docker (overlay-on-overlay not supported)
if ! docker info >/dev/null 2>&1; then
    echo "[entrypoint] Starting dockerd (storage-driver=vfs)..."
    dockerd \
        --host=unix:///var/run/docker.sock \
        --storage-driver=vfs \
        --iptables=false \
        &
    # Wait for docker to be ready
    for i in $(seq 1 60); do
        if docker info >/dev/null 2>&1; then
            echo "[entrypoint] dockerd ready (${i}s)"
            break
        fi
        sleep 1
    done
    if ! docker info >/dev/null 2>&1; then
        echo "[entrypoint] ERROR: dockerd failed to start after 60s"
        exit 1
    fi
fi

# Determine output dir from args (--output <dir>)
OUTPUT_DIR="/app/results/run"
for i in "$@"; do
    if [ "$PREV" = "--output" ]; then
        OUTPUT_DIR="$i"
    fi
    PREV="$i"
done

echo "[entrypoint] output dir: $OUTPUT_DIR"
echo "[entrypoint] S3 bucket: ${S3_RESULTS_BUCKET:-not set}"
echo "[entrypoint] docker info:"
docker info 2>&1 | grep -E 'Storage Driver|Server Version|Cgroup'

# Detect eval mode (--eval flag as first arg)
if [ "$1" = "--eval" ]; then
    shift
    # args: --predictions-s3 <s3-key> --run-id <id> --workers <n> --dataset <name> --output <dir>
    PREDICTIONS_S3=""
    RUN_ID="eval-run"
    WORKERS=4
    DATASET="SWE-bench/SWE-bench_Verified"
    while [ $# -gt 0 ]; do
        case "$1" in
            --predictions-s3) PREDICTIONS_S3="$2"; shift 2 ;;
            --run-id) RUN_ID="$2"; shift 2 ;;
            --workers) WORKERS="$2"; shift 2 ;;
            --dataset) DATASET="$2"; shift 2 ;;
            --output) OUTPUT_DIR="$2"; shift 2 ;;
            *) shift ;;
        esac
    done
    echo "[entrypoint] eval mode: predictions-s3=$PREDICTIONS_S3 run-id=$RUN_ID workers=$WORKERS"
    # Download predictions from S3
    python3 -c "
import boto3, os
s3 = boto3.client('s3', region_name='us-west-2')
bucket, key = '${S3_RESULTS_BUCKET}', '$PREDICTIONS_S3'
s3.download_file(bucket, key, '/tmp/eval-predictions.jsonl')
print(f'[entrypoint] downloaded predictions from s3://{bucket}/{key}')
"
    cd /app
    python3 -m swebench.harness.run_evaluation         --dataset_name "$DATASET"         --predictions_path /tmp/eval-predictions.jsonl         --max_workers "$WORKERS"         --run_id "$RUN_ID"         --cache_level env
    EXIT_CODE=$?
    # Copy report to output dir for S3 upload
    mkdir -p "$OUTPUT_DIR"
    cp -r "evaluation_results/${RUN_ID}"* "$OUTPUT_DIR/" 2>/dev/null || true
else
    # Run pipeline
    cd /app
    python scripts/run_with_jingu_gate.py "$@"
    EXIT_CODE=$?
fi

# Upload results to S3 using Python boto3
if [ -n "$S3_RESULTS_BUCKET" ] && [ -d "$OUTPUT_DIR" ]; then
    RUN_NAME=$(basename "$OUTPUT_DIR")
    echo "[entrypoint] uploading results to s3://${S3_RESULTS_BUCKET}/${RUN_NAME}/"
    python3 - <<PYEOF
import boto3, os, pathlib
bucket = os.environ["S3_RESULTS_BUCKET"]
run_name = "${RUN_NAME}"
output_dir = pathlib.Path("${OUTPUT_DIR}")
region = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
s3 = boto3.client("s3", region_name=region)
for f in output_dir.rglob("*"):
    if f.is_file():
        key = f"{run_name}/{f.relative_to(output_dir)}"
        s3.upload_file(str(f), bucket, key)
        print(f"  uploaded: {key}")
print("[entrypoint] upload complete")
PYEOF
fi

exit $EXIT_CODE
