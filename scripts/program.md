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

**B1 active.**

jingu-trust-gate is now integrated via subprocess bridge (Python → Node.js → TS gate).
Gate mode: `GATE_MODE = "trust_gate"` in `run_with_jingu_gate.py`.

The loop is in the B1 phase: every patch goes through `PatchAdmissionPolicy` before
being accepted as a candidate. The agent's job is to tune gate parameters within the
already-defined trust-gate design — not to redesign the system.

B1 gate pipeline:
  mini-SWE-agent patch
    → normalize_patch()
    → jingu_gate_bridge.evaluate_patch_from_traj()  ← NEW
      → gate_runner.js (Node subprocess)
        → PatchAdmissionPolicy (TS)
          R1: parse/structural validity
          R2: trajectory evidence (submitted vs LimitsExceeded)
          R3: apply_result (if git apply ran)
    → admit/reject/downgrade-speculative
    → retry with gate's retry_hint if rejected

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
- `GATE_MODE`: switch between "trust_gate" (B1) and "structural" (B0 fallback)
- Gate parameters via `options` dict passed to `evaluate_patch_from_traj()`
  e.g. `options={"require_trajectory": False}` to relax evidence requirement
- `score_patch`: scoring function for candidate selection
- `BASE_CONFIG["agent"]["step_limit"]`: step budget for mini-SWE-agent
- retry_hint logic: fallback hints when gate feedback is absent

**In `scripts/patch_admission_policy.js`** (B1 Layer 3 params, loop-tunable):
- `GATE_PARAMS.require_trajectory`: require traj evidence to avoid speculative downgrade
- `GATE_PARAMS.max_files_changed`: reject patches touching too many files

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
- `jingu_gate_bridge.py` — Python→TS subprocess bridge (infra)
- `gate_runner.js` — Node.js gate entry point (infra)
- Any file outside `scripts/run_with_jingu_gate.py` and `scripts/patch_admission_policy.js`

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
