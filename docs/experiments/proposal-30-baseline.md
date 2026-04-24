# Experiment: Structured Proposal Record v0 — 30-Instance Baseline

## Config
- **Commit**: 341dcc7
- **Model**: claude-sonnet-4-6
- **Attempts**: 2
- **Batch**: proposal-30-baseline
- **Baseline**: ladder-sonnet46-full30 (22/30, commit 436506e)

## What Was Tested
`build_proposal_record()` extracts agent intent from existing phase_records:
- target_files (from DESIGN/ANALYZE records)
- actual_files_written (from patch)
- failure_diagnosis (from ANALYZE.root_cause)
- intent_vs_outcome overlap (target vs actual alignment)
- principal_used, confidence

Telemetry only — no gating, no behavior change.

## Results

### Resolve Rate
- **20/29 resolved** (69.0%) — 2 instances missing vs 30-instance set
- Within LLM variance range of 22/30 baseline

### Proposal Record Metrics
| Metric | Value |
|--------|-------|
| Proposal present | 28/28 (100%) |
| Target files empty | 0/28 (0%) |
| Diagnosis empty | 0/28 (0%) |
| Overlap < 1.0 | 7/28 (25%) |
| Overlap = 0.0 | 4/28 (14.3%) |
| Confidence mean | 0.95 (range 0.90-1.00) |

### Overlap vs Resolved Correlation
| Overlap | Resolve rate |
|---------|-------------|
| = 1.0 | 14/21 (66.7%) |
| < 1.0 | 4/7 (57.1%) |

Correlation is **weak** (~10pp difference). Not actionable as a gate.

### Overlap < 1.0 Breakdown
- 4/7 instances with overlap<1.0 **still resolved** (11066, 11333, 11400, 11149)
- "Missing from plan" files are usually test files or related modules correctly added
- Hard gate on overlap would **regress** 4 resolved instances

## Decision
- **No hard gate** — overlap does not strongly predict failure
- **Keep telemetry** — proposal_record stays in jingu_body for observability
- **Confidence not discriminating** — uniformly 0.90-1.00, not useful
- **Agent structured output is already rich** — 100% target/diagnosis coverage

## Key Insight
The bottleneck is not intent declaration (agent always knows what it wants to do).
The bottleneck is search/discovery ability (agent sometimes can't find the right code).
