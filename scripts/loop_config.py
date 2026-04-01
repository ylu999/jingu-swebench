"""
loop_config.py — Infrastructure configuration for jingu-swebench auto loop.

This file is the ONLY place to change infra parameters.
auto_loop.py imports from here; it never hardcodes infra values.

Change this file when:
  - Cloud host changes
  - Python / script paths on cloud change
  - Default instances change
  - Stage1/2 timeouts need tuning
  - Local Docker becomes available (set STAGE1_LOCAL=True)

Do NOT change:
  - auto_loop.py (loop logic)
  - run_with_jingu_gate.py except via the agent
  - fast_eval.py / swebench_infra.py (eval infrastructure)
"""

import os
from pathlib import Path

# ── Cloud SSH ──────────────────────────────────────────────────────────────────

CLOUD_HOST    = os.environ.get("CLOUD_HOST",    "cloud")
CLOUD_PYTHON  = os.environ.get("CLOUD_PYTHON",  "~/.local/share/mise/shims/python")
CLOUD_SCRIPTS = os.environ.get("CLOUD_SCRIPTS", "~/jingu-swebench/scripts")
CLOUD_RESULTS = os.environ.get("CLOUD_RESULTS", "~/jingu-swebench/results")

# ── Stage execution ────────────────────────────────────────────────────────────

# Local Docker requires enterprise login (locked on dev laptop).
# Set to True only if local Docker becomes available.
STAGE1_LOCAL = os.environ.get("STAGE1_LOCAL", "false").lower() == "true"

# ── Default instances ──────────────────────────────────────────────────────────

DEFAULT_INSTANCES = [
    "django__django-11039",
    "django__django-11001",
    "django__django-11019",
    "django__django-11049",
    "django__django-11099",
]

# ── Timeouts ───────────────────────────────────────────────────────────────────

# Stage1: per-instance budget × max_attempts × instances + headroom
# Formula: max_attempts * n_instances * per_instance_budget_s + headroom_s
STAGE1_PER_INSTANCE_BUDGET_S = 400   # seconds per instance per attempt
STAGE1_HEADROOM_S             = 60

# Stage2: fast_eval Docker pytest (much faster)
STAGE2_TIMEOUT_S = 180

# Claude agent timeout
CLAUDE_AGENT_TIMEOUT_S = int(os.environ.get("CLAUDE_TIMEOUT", "1800"))

# ── Scripts to sync to cloud before each eval ─────────────────────────────────
# Relative to SCRIPT_DIR in auto_loop.py.
CLOUD_SYNC_SCRIPTS = [
    "run_with_jingu_gate.py",
    "swebench_infra.py",
    # B1: jingu-trust-gate bridge files
    "jingu_gate_bridge.py",
    "gate_runner.js",
    "patch_admission_policy.js",
    # B2: adversarial reviewer
    "patch_reviewer.py",
]

# ── B1: jingu-trust-gate dist sync ────────────────────────────────────────────
# The TS gate dist (~456K) must be present on cloud for gate_runner.js to import.
# Local dist is built from jingu-trust-gate repo; cloud receives it via rsync/scp.
JINGU_TRUST_GATE_DIST_LOCAL = os.environ.get(
    "JINGU_TRUST_GATE_DIST_LOCAL",
    str(Path(__file__).parent.parent.parent / "jingu-trust-gate" / "dist" / "src"),
)
# Where it lands on cloud (gate_runner.js reads JINGU_TRUST_GATE_DIST env var)
CLOUD_TRUST_GATE_DIST = os.environ.get(
    "CLOUD_TRUST_GATE_DIST",
    "~/jingu-swebench/jingu-trust-gate-dist",
)
