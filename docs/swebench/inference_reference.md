# SWE-bench Inference Reference

Source: https://www.swebench.com/SWE-bench/reference/inference/

## Overview

Inference = generating model patches for SWE-bench instances.
Evaluation = testing those patches with Docker harness.

These are separate steps. You can use any inference approach as long as
output matches the prediction format.

## Prediction Format (JSONL)

One JSON object per line:
```json
{
  "instance_id": "django__django-11039",
  "model_patch": "--- a/django/db/models/sql/compiler.py\n+++ b/django/db/models/sql/compiler.py\n@@ ...",
  "model_name_or_path": "my-agent-v1"
}
```

Rules:
- `model_patch` must be a valid unified diff string
- Empty string `""` = no patch submitted (counts as unresolved)
- `model_name_or_path` is just a label — any string

## Built-in Inference (via swebench.inference)

### API-based (OpenAI / Anthropic)
```bash
python -m swebench.inference.run_api \
  --dataset_name_or_path princeton-nlp/SWE-bench_oracle \
  --model_name_or_path claude-2 \
  --output_dir ./outputs \
  --split test
```

### Local Llama models
```bash
python -m swebench.inference.run_llama \
  --dataset_path princeton-nlp/SWE-bench_oracle \
  --model_name_or_path princeton-nlp/SWE-Llama-13b \
  --output_dir ./outputs \
  --temperature 0
```

## Custom Inference (our approach)

We use `run_with_jingu_gate.py` which:
1. Calls mini-SWE-agent (via Modal sandbox)
2. Gets candidate patches
3. Applies Jingu structural gates
4. Selects best patch
5. Writes JSONL to output dir

## Key Design: FAIL_TO_PASS Tests

A patch is "resolved" if:
1. It can be applied (`git apply` succeeds)
2. After applying: `FAIL_TO_PASS` tests pass (they were failing before)
3. After applying: `PASS_TO_PASS` tests still pass (no regression)

The `test_patch` is applied AFTER the model patch — it adds the test files
containing `FAIL_TO_PASS` tests to the environment.

## mini-SWE-agent + Modal

Our execution model:
- mini-SWE-agent runs inside Modal cloud containers (not local Docker)
- Local process = controller only
- Bedrock (Claude Sonnet) = the LLM inside the agent
- Modal handles container lifecycle and sandboxing

## Common Pitfalls

1. **Import errors in Docker** — usually wrong Python version or missing dependency in env image
2. **No tests ran** — `FAIL_TO_PASS` tests are in `test_patch`, not in the base repo
3. **Parse failures** — model output not valid unified diff format
4. **Apply failures** — diff applies to wrong base commit or wrong file
