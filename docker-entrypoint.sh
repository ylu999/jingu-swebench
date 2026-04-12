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
    # Heartbeat: emit periodic signal so peek/monitors detect early failures
    EVAL_START=$(date +%s)
    (
        while true; do
            sleep 30
            ELAPSED=$(( $(date +%s) - EVAL_START ))
            echo "[eval-heartbeat] still running, elapsed=${ELAPSED}s"
        done
    ) &
    HEARTBEAT_PID=$!
    python3 -m swebench.harness.run_evaluation         --dataset_name "$DATASET"         --predictions_path /tmp/eval-predictions.jsonl         --max_workers "$WORKERS"         --run_id "$RUN_ID"         --cache_level env
    EXIT_CODE=$?
    kill $HEARTBEAT_PID 2>/dev/null || true
    # Copy report to output dir for S3 upload
    mkdir -p "$OUTPUT_DIR"
    echo "[entrypoint] searching for eval report (run_id=${RUN_ID}):"
    # SWE-bench writes report as <model>.<run_id>.json in cwd or evaluation_results/
    ls -la evaluation_results/ 2>/dev/null || echo "  (no evaluation_results/ dir)"
    ls -la *"${RUN_ID}"*.json 2>/dev/null || echo "  (no ${RUN_ID} json in cwd)"
    cp -r "evaluation_results/${RUN_ID}"* "$OUTPUT_DIR/" 2>/dev/null || true
    cp *"${RUN_ID}"*.json "$OUTPUT_DIR/" 2>/dev/null || true
    # Generate unified eval_results.json with per-instance resolved/unresolved lists
    python3 - "$RUN_ID" "$OUTPUT_DIR" <<'EVALEOF'
import json, glob, sys, os
run_id, output_dir = sys.argv[1], sys.argv[2]
results = {}
# Search both evaluation_results/ and cwd for report JSON
search_patterns = [
    f"evaluation_results/{run_id}*.json",
    f"evaluation_results/*{run_id}*.json",
    f"*{run_id}*.json",
]
found_files = set()
for pattern in search_patterns:
    found_files.update(glob.glob(pattern))
for f in sorted(found_files):
    try:
        data = json.load(open(f))
        if "resolved_ids" in data or "unresolved_ids" in data:
            results.update(data)
            print(f"[entrypoint] loaded eval from {f}: resolved={len(data.get('resolved_ids', []))}")
        else:
            print(f"[entrypoint] skipped {f} (no resolved_ids key)")
    except Exception as e:
        print(f"[entrypoint] warning: could not read {f}: {e}")
if results:
    out_path = os.path.join(output_dir, "eval_results.json")
    json.dump(results, open(out_path, "w"), indent=2)
    print(f"[entrypoint] wrote {out_path} ({len(results.get('resolved_ids',[]))} resolved)")
else:
    print("[entrypoint] WARNING: no eval results found")
EVALEOF
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
