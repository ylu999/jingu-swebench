# SWE-bench Intervention Line — Closure Record

**Date:** 2026-04-25
**Decision:** Close all SWE-bench intervention workstreams. No further gate/prompt/routing experiments.

## Best Result

**ladder-sonnet46-full30: 22/30 (73.3%)** — Sonnet 4.6 + Jingu best_config_v1

Model ladder (4-cell matrix):
| | Model-only | +Jingu |
|--|--|--|
| S4.5 | 16/30 | 19/30 (+3) |
| S4.6 | 19/30 | 22/30 (+3) |

Jingu uplift: **+3 on both models** (consistent, additive, orthogonal to model upgrade).

## Binding Failure Analysis (commit 831d85e)

8 unresolved instances classified:

| Category | Count | Description |
|----------|-------|-------------|
| F | 5 | Correct file, wrong patch logic (capability ceiling) |
| A | 2 | Design-file mismatch (1 test-only, 1 no-patch) |
| C | 1 | Patched OUT-OF-SCOPE file |

**False positive rate: 6/10 resolved would be killed by binding check.**
**Conclusion: Execution Binding Gate is NOT viable.** Net effect would be negative.

## All Intervention Lines — Final Status

| Line | Result | Commit |
|------|--------|--------|
| EFR routing | **+3 uplift (only successful line)** | 1eb5a7d |
| Design+Exec admission | Non-regressive (21/30), diagnostic only | 4d85bef |
| DHG (dual hypothesis) | All TIEs, 0 uplift | d06d59e |
| Exec interventions | 21/30 = net -1, disabled | fb9e131 |
| Mechanism critic | Zero discriminative power, killed | b8b9586 |
| Test file guard | Safety invariant, not uplift | 837f16e |
| Direction gate | 100% compliance, 0% effectiveness | — |
| Near-miss repair | Classifier correct, repair not effective | 683e354 |
| Binding gate | Data says no: F=5/8, FP=60% | 831d85e |

## Reusable Assets

These components have proven value and should be preserved:

1. **controlled_verify** — CV with f2p/p2p tracking
2. **phase telemetry** — step_events, decisions, prompt_snapshot per attempt
3. **failure attribution** — failure_type/mode/layer/source classification
4. **EFR routing** — failure-type-specific phase routing (the only uplift driver)
5. **offline binding analyzer** — `scripts/analyze_binding_failures.py`
6. **checkpoint system** — phase_records + cp_state snapshots per step

## Conclusion

SWE-bench has proven that governance can help up to a point (+3 consistent uplift via routing), but remaining 8 failures are model capability / patch synthesis errors that no control-plane intervention can fix. Further intervention experiments have exhausted returns.
