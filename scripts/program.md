# AutoResearch Program: jingu-swebench Optimizer

## Ultimate Goal

Integrate real Jingu features into the SWE-bench pipeline and measure their effect
against the stored mini-SWE-agent baseline (B0).

Target: demonstrate that each Jingu layer improves resolve_rate over B0.

## Baseline (FROZEN — never modify)

**B0 = 4/5 resolve (80%)** — stored in `results/B0_baseline.json`

- mini-SWE-agent + structural diff-format check only
- No trust-gate, no policy-core, no jingu-agent governance
- All future results are compared against this

## Experiment Sequence

| Stage | What's added | Measures |
|-------|-------------|---------|
| B0 | pure mini-SWE-agent (frozen baseline) | floor |
| B1 | + jingu-trust-gate | does admission control help? |
| B2 | + jingu-policy-core | does policy-driven gate help? |
| B3 | + jingu-agent governance | does governed retry loop help? |

Each stage must be compared to B0. A stage is "better" only if resolve_rate strictly
increases on the same 5 instances.

## Current Position

**B1 integration in progress.**

The loop is currently in the B1 phase: jingu-trust-gate has been (or is being)
integrated. The agent's job is to tune the gate parameters within the already-defined
trust-gate design — not to redesign the system.

## Metrics (IMMUTABLE — never modify the definitions)

Primary optimization target:
```
resolve_rate = resolved_instances / total_instances
```
- `resolved` = patch passes FAIL_TO_PASS tests inside Docker container (fast_eval.py)
- Higher is strictly better
- Compare against B0 = 0.80

Secondary (gate filter, not the optimization target):
```
acceptance_rate = accepted_instances / total_instances
```
- `accepted` = patch passes Jingu gate (structural + policy)
- High acceptance + low resolve = gate too weak

## What the Loop Can Modify

ONLY `scripts/run_with_jingu_gate.py`:
- Gate parameters and thresholds (within the current stage's design)
- retry_hint logic: what failure context to inject on retry
- score_patch: scoring function for candidate selection
- Stage-specific config (e.g. trust-gate strictness level)

**Rule: auto-loop may tune policies, but must not define them.**
**Search over a fixed design; don't search for the design itself.**

## What the Loop MUST NOT Modify

- `auto_loop.py` — the loop itself
- `loop_config.py` — infrastructure configuration
- `program.md` — this file
- `compare_groups.py` — eval reporting
- `submit-sbcli.mjs` — official submission
- `swebench_infra.py` — eval infrastructure
- `fast_eval.py` — fast resolve evaluator
- Any file outside `scripts/run_with_jingu_gate.py`

## CRITICAL: Eval Is Owned by auto_loop, Not the Agent

**The agent MUST NOT run any eval.**

- Do NOT run `run_with_jingu_gate.py` on cloud
- Do NOT run `fast_eval.py`
- Do NOT start Docker containers
- Do NOT SSH to cloud to check eval progress

auto_loop.py owns the entire eval pipeline:
1. Detects file change → syncs run_with_jingu_gate.py to cloud
2. Runs run_with_jingu_gate.py on cloud → generates patches
3. Runs fast_eval.py on cloud → measures resolve_rate
4. Writes results to journal → feeds next round context

**The agent job is ONLY:**
1. Analyze round history and metrics already provided in context
2. Form a hypothesis about gate parameter tuning
3. Make ONE change to `run_with_jingu_gate.py`
4. Write result JSON
5. Exit immediately

## Known Findings

- B0 baseline: 4/5 resolve. 11019 is the only unresolved instance at baseline.
- 11019 failure: agent runs out of steps (LimitsExceeded) — complex topological sort fix
- step_limit=100 gives agent more room than the old step_limit=60
- PARSE_FAILED and PATCH_APPLY_FAILED are dominant gate failure modes
- Instance 11049 is parse-sensitive (avoid strict output constraints)
