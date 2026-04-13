# Design: Step-Level Governance — Continuous Admission Control

## Status: DRAFT v1
## Date: 2026-04-13
## Context: p15 smoke test revealed governance gap — agent stuck in DECIDE 36 steps with no enforcement

---

## Problem Statement

Current governance model: **boundary-only control**
```
agent step = free (no governance)
phase advance = controlled (gate + admission)
```

p15 django__django-10999 evidence:
- Attempt 2: ANALYZE (14 steps) → DECIDE (36 steps, no submission, no protocol violation)
- Checkpoint escalation Level 1 (step 8) and Level 2 (step 15) fired warnings
- Agent ignored ALL warnings — continued free-form thinking in DECIDE
- No protocol violation because agent never attempted phase advance
- System "saw the problem" but had no mechanism to force behavior change

**Root cause**: Protocol violation only fires at VerdictAdvance. If agent never advances,
governance is a NO-OP inside the phase.

---

## Critical API Constraint (CONFIRMED)

`tool_choice={"type": "function", "function": {"name": "submit_phase_record"}}` forces
EXACTLY that tool. Agent CANNOT call bash in the same response.

**Implication**: We cannot force submit_phase_record on every step — it would kill
code exploration capability. The "tool-only step contract" approach is impossible
with the current API.

**Evidence**: Claude API docs + litellm behavior confirmed by code analysis.
- `tool_choice: "tool"` = exactly one tool, no parallel calls allowed
- `tool_choice: "any"` = must use at least one tool, but allows choice
- `tool_choice: "auto"` = default, may or may not call tools

---

## Architecture: Submission Deadline Pattern

Instead of "force every step", use "submission deadline with escalating enforcement":

```
Steps 1..K-2:  agent explores freely (tool_choice=auto, bash available)
Step K-1:      hard warning injected + force armed for next query
Step K:        tool_choice=submit_phase_record (bash blocked for 1 step)
Step K+1:      if still no submission → protocol_violation → STOP
```

**Key insight**: Agent loses bash for exactly 1 step (the forced step). This is acceptable
because:
1. Agent has had K-2 steps to explore already
2. The forced step is purely for summarizing findings into a phase record
3. After submission, agent resumes with bash access

### Phase-Specific Deadlines

| Phase | K (deadline) | Rationale |
|-------|-------------|-----------|
| OBSERVE | 15 | Longest exploration phase, needs many reads |
| ANALYZE | 12 | Moderate exploration, must converge on root cause |
| DECIDE | 8 | Must not stall — decision should be quick after analysis |
| EXECUTE | 10 | May need several iterations on patch |
| DESIGN | 10 | Similar to ANALYZE |
| UNDERSTAND | skip | No submission required |

---

## WS-1: Hard No-Submission Timeout

### Current State (p15 implementation)

```python
# step_sections.py lines 414-496
_CHECKPOINT_SOFT = 8   # Level 1: reminder message
_CHECKPOINT_HARD = 15  # Level 2: warning + set_force_phase_record(True)
# NO Level 3: no hard stop after force
```

### Problem
Level 2 arms `set_force_phase_record(True)`, but:
1. The forced step produces submit_phase_record call → intercepted → stored
2. **BUT**: the VerdictAdvance check hasn't fired yet (agent is mid-phase)
3. Protocol violation only fires when VerdictAdvance happens
4. If agent submits a phase record via force but stays in same phase, nothing happens

Wait — actually if the forced step makes agent call submit_phase_record, then
`pop_submitted_phase_record()` will return it at the next control plane evaluation.
The question is: does the control plane evaluate it?

### Investigation: What happens after forced submission?

After Level 2 fires and agent is forced to submit:
1. `_parse_actions()` intercepts the submit_phase_record call → stores in `_submitted_phase_record`
2. On the NEXT step, `_step_cp_update_and_verdict()` runs
3. At line 641, `pop_submitted_phase_record()` retrieves the stored record
4. Record is built and added to `state.phase_records`
5. `decide_next()` evaluates with new phase_records
6. If decide_next returns VerdictAdvance → gate evaluation → advance

**The gap**: If `decide_next()` doesn't return VerdictAdvance (e.g., because
the submitted record doesn't change the control plane's decision), the agent
stays in the same phase WITH a submitted record but WITHOUT advancing.

### Change Required

Add Level 3: **Terminal enforcement**

```python
_CHECKPOINT_TERMINAL = _CHECKPOINT_HARD + 3  # 3 steps after hard warning

if state._steps_without_submission >= _CHECKPOINT_TERMINAL:
    # Protocol violation — hard stop or force redirect
    if state._submission_escalation_level < 3:
        state._submission_escalation_level = 3
        state.early_stop_verdict = VerdictStop(
            reason=f"step_governance_timeout_{_current_phase_str}",
        )
        print(f"    [step-governance] TERMINAL: {_current_phase_str} "
              f"steps_without_submission={state._steps_without_submission} → STOP")
```

**Alternative**: Instead of STOP, force redirect to next phase:
```python
# Force advance: treat timeout as implicit phase completion
# Build a minimal phase record from diagnostic extraction
# Advance with warning flag
```

Decision: **STOP is safer for v1**. Force advance risks garbage-in propagation.

---

## WS-2: Submission Deadline with Force

### Current: one-shot force only at Level 2 boundary

The current `set_force_phase_record(True)` fires once at step 15 and resets.
If agent submits on that step but doesn't advance, the force is consumed.

### Change: Continuous force after deadline

After the deadline step K, force EVERY subsequent step until submission is consumed:

```python
if state._steps_without_submission >= _CHECKPOINT_HARD:
    # Re-arm force on EVERY step until agent submits
    if _model_peek is not None and hasattr(_model_peek, "set_force_phase_record"):
        _model_peek.set_force_phase_record(True)
```

This means:
- Steps 1..(K-1): free exploration with bash
- Steps K+: EVERY step forces submit_phase_record (no bash)
- Agent is "locked out" of exploration until it submits

### Risk: Agent produces junk submissions to escape the lock

Mitigation: submission goes through admission gate (principal + field checks).
If admission rejects, agent stays locked. After 3 rejections → STOP.

---

## WS-3: Failure Attribution + Targeted Retry

(Pending ws3-ws4 agent analysis — will be filled in after code research completes)

### Concept

After controlled_verify failure:
1. Classify failure: wrong_direction / incomplete_fix / regression / format_error
2. Route retry to specific phase (DECIDE / EXECUTE / DESIGN)
3. Inject specific repair hint from attribution

### Integration Points (from ws1-ws2 analysis)
- `controlled_verify` result available in step_sections.py
- Retry happens in `run_with_jingu_gate.py::run_agent()` between attempts
- `build_execution_feedback()` constructs retry hint
- `state.last_analyze_root_cause` carries cognition context

---

## WS-4: Exploration Enforcement

(Pending ws3-ws4 agent analysis)

### Concept

Track past decisions and patches. On retry, reject repeated approaches.

### Integration Points
- Decision content available from submitted phase records (DECIDE phase)
- Patch content available from `git diff` in step_sections.py
- Cross-attempt state needs to persist in `run_agent()` scope

---

## Implementation Priority

| WS | Change | Impact | Effort | Priority |
|----|--------|--------|--------|----------|
| WS-1 | Terminal enforcement (STOP after K+3) | Eliminates infinite stall | Small (add Level 3) | P0 — do first |
| WS-2 | Continuous force after deadline | Locks agent into submission | Small (re-arm force) | P0 — do with WS-1 |
| WS-3 | Failure attribution | Better retry routing | Medium (new module) | P1 — after WS-1 confirmed |
| WS-4 | Exploration enforcement | Prevents repeated errors | Medium (tracking state) | P2 — after WS-3 |

---

## Files to Modify

| File | WS | Change |
|------|-----|--------|
| `scripts/step_sections.py` | WS-1, WS-2 | Add Level 3 terminal, continuous force after deadline |
| `scripts/step_monitor_state.py` | WS-1 | Terminal escalation level tracking |
| `mini-swe-agent/jingu_model.py` | — | No change needed (force mechanism already works) |
| `scripts/attribution_engine.py` | WS-3 | New file: failure classification |
| `scripts/run_with_jingu_gate.py` | WS-3, WS-4 | Retry routing, cross-attempt state |

---

## Smoke Test Plan

After WS-1 + WS-2:
1. Run django__django-10999 — verify DECIDE stall terminates at step K+3 (not 36)
2. Run django__django-11095 — verify no regression (already resolved)
3. Check logs for `step_governance_timeout` events

After WS-3:
4. Run instance with wrong patch — verify attribution routes to DECIDE retry

After WS-4:
5. Run instance with repeated patch — verify rejection and new direction
