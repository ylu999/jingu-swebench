"""
loop_config.py — Infrastructure configuration for jingu-swebench.

Eval runs on ECS (EC2 launch type, Docker-in-Docker via vfs storage driver).
Scripts are baked into the container image at build time.
Results are uploaded to S3 after each run.

Change this file when:
  - Default instances change
  - Per-instance timeouts need tuning
  - Claude agent timeout needs tuning

Do NOT change:
  - run_with_jingu_gate.py except via the agent
  - fast_eval.py / swebench_infra.py (eval infrastructure)
"""

import os

# ── Default instances ──────────────────────────────────────────────────────────

DEFAULT_INSTANCES = [
    "django__django-11039",
    "django__django-11001",
    "django__django-11019",
    "django__django-11049",
    "django__django-11099",
]

# ── Timeouts ───────────────────────────────────────────────────────────────────

# Per-instance budget × max_attempts × instances + headroom
# Formula: max_attempts * n_instances * per_instance_budget_s + headroom_s
STAGE1_PER_INSTANCE_BUDGET_S = 400   # seconds per instance per attempt
STAGE1_HEADROOM_S             = 60

# Claude agent timeout
CLAUDE_AGENT_TIMEOUT_S = int(os.environ.get("CLAUDE_TIMEOUT", "1800"))
