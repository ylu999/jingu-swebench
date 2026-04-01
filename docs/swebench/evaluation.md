# SWE-bench Evaluation Guide

Source: https://www.swebench.com/SWE-bench/guides/evaluation/

## Overview

SWE-bench evaluates AI models by applying generated patches to real repositories and running tests
to verify issue resolution in containerized Docker environments.

## Basic Evaluation Commands

### SWE-bench Lite (recommended for iteration)
```bash
python -m swebench.harness.run_evaluation \
    --dataset_name princeton-nlp/SWE-bench_Lite \
    --predictions_path <path_to_predictions> \
    --max_workers 8 \
    --run_id my_first_evaluation
```

### SWE-bench Verified (target for leaderboard)
```bash
python -m swebench.harness.run_evaluation \
    --dataset_name princeton-nlp/SWE-bench_Verified \
    --predictions_path <path_to_predictions> \
    --max_workers 8 \
    --run_id my_evaluation
```

### Full SWE-bench
```bash
python -m swebench.harness.run_evaluation \
    --dataset_name princeton-nlp/SWE-bench \
    --predictions_path <path_to_predictions> \
    --max_workers 12 \
    --run_id full_evaluation
```

## Prediction Format (JSONL)

Each line must be a JSON object with:
- `instance_id`: Repository and issue identifier (e.g., `django__django-11039`)
- `model_name_or_path`: Model identifier (any string)
- `model_patch`: Patch content as a unified diff string

```json
{"instance_id": "django__django-11039", "model_patch": "--- a/file.py\n+++ b/file.py\n...", "model_name_or_path": "my-model"}
```

## Advanced Options

```bash
# Evaluate specific instances only
--instance_ids astropy__astropy-14539 sympy__sympy-20590

# Control Docker caching
--cache_level env   # Options: none, base, env, instance

# Clean up after evaluation
--clean True

# Force rebuild Docker images
--force_rebuild True

# Set timeout per instance (seconds)
--timeout 300
```

## Key Metrics

Results saved in `evaluation_results/`:
- total instances
- submitted/completed counts
- **resolved instances** (FAIL_TO_PASS tests now pass, PASS_TO_PASS still pass)
- **resolution rate** = resolved / total

## Cloud Evaluation (sb-cli)

Alternative to local Docker — no Docker setup needed:
```bash
pip install sb-cli
sb-cli gen-api-key your@email.com
export SWEBENCH_API_KEY=<key>
sb-cli submit swe-bench_verified test \
  --predictions_path predictions.jsonl \
  --run_id my-run
```

**Quota warning:**
- `test` split: ~1 run/subset — submit only final predictions
- `dev` split: 976+ runs — use for format/pipeline validation
