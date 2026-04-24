# Experiment: Execution-Level Interventions Validation

## Config
- **Commit**: b7f87c6
- **Model**: claude-sonnet-4-6
- **Attempts**: 2
- **Baseline**: ladder-sonnet46-full30 (22/30, commit 436506e)

## Interventions Tested
1. **Stall detector**: LimitsExceeded + 0 files → STALL DETECTED hint + candidate source files
2. **Near-miss finisher**: f2p_ratio > 0.9 → targeted prompt with failing test names
3. **Derived candidate files**: infer source paths from test failures/problem statement

## Results

### 8-Instance Targeted Batch (intervention-8unresolved)
- **4/8 resolved** (10973, 11087, 11206, 11265)
- All via A2 rescue (rescued_rate=100%)
- 0 regressions (all 8 were previously unresolved)

### 30-Instance Full Validation (intervention-30-validation-b7f87c6)
- **21/30 resolved** (vs 22/30 baseline)
- New win: 10973 (+1)
- Regressions: 11138, 11292 (-2)
- **Net gain: -1**

## Decision Gate
| Criterion | Threshold | Actual | Pass? |
|-----------|-----------|--------|-------|
| resolve_rate | >=24/30 | 21/30 | FAIL |
| regressions | <=1 | 2 | FAIL |
| net_gain | >=+2 | -1 | FAIL |

## Regression Attribution
- **11138**: Only A1 dir saved, no intervention text in A2 prompt → pure LLM variance
- **11292**: 0 strategy log entries, no data in S3 → pure LLM variance
- **Neither regression caused by intervention code** — stochastic model behavior

## Key Insight
8-instance batch overstated effect. 3/4 "new resolves" (11087, 11206, 11265) did not reproduce on full 30-instance run. The intervention mechanism fires correctly but does not produce **stable** causal uplift.

## Decision
- **Do not enable by default** (EXEC_INTERVENTIONS_ENABLED=0)
- Code retained behind feature flag (commit fb9e131)
- Interventions are non-harmful but also non-beneficial at 30-instance scale
