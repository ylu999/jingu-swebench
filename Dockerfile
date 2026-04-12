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
     scripts/cognition_schema.py \
     scripts/gate_runner.js \
     scripts/patch_admission_policy.js \
     scripts/subtype_contracts.py \
     scripts/phase_prompt.py \
     scripts/principal_gate.py \
     scripts/principal_inference.py \
     scripts/phase_record.py \
     scripts/in_loop_judge.py \
     scripts/verification_evidence.py \
     scripts/governance_pack.py \
     scripts/governance_runtime.py \
     scripts/swebench_failure_reroute_pack.py \
     scripts/unresolved_case_classifier.py \
     scripts/phase_record_pack.py \
     scripts/failure_classifier.py \
     scripts/repair_prompts.py \
     scripts/analysis_gate.py \
     scripts/gate_rejection.py \
     scripts/failure_routing.py \
     scripts/extract_failure_events.py \
     scripts/compute_routing_stats.py \
     scripts/suggest_routing.py \
     scripts/strategy_prompts.py \
     scripts/check_onboarding.py \
     scripts/phase_validator.py \
     scripts/phase_schemas.py \
     scripts/cognition_prompts.py \
     scripts/jingu_onboard.py \
     scripts/step_monitor_state.py \
     scripts/signal_extraction.py \
     scripts/controlled_verify.py \
     scripts/jingu_adapter.py \
     scripts/jingu_agent.py \
     scripts/step_sections.py \
     scripts/step_event_emitter.py \
     scripts/decision_logger.py \
     scripts/checkpoint.py \
     scripts/replay_engine.py \
     scripts/replay_cli.py \
     scripts/replay_traj.py \
     scripts/traj_diff.py \
     scripts/prompt_regression.py \
     /app/scripts/
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
ENV PYTHONPATH=/app/scripts

ENTRYPOINT ["/app/docker-entrypoint.sh"]
