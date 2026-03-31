# jingu-swebench

Jingu adapter for SWE-bench — proves Jingu improves LLM coding performance on a real industry benchmark.

## What this is

Insert Jingu's governance loop (gates + retry) into the SWE-bench inference pipeline.
Compare `raw` (single LLM call) vs `jingu` (governed: gate + retry) on resolved rate.

## Quick start

```bash
# run raw baseline on 20 Lite instances
node dist/src/cli/run.js --mode raw --dataset lite --n 20

# run jingu mode
node dist/src/cli/run.js --mode jingu --dataset lite --n 20

# compare both
node dist/src/cli/run.js --mode compare --dataset lite --n 20
```

## Plan

See [PLAN.md](./PLAN.md) for full architecture, module structure, types, gates, and 5-day milestones.

## Status

🚧 In progress — Day 1
