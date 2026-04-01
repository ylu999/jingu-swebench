# AutoResearch Program: jingu-swebench Optimizer

## Ultimate Goal
Maximize acceptance_rate on SWE-bench instances using mini-SWE-agent + Jingu gates.
Target: >= 50% acceptance rate on swe-bench_verified (test split).

## Current Position in Larger Goal
This loop is the **inner optimization loop** of jingu-swebench.
The outer goal is to submit a competitive score to the SWE-bench leaderboard via `sb-cli submit`.
Each loop round improves the agent's patch generation strategy.

## Metrics (IMMUTABLE — never modify the definitions)

Primary optimization target:
```
resolve_rate = resolved_instances / total_instances
```
- `resolved` = patch passes FAIL_TO_PASS tests inside Docker container (fast_eval.py)
- This is the true signal — closer to leaderboard ground truth
- Higher is strictly better

Secondary (gate filter, not the optimization target):
```
acceptance_rate = accepted_instances / total_instances
```
- `accepted` = patch passes Jingu structural gate (format check only)
- High acceptance_rate with low resolve_rate = gate is too weak

Final truth (used sparingly, not per-round):
- sb-cli submit → official leaderboard score
- Use only to validate significant improvements (5+ pp resolve_rate gain)

## What the Loop Can Modify
ONLY `scripts/run_with_jingu_gate.py`:
- `BASE_CONFIG`: model params, agent mode, timeouts
- `retry_hint` logic: what failure context to inject on retry
- `jingu_structural_check`: patch validation heuristics
- `score_patch`: scoring function for candidate selection

## What the Loop MUST NOT Modify
- `auto_loop.py` — the loop itself
- `program.md` — this file
- `compare_groups.py` — eval reporting
- `submit-sbcli.mjs` — official submission
- `swebench_infra.py` — eval infrastructure (timing, dataset loading, agent runner, parallelism)
- Any file outside `scripts/run_with_jingu_gate.py`

## CRITICAL: Eval Is Owned by auto_loop, Not the Agent

**The agent MUST NOT run any eval.**

This means:
- Do NOT run `run_with_jingu_gate.py` on cloud
- Do NOT run `fast_eval.py`
- Do NOT start Docker containers
- Do NOT run `swebench.harness.run_evaluation`
- Do NOT poll results or wait for containers

**Why:** auto_loop.py runs eval automatically after detecting a file change.
If the agent also runs eval, the round takes 1+ hour instead of 5 minutes.

**The agent job is ONLY:**
1. Analyze round history and metrics already provided in context
2. Form a hypothesis
3. Make ONE change to `run_with_jingu_gate.py`
4. Write result JSON
5. Exit immediately

auto_loop will detect the file change, run eval, and report results in the next round context.

## Constraints
1. Each hypothesis must be ONE change at a time (no compound changes)
2. Changes must be reversible via git reset
3. A change is committed only if resolve_rate strictly increases
4. Never modify the scoring metric to make it easier to satisfy (no eval gaming)
5. Agent model: bedrock/global.anthropic.claude-sonnet-4-5-20250929-v1:0 (can be changed)
6. **Do not run eval** — see CRITICAL section above

## Known Findings (do not re-test)
- Layer A (localization) + Layer B (patching): acceptance ~55% on 9 django instances
- Layer C (strict-observed-only): no net gain, increases parse failures
- attempts=3 vs attempts=1: +11pp recovery from hard instances
- PARSE_FAILED and PATCH_APPLY_FAILED are the dominant failure modes
- Instance django__django-11049 is parse-sensitive (avoid strict output constraints)

## Round History Interpretation
- If the same failure_code dominates multiple rounds → structural problem, not tuning
- If acceptance_rate oscillates → hypothesis direction is wrong
- If improvement stalls at round 5+ → consider changing model or agent mode
