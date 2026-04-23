# Why +3? A Deep Dive into Jingu's SWE-bench Uplift

## The Failure Landscape

On SWE-bench Verified (30 Django instances), the baseline model-only agent (Sonnet 4.5) resolves 16/30 on a single attempt. The 14 unresolved instances share a common pattern: **wrong_patch** — the agent reaches the execution phase, produces a patch, but the patch is incorrect.

Breaking down the 14 failures:
- **wrong_direction** (8): agent modifies the wrong file or wrong code region
- **incomplete_fix** (3): correct location, but patch is partial
- **verify_gap** (2): patch looks right but fails edge cases
- **environment** (1): infra failure, not agent failure

The critical insight: **all 14 failures reach EXECUTE phase**. The agent doesn't get stuck — it completes the task loop but produces the wrong output.

## What We Tried (and What Didn't Work)

### Dead Lines (validated zero uplift)

| Mechanism | Hypothesis | Result | Why It Failed |
|-----------|-----------|--------|---------------|
| Fix hypothesis ranking | Rank multiple fix approaches before coding | 0/4 uplift | Agent generates hypotheses post-hoc, not as genuine alternatives |
| Direction reconsideration | Force agent to reconsider file choice | 0/4 uplift | 5/6 wrong_direction cases target the correct file — they're wrong_patch, not wrong_file |
| Multi-candidate selection | Generate N patches, pick best | Infeasible | Blind diff comparison cannot distinguish patch quality |
| Design gate | Require structured design before coding | 0% rejection | Agent fills all contract fields on first try (non-constraining) |
| Wrong-direction routing | Route agent to different files on retry | 0/8 effectiveness | 100% compliance (agent switches files), but picks wrong new direction |

The pattern: **patch-level thinking aids don't help because the failure is at the capability frontier, not at the process level.** The agent isn't failing because it forgot to think — it's failing because the fix requires understanding it doesn't have.

### What Works: Process Control (+3)

The only mechanism that produces uplift is **EFR (Execution Feedback Routing)**:

1. First attempt runs normally (single pass)
2. If tests fail, classify the failure type from test output
3. Route the retry to the appropriate recovery phase:
   - `incomplete_fix` → DESIGN (rethink the approach)
   - `wrong_direction` → ANALYZE (find better target)
   - `verify_gap` → EXECUTE (fix edge cases)
4. Inject structured execution feedback (failing test names, output excerpts)

This produces +3 resolved instances on both Sonnet 4.5 and Sonnet 4.6.

## Why +3 Is Stable

The +3 uplift is remarkably consistent:

| Axis | Δ | Interpretation |
|------|---|---------------|
| S4.5 → S4.6 (model-only) | +3 | Model upgrade gains |
| S4.5 → S4.6 (+Jingu) | +3 | Same upgrade gains preserved under governance |
| S4.5: model-only → +Jingu | +3 | Governance uplift on weaker model |
| S4.6: model-only → +Jingu | +3 | Governance uplift on stronger model |

The symmetry means: **governance and model capability are orthogonal**. They operate on different failure modes and neither diminishes the other.

### Jingu Resolves Different Instances Per Model

| Model | Jingu-only resolved |
|-------|-------------------|
| Sonnet 4.5 | django-10973, django-11400, django-11477 |
| Sonnet 4.6 | django-11141, django-11477, django-11490 |

Only django-11477 is recovered on both models. This confirms that Jingu's uplift is not from a fixed set of "easy retries" — it adapts to each model's specific failure pattern.

### Regression Recovery

Model upgrade (S4.5 → S4.6) caused 2 regressions in model-only mode:
- django-11141 and django-11490: resolved on S4.5, lost on S4.6 model-only

Both were **recovered by Jingu** on S4.6. Jingu acts as a regression guard — its retry mechanism compensates for model-version instability.

## The Opus 4.6 Ceiling

With the strongest model (Opus 4.6) + Jingu governance: **23/30 (76.7%)**.

This resolves 2 additional instances beyond Sonnet 4.6 + Jingu: django-11206 and django-11265, both in the `wrong_direction` category that no process-level intervention could fix on Sonnet.

The remaining 7 unresolved instances represent the current capability ceiling — they require deeper code understanding that neither governance nor model upgrade currently provides.

## Architectural Implication

Jingu's value is **process control**, not **patch-level reasoning**:

- Retry routing = effective (+3 stable)
- Patch-level aids (hypothesis, direction, selection) = ineffective (0 uplift)

This suggests a general principle for LLM agent governance:

> **Don't try to make the agent think better. Give it structured recovery paths when it thinks wrong.**

The agent's reasoning within a phase is unconstrained and effective. What it lacks is the ability to recover from a failed attempt with appropriate context. Governance provides that recovery structure.

## Methodology

- Dataset: SWE-bench Verified, 30 Django instances
- Config: `best_config_v1` (EFR ON, all dead lines OFF)
- Agent: mini-swe-agent 2.1.0
- Evaluation: Official SWE-bench harness (swebench==4.1.0)
- Runtime: ECS EC2 c5.9xlarge, Docker-in-Docker
- Models: AWS Bedrock cross-region inference
- Reproducible: `./scripts/reproduce_benchmark.sh`
- Tag: `v1.0-benchmark`
