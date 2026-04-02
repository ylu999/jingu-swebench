#!/bin/bash
set -e

# Start Docker daemon (needed for SWE-bench eval containers)
# ECS task must have privileged: true
if ! docker info >/dev/null 2>&1; then
    echo "[entrypoint] Starting dockerd..."
    dockerd --host=unix:///var/run/docker.sock &
    DOCKERD_PID=$!
    # Wait for docker to be ready
    for i in $(seq 1 30); do
        if docker info >/dev/null 2>&1; then
            echo "[entrypoint] dockerd ready"
            break
        fi
        sleep 1
    done
fi

# Run pipeline with all passed args
cd /app
exec python scripts/run_with_jingu_gate.py "$@"
