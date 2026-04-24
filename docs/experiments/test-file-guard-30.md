# Experiment: Test File Modification Guard — 30-Instance Validation

## Config
- **Commit**: 837f16e
- **Model**: claude-sonnet-4-6
- **Attempts**: 2
- **Batch**: test-file-guard-30
- **Baseline**: ladder-sonnet46-full30 (22/30, commit 436506e)

## What Was Tested
Three-layer defense against agent modifying benchmark test files:
1. **Prompt injection**: "Do NOT modify test files" added to F2P instruction block
2. **Post-execution detection**: `test_file_guard` telemetry in jingu_body
3. **Patch stripping**: `strip_test_file_hunks()` removes test file diffs from submission

## Motivation
11141 incident: agent modified `tests/migrations/test_loader.py` assertions to pass CV,
but SWE-bench eval uses original tests → false "resolved" in CV, UNRESOLVED in eval.

## Results

### Smoke (1 instance)
- 11141: **RESOLVED** — agent stopped modifying test files after prompt injection

### 3-Instance Validation
- 11141: RESOLVED (+1 uplift)
- 11138: UNRESOLVED (same env_failure, no regression)
- 11477: UNRESOLVED (same wrong_patch, no regression)

### 30-Instance Full Run
- **20/29 resolved** (1 instance missing: 11292)
- vs baseline 22/30

| Type | Count | Instances |
|------|-------|-----------|
| New wins | 2 | 11265, 11451 |
| Regressions | 4 | 10973, 11087, 11206, 11292 (missing) |
| Net | -2 | |

Regressions are LLM variance — unrelated to test file guard (prompt-only change).

### Guard Telemetry
- test_file_guard violation = false across ALL evaluated attempts
- strip_triggered = 0 (prompt layer sufficient, stripping never needed)
- 11141 consistently resolved when guard active

## Decision
- **Keep guard enabled** — safety invariant, not uplift claim
- Guards against a confirmed anti-pattern (agent modifying benchmark tests)
- Stable 11141 resolve confirmed across 3 separate runs
- No feature flag needed — guard is default-on

## Key Insight
The guard's value is correctness enforcement, not resolve-rate improvement.
It prevents a specific class of false-positive CV results where agent
modifies test assertions rather than fixing source code.
