# Jingu SWE-bench Benchmark Results

## Final Results (v1.0)

> **Jingu delivers a consistent +3 uplift across model tiers (Sonnet 4.5 → 4.6). With Opus 4.6, Jingu pushes the ceiling to 23/30, showing additive gains with model scaling.**

### Four-Cell Attribution Matrix

| | Model-only (1 attempt) | + Jingu (2 attempts) | Jingu Δ |
|---|---|---|---|
| **Claude Sonnet 4.5** | 16/30 (53.3%) | 19/30 (63.3%) | **+3** |
| **Claude Sonnet 4.6** | 19/30 (63.3%) | 22/30 (73.3%) | **+3** |
| **Model Δ** | **+3** | **+3** | |

### Ceiling Run

| Model | Config | Resolved | Note |
|-------|--------|----------|------|
| **Claude Opus 4.6** | +Jingu (2 attempts) | **23/30** (76.7%) | 29/30 completed; 1 env_failure (django-11490) |

Opus 4.6 resolves 2 additional instances vs Sonnet 4.6 + Jingu: django-11206, django-11265.

- Dataset: SWE-bench Verified (30 Django instances)
- Config: best_config_v1 — EFR routing ON, all experimental features OFF
- Jingu governance: phase-aware retry routing with execution feedback

## Instance-Level Attribution

### Jingu-Only Resolved (not resolved by model alone)

| Model | Instances | Count |
|-------|-----------|-------|
| Sonnet 4.5 | django-10973, django-11400, django-11477 | 3 |
| Sonnet 4.6 | django-11141, django-11477, django-11490 | 3 |

- django-11477 is the only instance Jingu resolves on **both** models
- Jingu resolves **different** instances per model — not a fixed set

### Regression Recovery

Model upgrade (S4.5 → S4.6) caused 2 regressions in model-only:
- django-11141: resolved on S4.5, lost on S4.6 model-only, **recovered by Jingu**
- django-11490: resolved on S4.5, lost on S4.6 model-only, **recovered by Jingu**

Jingu acts as a **regression guard**: its retry mechanism compensates for model-version instability.

### Unresolved (8 instances, all wrong_patch)

10097, 10554, 10973, 10999, 11087, 11206, 11265, 11276

## What Jingu Does (and Doesn't Do)

### Active uplift driver
- **Phase-aware retry routing**: when first attempt fails, classify failure type → route to appropriate recovery phase (ANALYZE/DESIGN/EXECUTE) → 2nd attempt gets targeted context
- **Execution feedback**: test output from failed attempt fed back as structured signal

### Inactive / zero uplift (experimentally validated)
- Fix hypothesis ranking (mechanism correct, 0 behavioral uplift)
- Direction reconsideration (mechanism correct, 0 behavioral uplift)
- Multi-candidate selection (infeasible — blind diff fails)
- Design gate (non-constraining — 0% rejection rate)
- Wrong-direction routing (100% compliance, 0% effectiveness)

### Interpretation
Jingu's value is **process control** (retry routing, feedback injection), not **patch-level thinking aid** (hypothesis ranking, direction correction). The system amplifies model capability by providing structured recovery paths.

## Methodology

### Configuration (best_config_v1)

| Parameter | Value |
|-----------|-------|
| max_attempts | 2 (model-only: 1) |
| EFR routing | ON (phase-aware retry with execution feedback) |
| fix_hypothesis | OFF (0 behavioral uplift) |
| direction_recon | OFF (0 behavioral uplift) |
| candidate_selection | OFF (infeasible) |

### Dataset & Environment

- **Dataset**: SWE-bench Verified — 30 Django instances (from 231 Django total, 500 verified)
- **Evaluation**: Official SWE-bench harness (`swebench==4.1.0`, `run_evaluation`)
- **Base agent**: mini-swe-agent 2.1.0 (`ProgressTrackingAgent`)
- **Runtime**: ECS EC2 (c5.9xlarge), privileged Docker-in-Docker
- **Model access**: AWS Bedrock cross-region inference
- **Model strings**: `bedrock/global.anthropic.claude-sonnet-4-5`, `bedrock/global.anthropic.claude-sonnet-4-6`, `bedrock/global.anthropic.claude-opus-4-6-v1`

### Batches (chronological)

| Batch | Model | Config | Attempts | Resolved | Commit |
|-------|-------|--------|----------|----------|--------|
| baseline trunk-44d1c33 | S4.5 | model-only | 1 | 16/30 | 44d1c33 |
| best-config-v1 | S4.5 | +Jingu | 2 | 19/30 | e30aefa |
| ladder-sonnet46-modelonly-full30 | S4.6 | model-only | 1 | 19/30 | 436506e |
| ladder-sonnet46-full30 | S4.6 | +Jingu | 2 | 22/30 | 436506e |
| ceiling-opus46-full30 | Opus 4.6 | +Jingu | 2 | 23/30* | b6cfa92 |

*29/30 completed; 1 env_failure on django-11490 (does not affect main conclusion).

### Reproducibility

- All runs on same 30 Django instances from SWE-bench Verified
- Same Docker image (jingu-swebench:latest) on same ECS infrastructure
- Same mini-swe-agent 2.1.0 base agent, same evaluation harness
- Run artifacts (trajectories, predictions, eval results) stored in S3
- Stability verified: S4.6 +Jingu subset (11 instances) reproduced identically across two runs

### Fair comparison note

- "Model-only" = max_attempts=1 (single pass, no retry routing)
- "+Jingu" = max_attempts=2 (Jingu governance active on retry)
- Both use same base agent, same prompts, same evaluation harness
- All reported numbers use `best_config_v1` configuration
