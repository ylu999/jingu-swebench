# Jingu: Governance-Driven Uplift for LLM Code Agents

## Problem

LLM agents on SWE-bench fail ~40-50% of instances not because they lack capability, but because they lack structured recovery when their first attempt fails. Model upgrades help, but leave a consistent residual failure set.

## Method

**Jingu** adds a governance layer on top of an LLM coding agent:

1. **Phase-aware retry routing** — classify first-attempt failure (wrong_direction / incomplete_fix / verify_gap) → route retry to the right recovery phase (ANALYZE / DESIGN / EXECUTE)
2. **Execution feedback injection** — extract test output from failed attempt → feed back as structured signal for retry

No patch-level modification. No prompt hacking. The agent's reasoning is unconstrained; only the retry path is governed.

## Results

| | Model-only | +Jingu | Δ |
|---|---|---|---|
| **Sonnet 4.5** | 16/30 | 19/30 | **+3** |
| **Sonnet 4.6** | 19/30 | 22/30 | **+3** |
| **Opus 4.6** | — | 23/30 | ceiling |

Dataset: SWE-bench Verified, 30 Django instances. Config: `best_config_v1`.

## Key Insight

**Governance and model capability are additive, not substitutive.**

- Model upgrade: +3 (both configs)
- Jingu uplift: +3 (both models)
- Effects are orthogonal — neither diminishes the other
- Jingu recovers model-upgrade regressions (2 instances lost on S4.6, recovered by Jingu)

## What Works (and What Doesn't)

| Mechanism | Status | Uplift |
|-----------|--------|--------|
| EFR retry routing | Active | **+3** |
| Fix hypothesis ranking | Validated, inactive | 0 |
| Direction reconsideration | Validated, inactive | 0 |
| Multi-candidate selection | Infeasible | 0 |
| Design gate | Non-constraining | 0 |

Jingu's value is **process control** (retry routing), not **patch-level thinking aid**.

## Reproducibility

```bash
./scripts/reproduce_benchmark.sh --model sonnet-4-6 --attempts 2
```

All artifacts (trajectories, predictions, eval results) stored in S3. Tag: `v1.0-benchmark`.
