# Jingu × mini-SWE-agent — Integration Plan

## Strategic Direction

**Don't let Jingu generate patches. Let Jingu validate, reject, and retry.**

```
Issue
→ mini-SWE-agent (executes: reads files, edits, runs tests in Docker)
→ patch candidate
→ Jingu Layer (verify, reject reason, retry hint, candidate ranking)
→ best patch
→ SWE-bench harness (ground truth evaluation)
```

## Why mini-SWE-agent

- Official SWE-bench scaffold, >70% on Verified with minimal code (~100 lines)
- Extremely simple — easy to insert Jingu hooks
- Proven execution loop: file reading, shell, structured tool calls
- No custom patch parser needed

## Jingu's Role (Validator + Amplifier)

Jingu does NOT:
- Generate patches from scratch
- Parse LLM free text → diff
- Maintain file editor state

Jingu DOES:
- Gate: structural check, apply check, test check
- Retry hint: structured failure signal back to agent
- Rank: multi-attempt best-of-N selection
- Score: prefer small, single-file, targeted patches

## Integration Points (two hooks)

### Hook 1 — After patch generation
```ts
const patch = agent.generate_patch(issue)
const evalResult = jingu.evaluate(patch, context)
```

### Hook 2 — Retry with feedback
```ts
if (!evalResult.pass) {
  agent = agent.with_feedback(evalResult.retryHint)
  continue
}
```

## Retry Hint Format
```
Previous patch failed:

Error type: TEST_FAILED
Failing test: test_order_by_multiline_sql
Error: AssertionError at compiler.py:356

Fix this specific issue. Do not modify unrelated code.
```

## Scoring
```ts
score = 1000
score -= filesTouched.length * 10   // prefer single-file
score -= patchLines / 20            // prefer small patches
score += failToPassCount * 200      // reward fixing the right tests
```

## Execution Plan

### Day 1 (NOW) — Understand mini-SWE-agent
- [ ] Clone mini-SWE-agent, read source
- [ ] Identify patch output hook point
- [ ] Run 5 instances baseline (pass@1)

### Day 2 — Add Jingu Gate
- [ ] Add evaluate() after patch generation
- [ ] Add retry loop (max 3 attempts)
- [ ] Add structured failure hint to retry

### Day 3 — Add Ranking
- [ ] Collect all passing candidates across attempts
- [ ] Score and select best
- [ ] Run 50 instances: pass@1 vs pass@5(best)

### Day 4 — Full eval
- [ ] Run full SWE-bench Lite (300 instances)
- [ ] Report baseline vs Jingu-amplified

## Key Metrics
- pass@1 baseline (mini-SWE-agent alone)
- pass@5(best) (+ Jingu select_best)
- retry_success_rate (initially-failing → passing after hint)
- avg_attempts_to_pass

## Current jingu-swebench Status (as of Day 5)
- infra: complete (fuzz=25, filterPyOnly, dedup, file grounding)
- p162 reasoning protocol: implemented (analysis block + regex hint)
- results: 2/4 django resolved (10914, 11039)
- remaining bottleneck: reasoning failures (11001 regex, 11019 over-engineering)
- next pivot: replace custom patch generation with mini-SWE-agent

## Day 1 Findings (2026-03-31)

### mini-SWE-agent Status
- Installed: v2.2.8 ✓
- Modal sandbox: starts correctly (django-11039 image pulled in ~13s) ✓
- Hook point confirmed: `process_instance` → `agent.run()` → `info["submission"]` ✓

### Model Integration Blocker
- mini-SWE-agent uses litellm → Bedrock cross-region profile
- Error: `tool_choice.type: Field required` — `parallel_tool_calls` not compatible with Bedrock via litellm
- Fix: Write custom `BedRockModel` class for mini-SWE-agent that uses boto3 directly (same as jingu-swebench llm-client.ts)

### Next (Day 2)
- [ ] Write `scripts/bedrock_model.py` — custom Model class using boto3 Bedrock
- [ ] Write `scripts/run_mini_swe_agent.py` — uses process_instance with custom model
- [ ] Test: run django-11039 with custom model, get submission patch
- [ ] Add Jingu gate after submission

## Day 2 Findings (2026-03-31)

### LiteLLM Bug — Fixed
- Bug: `parallel_tool_calls` handler generates `_parallel_tool_use_config` with `tool_choice` dict missing `"type"` field
- Location: `litellm/llms/bedrock/chat/converse_transformation.py` line ~917
- Fix: patched `parallel_tool_calls` block to `pass` (skip generating the broken config)
- File: `.bak` backup created at `converse_transformation.py.bak`

### mini-SWE-agent Run — Working
- Model: `bedrock/global.anthropic.claude-sonnet-4-5-20250929-v1:0`
- Config: `parallel_tool_calls=false`  
- Environment: `swerex_modal` (Modal sandbox starts in ~2s, runtime ready in ~14s)
- django-11039: 44 steps, $0.37, correct patch generated
- Issue: `--exit-immediately` causes empty submission in traj (agent exits before COMPLETE cmd executes)
- Fix: remove `--exit-immediately` OR parse patch from log/traj messages

### Patch Quality
mini-SWE-agent correctly fixed django-11039:
```diff
-        self.output_transaction = migration.atomic
+        # and if the database can rollback DDL
+        self.output_transaction = migration.atomic and connection.features.can_rollback_ddl
```

### Next (Day 3)
- [ ] Fix submission extraction: drop --exit-immediately, use preds.json output instead
- [ ] Write scripts/run_with_jingu_gate.py: wraps process_instance, adds Jingu gate after submission
- [ ] Run baseline (mini-SWE-agent alone, 5 django instances) 
- [ ] Add retry loop: if gate fails, re-run agent with failure hint
- [ ] Compare pass@1 vs pass@3(best)
