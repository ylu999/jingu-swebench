# SWE-bench Harness Reference

Source: https://www.swebench.com/SWE-bench/reference/harness/

## Core Scripts

```bash
# Build Docker images (optional pre-build step)
python -m swebench.harness.prepare_images \
    --dataset_name princeton-nlp/SWE-bench_Verified \
    --max_workers 4

# Run evaluation
python -m swebench.harness.run_evaluation \
    --dataset_name princeton-nlp/SWE-bench_Verified \
    --predictions_path predictions.jsonl \
    --max_workers 8 \
    --run_id my_run
```

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `--dataset_name` | str | required | HuggingFace dataset name |
| `--split` | str | `test` | Dataset split |
| `--predictions_path` | str | required | Path to JSONL predictions file (or `"gold"`) |
| `--max_workers` | int | 4 | Parallel evaluation workers |
| `--run_id` | str | required | Unique run identifier |
| `--cache_level` | str | `env` | Docker caching: `none\|base\|env\|instance` |
| `--clean` | bool | False | Remove Docker resources after eval |
| `--instance_ids` | list | None | Space-separated list to evaluate subset |
| `--open_file_limit` | int | 4096 | File descriptor limit |
| `--force_rebuild` | bool | False | Rebuild all Docker images from scratch |
| `--log_level` | str | `INFO` | Logging verbosity |
| `--namespace` | str | `swebench` | Docker image namespace prefix |
| `--timeout` | int | 300 | Max seconds per instance |
| `--modal` | bool | False | Use Modal for cloud execution |

## Cache Levels (Docker storage tradeoff)

| Level | Storage | Speed | Notes |
|-------|---------|-------|-------|
| `none` | Minimal | Slowest | Rebuild everything each run |
| `base` | Minimal | Slow | Base Python images only |
| `env` | ~100GB | Moderate | **Default** — environment images cached |
| `instance` | ~2TB | Fastest | All images pre-built |

## System Requirements

- Docker installed and running
- Storage: minimum 120GB free (env cache level)
- RAM: 16GB+ recommended
- CPU: 8+ cores recommended
- Architecture: x86_64 (arm64 experimental)

## Worker Count Guidance

```
max_workers = min(0.75 * cpu_count(), 24)
```
- 8 CPU → 6 workers
- 16 CPU → 12 workers
- 32 CPU → 24 workers

## Evaluation Result

Pass/fail criteria for a resolved instance:
1. All `FAIL_TO_PASS` tests must transition from failing to passing
2. All `PASS_TO_PASS` tests must remain passing (no regression)

## Verifying Ground Truth

```bash
python -m swebench.harness.run_evaluation \
    --dataset_name princeton-nlp/SWE-bench_Verified \
    --predictions_path gold \
    --max_workers 4 \
    --run_id verify_ground_truth \
    --instance_ids django__django-11039
```
