# SWE-bench Datasets

Source: https://www.swebench.com/SWE-bench/guides/datasets/

## Available Datasets

| Dataset | Size | Purpose |
|---------|------|---------|
| `princeton-nlp/SWE-bench` | 2,294 instances | Full benchmark |
| `princeton-nlp/SWE-bench_Lite` | 534 instances | Fast iteration |
| `princeton-nlp/SWE-bench_Verified` | 500 instances | Expert-verified solvable — **leaderboard target** |
| `SWE-bench/SWE-bench_Multimodal` | 100 dev + 500 test | Visual/UI elements |
| SWE-bench Multilingual | 300 instances | 9 languages, 42 repos |

## Instance Structure

Each problem instance contains:

### Identifiers
- `instance_id` — e.g., `django__django-11039`
- `repo` — GitHub repo (e.g., `django/django`)
- `issue_id` — GitHub issue number
- `base_commit` — commit SHA to apply patch on top of

### Content
- `problem_statement` — issue description shown to the model
- `version` — repo version string
- `issue_url` — GitHub issue URL
- `pr_url` — merged PR that fixed the issue

### Solutions
- `patch` — gold solution patch (ground truth)
- `test_patch` — test file changes

### Test Metadata
- `FAIL_TO_PASS` — tests that must change from failing to passing (the fix)
- `PASS_TO_PASS` — tests that must remain passing (no regression)

### Verified-specific
- `difficulty` — expert-estimated difficulty level

## Loading via Python

```python
from datasets import load_dataset

# Load verified test split
ds = load_dataset("princeton-nlp/SWE-bench_Verified", split="test")

# Access an instance
instance = ds[0]
print(instance["instance_id"])
print(instance["problem_statement"])
print(instance["FAIL_TO_PASS"])
```

## Instance ID Format

`<org>__<repo>-<issue_number>` → e.g., `django__django-11039`

## Key Notes

- **FAIL_TO_PASS** = these tests define what "solved" means. If they pass after patch → resolved.
- **PASS_TO_PASS** = regression guard. If any of these fail → not resolved (regression).
- `base_commit` is what Docker checks out before applying the patch.
- The `test_patch` is applied AFTER the model patch — it adds the test files that define FAIL_TO_PASS.
