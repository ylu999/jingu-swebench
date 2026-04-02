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
        docker info 2>&1 || true
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

# Run pipeline
cd /app
python scripts/run_with_jingu_gate.py "$@"
EXIT_CODE=$?

# Upload results to S3 if bucket is configured
if [ -n "$S3_RESULTS_BUCKET" ] && [ -d "$OUTPUT_DIR" ]; then
    RUN_NAME=$(basename "$OUTPUT_DIR")
    echo "[entrypoint] uploading results to s3://${S3_RESULTS_BUCKET}/${RUN_NAME}/"
    aws s3 sync "$OUTPUT_DIR" "s3://${S3_RESULTS_BUCKET}/${RUN_NAME}/" --region "${AWS_DEFAULT_REGION:-us-west-2}"
    echo "[entrypoint] upload complete"
fi

exit $EXIT_CODE
