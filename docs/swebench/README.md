# SWE-bench Documentation Index

Saved from https://www.swebench.com/SWE-bench/ — for offline reference by Claude Code.

## Files

- `quickstart.md` — Installation, basic usage, verification
- `evaluation.md` — Harness evaluation commands, prediction format, options
- `datasets.md` — Dataset variants, instance structure, fields
- `docker_setup.md` — Docker installation, caching, resource tuning
- `harness_reference.md` — Full harness parameter reference
- `inference_reference.md` — Inference API, prediction generation

## Key Facts (quick reference)

### Prediction format (JSONL)
```json
{"instance_id": "repo__repo-NNNN", "model_patch": "<unified diff>", "model_name_or_path": "my-model"}
```

### Official evaluation command
```bash
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Verified \
  --predictions_path predictions.jsonl \
  --max_workers 8 \
  --run_id my_run
```

### Cache levels
- `none` — slowest, minimal disk
- `base` — minimal disk
- `env` — default, ~100GB, moderate speed
- `instance` — ~2TB, fastest

### Dataset sizes
- SWE-bench: 2294 instances
- SWE-bench_Lite: 534 instances
- SWE-bench_Verified: 500 instances (expert-verified solvable)

### Performance tuning
- Workers: `min(0.75 * cpu_count(), 24)` — for 8 CPUs → 6 workers

### sb-cli (cloud evaluation — no Docker needed)
```bash
pip install sb-cli
sb-cli gen-api-key your@email.com
export SWEBENCH_API_KEY=<key>
sb-cli submit swe-bench_verified test \
  --predictions_path predictions.jsonl \
  --run_id my-run
```
- test split quota: ~1 run/subset (use sparingly)
- dev split quota: 976+ runs (use for validation)
