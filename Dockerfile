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
    "mini-swe-agent==2.1.0" \
    "litellm==1.83.0" \
    "swebench==4.1.0" \
    "boto3==1.42.1" \
    "pydantic==2.12.5"

# p224: Install JinguModel into mini-swe-agent's model directory + register in mapping
COPY mini-swe-agent/jingu_model.py /usr/local/lib/python3.12/site-packages/minisweagent/models/jingu_model.py
RUN python3 -c "\
import pathlib; \
p = pathlib.Path('/usr/local/lib/python3.12/site-packages/minisweagent/models/__init__.py'); \
t = p.read_text(); \
old = '\"litellm\": \"minisweagent.models.litellm_model.LitellmModel\"'; \
new = '\"jingu\": \"minisweagent.models.jingu_model.JinguModel\",\n    \"litellm\": \"minisweagent.models.litellm_model.LitellmModel\"'; \
p.write_text(t.replace(old, new)); \
print('JinguModel registered in _MODEL_CLASS_MAPPING')"

# Neutralize mini-swe-agent global .env to prevent dotenv from polluting JINGU_MODEL.
# The .env may accumulate stale vars across Docker layer cache; truncate it.
RUN rm -f /root/.config/mini-swe-agent/.env 2>/dev/null; \
    mkdir -p /root/.config/mini-swe-agent && touch /root/.config/mini-swe-agent/.env

# Copy jingu-swebench.yaml into the installed mini-swe-agent config directory.
# mini-swe-agent 2.1.0 ships swebench.yaml; we add our fork alongside it.
# This overrides ENVIRONMENT_NOT_AGENT_WORK violations from swebench.yaml defaults.
COPY config/jingu-swebench.yaml /usr/local/lib/python3.12/site-packages/minisweagent/config/benchmarks/jingu-swebench.yaml

# Working dir
WORKDIR /app

# Copy jingu-trust-gate compiled dist + node_modules
COPY jingu-trust-gate/dist /app/jingu-trust-gate/dist
COPY jingu-trust-gate/package.json /app/jingu-trust-gate/package.json
COPY jingu-trust-gate/node_modules /app/jingu-trust-gate/node_modules

# Copy all scripts (*.py + *.js) — no flat listing, new scripts auto-included
COPY scripts/*.py scripts/*.js /app/scripts/
# B1-CP: reasoning control plane Python module
COPY scripts/control/ /app/scripts/control/
# p222: cognition contracts (single source of truth for phase/subtype definitions)
COPY scripts/cognition_contracts/ /app/scripts/cognition_contracts/

# p227-04: jingu_loader Python package (from jingu-bundle-loader, copied by ops.py build)
COPY python/jingu_loader/ /app/python/jingu_loader/

# Bundle JSON (compiled contract from jingu-cognition)
COPY bundle.json /app/bundle.json

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
ENV PYTHONPATH=/app/scripts:/app/python
ENV JINGU_BUNDLE_PATH=/app/bundle.json
# Override dotenv pollution from mini-swe-agent .env (dotenv override=False won't touch existing env)
ENV JINGU_MODEL=bedrock/global.anthropic.claude-sonnet-4-5-20250929-v1:0
ENV JINGU_CANDIDATE_SELECTION=0
ENV JINGU_DIRECTION_RECON=1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
