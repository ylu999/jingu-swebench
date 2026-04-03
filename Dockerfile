# jingu-swebench runner image
# ECS EC2 launch type with privileged: true (needed for DinD — SWE-bench eval containers)

FROM python:3.12-slim

# Install system deps: Docker CLI + daemon, Node.js 18, git
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg git \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 18
RUN curl -fsSL https://deb.nodesource.com/setup_18.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Python packages
RUN pip install --no-cache-dir \
    "mini-swe-agent==2.2.8" \
    "litellm==1.83.0" \
    "swebench==4.1.0" \
    "boto3==1.42.1" \
    "pydantic==2.12.5"

# Copy jingu-swebench.yaml into the installed mini-swe-agent config directory.
# mini-swe-agent 2.2.8 ships swebench.yaml; we add our fork alongside it.
# This overrides ENVIRONMENT_NOT_AGENT_WORK violations from swebench.yaml defaults.
COPY config/jingu-swebench.yaml /usr/local/lib/python3.12/site-packages/minisweagent/config/benchmarks/jingu-swebench.yaml

# Working dir
WORKDIR /app

# Copy jingu-trust-gate compiled dist + node_modules
COPY jingu-trust-gate/dist /app/jingu-trust-gate/dist
COPY jingu-trust-gate/package.json /app/jingu-trust-gate/package.json
COPY jingu-trust-gate/node_modules /app/jingu-trust-gate/node_modules

# Copy scripts
COPY scripts/run_with_jingu_gate.py \
     scripts/jingu_gate_bridge.py \
     scripts/retry_controller.py \
     scripts/strategy_logger.py \
     scripts/aggregate_strategies.py \
     scripts/preflight.py \
     scripts/patch_reviewer.py \
     scripts/patch_signals.py \
     scripts/declaration_extractor.py \
     scripts/cognition_check.py \
     scripts/gate_runner.js \
     scripts/patch_admission_policy.js \
     /app/scripts/

# Bake provenance into image (RT6: artifacts carry their own identity)
ARG GIT_COMMIT=unknown
ARG BUILD_TIMESTAMP=unknown
RUN echo "$GIT_COMMIT" > /app/.image_commit && \
    echo "$BUILD_TIMESTAMP" > /app/.build_timestamp
ENV GIT_COMMIT=$GIT_COMMIT
ENV BUILD_TIMESTAMP=$BUILD_TIMESTAMP

# gate_runner.js uses top-level await — must run as ESM.
# Node.js looks for package.json with "type":"module" up the directory tree.
RUN echo '{"type":"module"}' > /app/scripts/package.json

# Results volume mount point
RUN mkdir -p /app/results

# Entrypoint: start dockerd in background then run the pipeline
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

ENV JINGU_TRUST_GATE_DIST=/app/jingu-trust-gate/dist/src
ENV JINGU_SWEBENCH_SCRIPTS=/app/scripts
ENV PYTHONPATH=/app/scripts

ENTRYPOINT ["/app/docker-entrypoint.sh"]
