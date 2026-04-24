# Experiment: Mechanism Critic v1 — Shadow Mode 30-Instance Validation

## Config
- **Commit**: 57a10d4
- **Model (critic)**: bedrock/global.anthropic.claude-sonnet-4-6
- **Model (agent)**: claude-sonnet-4-6
- **Batch**: critic-shadow-30
- **Instances**: 30 (28 evaluated, 2 missing)
- **Attempts**: 2

## What Was Tested
Shadow-mode mechanism critic: after each attempt, a separate LLM call critiques the
agent's patch (problem + tests + patch, NO gold patch). Output written to
`jingu_body["mechanism_critic"]` as telemetry. Does NOT affect routing, retry, or prompts.

## Results

### Resolve Rate
- **21/29 resolved (72.4%)** — 1 missing from eval
- vs baseline 22/30 (73.3%) — non-regressive

### Critic Coverage
- **28/28 unique instances**: critic ran on ALL (0 skipped)
- All instances produce critic output (no patches were empty at the point of critic call)
- Average elapsed: ~6.3s per call

### Critical Finding: NO Discriminative Power

| Metric | Resolved (20) | Unresolved (8) |
|--------|--------------|----------------|
| Critic ran | 100% | 100% |
| Confidence=high | 60% (12/20) | 50% (4/8) |
| Confidence=medium | 40% (8/20) | 50% (4/8) |
| Avg mechanism text length | 150 chars | 150 chars |
| Critic detects issue | 100% | 100% |

**The critic ALWAYS finds "issues" — including in patches that successfully resolve the problem.**

The confidence distribution is nearly identical between resolved and unresolved instances.
There is no separation: critic signal cannot distinguish good patches from bad patches.

### Why This Happens
The critic is told "this patch did NOT fully resolve the issue" — so it always looks for
something wrong. Even correct patches have aspects that could be criticized (minor style,
alternative approaches, edge cases). The framing guarantees 100% detection rate with
no specificity.

## Decision
**KILL THIS LINE.**

Mechanism critic has zero predictive value. It cannot distinguish resolved from unresolved.
The fundamental design flaw: a critic that is told "find what's wrong" will always find
something, regardless of whether the patch is actually correct.

## Key Insight
LLM-as-critic for patch quality requires a discriminative framing ("is this patch correct?"),
not a generative framing ("what's wrong with this patch?"). But even discriminative framing
faces the same capability ceiling as the agent itself — if the model could reliably judge
patch correctness, it wouldn't need the critic.

## Commits
- 57a10d4: feat: add mechanism critic shadow mode (telemetry only)
- 295c033: docs: add mechanism critic v0 offline experiment results
