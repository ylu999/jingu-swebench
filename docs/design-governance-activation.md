# Design: Governance Activation — Wire Bundle to Runtime

## Status: DRAFT v2 (revised after code + data analysis)
## Date: 2026-04-13
## Batch Context: p14 (18/30 = 60%, 12 unresolved all wrong-patch, 0 infra failures)

---

## Problem Statement (Revised)

The original hypothesis was: "gates don't exist, agent skips analysis."

**After analyzing all 12 unresolved instances' decisions.jsonl + the actual gate code,
the real problem is different and more nuanced.**

### What Already Exists (more than expected)

The gate infrastructure is **complete**:

| Component | Status | Evidence |
|-----------|--------|----------|
| `evaluate_admission()` | Fully implemented | Checks required_principals, forbidden_principals, required_fields, root_cause, plan_grounding, evidence_basis, allowed_next |
| `submit_phase_record` tool | Implemented | Agent can call it; `pop_submitted_phase_record()` on model |
| Phase completion gate | Implemented | No transition without admitted PhaseRecord |
| Protocol violation detection | Implemented | 3 retries then STOP |
| Materialization gate (K=2) | Implemented | Forces patch after 2 steps in EXECUTE |
| Bundle repair hints | Partially wired | `_gov_repair.get_repair_hint()` called in step_sections.py |
| Phase schemas | Complete | ANALYZE/EXECUTE/JUDGE/OBSERVE/DECIDE/DESIGN all defined |
| Structured output tool | Implemented but disabled | `STRUCTURED_OUTPUT_ENABLED=false` by default |
| Analysis gate (force_pass) | Implemented | Checks principal scores, but force_pass after 2 retries |

### What the Data Actually Shows

Three distinct failure patterns across the 12 unresolved instances:

**Pattern A: Protocol Violation → Stop (4 cases: 10097, 10999, 11066, 11265)**
- Agent never calls `submit_phase_record` → system retries 3× → STOP
- Phases stuck at: OBSERVE (10097, 11265), ANALYZE (10999), DECIDE (11066)
- Root cause: Agent doesn't use the structured tool. It writes free-text analysis
  but the gate requires the tool call. Prompt says "call submit_phase_record" but
  agent ignores or doesn't understand.

**Pattern B: Execute No-Progress Loop → Stop (3 cases: 10554, 11087, 11333)**
- Agent enters EXECUTE, materialization gate fires (no patch after K=2 steps)
- Redirected to DECIDE/ANALYZE but loops → redirect limit exceeded → STOP
- Root cause: Agent is in EXECUTE but exploring/reading instead of writing.
  System correctly detects stall, redirects correctly, but agent doesn't change
  behavior on redirect.

**Pattern C: Completes Both Attempts, Wrong Patch (5 cases: 11141, 11206, 11292, 11400, 11433)**
- Agent submits phase records, passes gates, writes patches — but patches are wrong.
- Key evidence from 11292: `analysis_gate_force_pass` with `alternative_hypothesis=0.0`,
  `failed_rules=['invariant_capture']`. Gate detected weak analysis but gave up after
  2 retries and force-passed.
- Key evidence from 11206/11400: Only 4 decisions total — OBSERVE→ANALYZE→EXECUTE,
  zero rejections. Gates passed everything immediately.
- Root cause: Gate thresholds too lenient. Agent submits formally correct but
  semantically shallow phase records. The gate admits them because the structural
  fields are present (root_cause string exists, even if shallow).

### The Real Gap (revised)

| Problem | Pattern | Affected | Impact |
|---------|---------|----------|--------|
| Agent doesn't call `submit_phase_record` tool | A | 4 cases | System can't evaluate — stops |
| Redirect hint insufficient to change behavior | B | 3 cases | Agent loops same strategy |
| Analysis gate force_pass too lenient | C | 5 cases | Weak analysis passes through |
| Structured output disabled | A+C | 9 cases | Agent not forced into schema |

---

## Design Principle (Revised)

**Not building new gates. Not wiring new checks. The checks exist.**

**The real interventions are:**
1. **Make the agent actually use `submit_phase_record`** → eliminates Pattern A
2. **Make redirect hints more actionable** → reduces Pattern B
3. **Remove or tighten `force_pass` on analysis gate** → catches Pattern C
4. **Enable structured output** → forces schema compliance, eliminates Protocol Violations

---

## Architecture: What Changes

### Intervention 1: Enable Structured Output (highest impact, affects Pattern A + C)

**Current**: `STRUCTURED_OUTPUT_ENABLED=false`. Agent writes free text. `submit_phase_record`
is a tool the agent CAN call but often doesn't. When it doesn't, protocol violation → STOP.

**Change**: Enable structured output for all phases with schemas.

```python
# In run_with_jingu_gate.py or step_sections.py:
# STRUCTURED_OUTPUT_ENABLED = True (via env var or code default)
#
# When enabled:
# - Model is given phase-specific tool (submit_phase_record) with JSON schema
# - Model MUST call the tool (tool_choice forced)
# - Schema guarantees required fields are present
# - Eliminates Pattern A entirely (agent MUST submit structured record)
# - Improves Pattern C (schema enforces minLength, required arrays)
```

**Risk**: Forcing structured output changes the agent's interaction pattern significantly.
May break existing working behaviors.

**Mitigation**: Phase-gated enablement:
- Stage 1: ANALYZE only (highest value — forces root_cause, evidence, alternatives)
- Stage 2: EXECUTE (forces plan, change_scope)
- Stage 3: OBSERVE, DECIDE, JUDGE (lower priority)

### Intervention 2: Remove analysis_gate force_pass (Pattern C)

**Current**: After 2 RETRYABLE results from analysis gate, `force_pass` lets agent through
even with `alternative_hypothesis=0.0`.

**Change**: Replace `force_pass` with `redirect_to_observe` — if analysis is weak after
2 retries, send agent back to gather more evidence instead of letting them proceed
with a weak analysis.

```python
# BEFORE (step_sections.py):
# analysis_gate_force_pass after 2 retries → advance to EXECUTE

# AFTER:
# analysis_gate after 2 retries → redirect to OBSERVE with specific hint
# "Your analysis is incomplete. Return to OBSERVE and gather evidence for:
#  - alternative hypotheses (you had 0)
#  - invariant capture
#  Then re-enter ANALYZE."
```

**Risk**: Agent may loop OBSERVE→ANALYZE→OBSERVE indefinitely.
**Mitigation**: Max 2 OBSERVE redirects from analysis gate, then force_pass with warning.

### Intervention 3: Enrich Redirect Hints (Pattern B)

**Current**: `execute_no_progress` redirect says "go to DECIDE" but doesn't explain
what specifically to do differently.

**Change**: Include patch status and phase record info in redirect hint.

```python
# BEFORE:
# "[gate] execute_no_progress → redirect to DECIDE"

# AFTER:
# "[gate] execute_no_progress: You've spent N steps in EXECUTE without writing
#  a file. Your analysis identified {root_cause_summary}. Instead of exploring,
#  directly edit the file mentioned in your analysis. If your analysis is wrong,
#  return to ANALYZE with new evidence."
```

This uses the last admitted ANALYZE phase record's `root_cause` field to make
the redirect actionable.

### Intervention 4: Bundle Repair Templates (already partially wired)

**Current**: `_gov_repair.get_repair_hint()` is called in step_sections.py but the
`_FEEDBACK` dict in principal_gate.py is still the primary source for gate feedback.

**Change**: Complete the wiring — make `_build_admission_rejection()` use bundle
repair templates first, falling back to `_FEEDBACK` dict.

```python
# In principal_gate.py _build_admission_rejection():
# For each violation, try bundle repair hint first:
#   hint = gov.get_repair_hint(phase, principal) if gov else ""
#   if not hint:
#       hint = _FEEDBACK.get(principal, "")
```

### Intervention 5: Tighten required_fields in contracts

**Current**: `subtype_contracts.py` has empty `required_fields` for most phases.
Only `analysis.root_cause` (via cognition_contracts) has real required fields.

**Change**: Add required_fields to key phases:

```python
# analysis.root_cause: already has required_fields from cognition_contracts
# execution.code_patch: add required_fields = ["plan"]
# (plan is already checked by 3a in evaluate_admission, but having it in
#  required_fields makes it contract-driven not hardcoded)
```

---

## What This Design Does NOT Do

1. **Does not add new gates** — existing gates are sufficient
2. **Does not add new principals** — uses existing 14 rules
3. **Does not add new phases** — uses existing 7 phases
4. **Does not change admission semantics** — ADMITTED/RETRYABLE/REJECTED unchanged
5. **Does not change bundle format** — uses existing compiled bundle
6. **Does not add QJ→control plane wiring** — deprioritized (QJ parse bug must be
   fixed first; QJ signals are useful but not the bottleneck)

---

## Expected Impact on the 12 Unresolved Cases

### Pattern A: Protocol Violation (10097, 10999, 11066, 11265)

**Intervention 1 (structured output)**: Eliminates this pattern entirely.
With forced tool_choice, agent MUST submit structured phase record.
No more "agent ignores submit_phase_record" — it's the only output path.

**Expected**: 4/4 cases no longer stop at protocol violation.
Whether they then produce correct patches depends on analysis quality.

### Pattern B: Execute No-Progress (10554, 11087, 11333)

**Intervention 3 (redirect hints)**: More actionable redirects that reference
the agent's own analysis should break the loop pattern.

**Intervention 1 (structured output)**: With structured EXECUTE requiring `plan`
field, agent must state a plan before being in EXECUTE — may prevent the
"enter EXECUTE without knowing what to do" pattern.

**Expected**: 1-2 of 3 cases improve.

### Pattern C: Wrong Patch (11141, 11206, 11292, 11400, 11433)

**Intervention 2 (remove force_pass)**: 11292 specifically had weak analysis
(alternative_hypothesis=0.0) that was force-passed. Without force_pass,
agent would be redirected to OBSERVE for more evidence.

**Intervention 1 (structured output)**: ANALYZE schema requires root_cause
(minLength:20), evidence array, and alternative_hypotheses (minItems:1).
11206/11400 which passed with minimal analysis would be forced to provide
more complete records.

**Expected**: 2-3 of 5 cases improve.

### Overall Expected Impact

| Pattern | Current | Expected after | Net gain |
|---------|---------|----------------|----------|
| A (protocol violation) | 0/4 | 2-4/4 | +2 to +4 |
| B (execute stall) | 0/3 | 1-2/3 | +1 to +2 |
| C (wrong patch) | 0/5 | 2-3/5 | +2 to +3 |
| **Total** | **18/30** | **23-27/30** | **+5 to +9** |

Conservative target: **23/30 (77%)**. Optimistic: **25/30 (83%)**.

---

## Implementation Order (4 stages, prioritized by impact)

### Stage 1: Enable Structured Output for ANALYZE (highest impact)

Why first: Eliminates Pattern A (4 cases) + improves Pattern C (5 cases).
This single change addresses 9 of 12 unresolved cases.

Changes:
- Set `STRUCTURED_OUTPUT_ENABLED=true` for ANALYZE phase
- Use existing `ANALYZE_RECORD_SCHEMA` from phase_schemas.py
- Use existing `submit_phase_record` tool mechanism
- Force `tool_choice` so agent MUST call it

Files:
- `scripts/step_sections.py` — enable structured output for ANALYZE
- `scripts/run_with_jingu_gate.py` — env var default or per-phase config

Test: Run 1 instance, verify agent submits structured ANALYZE record with
root_cause, evidence, alternative_hypotheses.

### Stage 2: Remove analysis_gate force_pass

Why: 11292 showed weak analysis force-passed. This is the leniency gap.

Changes:
- Replace `force_pass` with redirect to OBSERVE
- Add redirect hint with specific missing signals
- Cap at 2 OBSERVE redirects, then force_pass with warning

Files:
- `scripts/step_sections.py` — modify analysis_gate_force_pass logic

Test: Run instance where analysis has alternative_hypothesis=0.0, verify
redirect to OBSERVE instead of force_pass.

### Stage 3: Enrich Redirect Hints for Execute Stall

Why: Pattern B (3 cases) — redirects don't include actionable context.

Changes:
- Include last ANALYZE root_cause in redirect hint
- Reference specific files from analysis evidence

Files:
- `scripts/step_sections.py` — modify execute_no_progress redirect message

Test: Run instance that stalls in EXECUTE, verify redirect includes
root_cause reference.

### Stage 4: Enable Structured Output for EXECUTE + remaining phases

Why: Extends Stage 1 benefit to more phases.

Changes:
- Enable structured output for EXECUTE (requires plan field)
- Enable for OBSERVE, DECIDE if Stage 1 is stable

Files:
- Same as Stage 1 but broader scope

Test: Verify EXECUTE submissions include plan, change_scope.

---

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|-----------|
| Structured output changes agent behavior unpredictably | High | Stage 1: ANALYZE only. Smoke test before batch. |
| Agent can't produce valid schema → infinite retry | Medium | Existing protocol_violation_stop after 3 retries still applies |
| Removing force_pass → OBSERVE↔ANALYZE loop | Medium | Cap at 2 OBSERVE redirects |
| Richer hints bloat context window | Low | Hints are 1-2 sentences, not paragraphs |
| Structured output incompatible with Bedrock model version | Medium | Test with current model before committing |

---

## Files to Modify

| File | Change | Stage |
|------|--------|-------|
| `scripts/step_sections.py` | Enable structured output per-phase; remove force_pass; enrich redirects | 1, 2, 3 |
| `scripts/run_with_jingu_gate.py` | Config for structured output enablement | 1 |
| `scripts/phase_schemas.py` | Verify schemas (already complete) | 1 |
| `scripts/principal_gate.py` | Bundle repair template fallback (intervention 4) | 4 |

---

## Data Appendix: All 12 Unresolved Failure Classifications

### Pattern A: Protocol Violation → Stop

| Instance | Attempt | Phase | Detail |
|----------|---------|-------|--------|
| 10097 | a2 | OBSERVE | 3 retries → protocol_violation_missing_phase_record |
| 10999 | a2 | ANALYZE | 3 retries → protocol_violation_missing_phase_record |
| 11066 | a2 | DECIDE | 3 retries → protocol_violation_missing_phase_record |
| 11265 | a2 | OBSERVE | 3 retries → protocol_violation_missing_phase_record |

### Pattern B: Execute No-Progress → Stop

| Instance | Attempt | Detail |
|----------|---------|--------|
| 10554 | a2 | materialization_gate force × 3, execute_no_progress redirect × 3 |
| 11087 | a2 | materialization_gate force × 2, redirect × 4, execute_redirect_limit (4>3) → stop |
| 11333 | a2 | materialization_gate force × 2, redirect × 4, execute_redirect_limit → stop |

### Pattern C: Completes, Wrong Patch

| Instance | Attempt | Detail |
|----------|---------|--------|
| 11141 | a1 | OBSERVE→ANALYZE→EXECUTE clean (1 protocol retry at ANALYZE then passed) |
| 11206 | a1 | OBSERVE→ANALYZE→EXECUTE clean (only 4 decisions — minimal analysis) |
| 11292 | a1 | analysis_gate_force_pass (alt_hypothesis=0.0, failed invariant_capture) |
| 11400 | a1 | OBSERVE→ANALYZE→EXECUTE clean (4 decisions — minimal analysis) |
| 11433 | a1 | 2 protocol violations at OBSERVE, then eventually advanced |

### Common Pattern in Attempt 2

All 12 instances show a degraded attempt 2:
- Pattern A: Protocol violation immediately (agent learned nothing from attempt 1)
- Pattern B: Materialization gate fires immediately (agent jumps to EXECUTE)
- Pattern C: Materialization gate fires (attempt 2 starts in EXECUTE, agent explores)

This suggests attempt 2's retry_hint is insufficient — agent doesn't get
enough context about WHY attempt 1 failed. The NBR (No Blind Retry) principle
may not be fully effective here.

---

## Success Criteria

After implementation, running the same 30 instances should show:
1. **Zero protocol violations** in ANALYZE phase (structured output eliminates them)
2. **All ANALYZE records have root_cause + evidence** (schema enforces it)
3. **No force_pass with alternative_hypothesis=0.0** (force_pass removed or tightened)
4. **Execute redirects include root_cause reference** (actionable hints)
5. **Quantitative: ≥23/30 resolved (77%)**
