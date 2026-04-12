# DESIGN A: Extract-as-Gate Implementation Design

**Plan**: PLAN-A-extract-as-gate.md
**Status**: DESIGN COMPLETE

---

## Architecture Change

One structural swap in `step_sections.py` VerdictAdvance handler:

```
BEFORE:  advance → extract → validate → (rollback if fail)
AFTER:   extract → validate → advance (only if all pass)
```

## Module Boundaries

```
step_sections.py
  _step_cp_update_and_verdict()
    └── VerdictAdvance handler (lines 414-1109)
         ├── [UNCHANGED] decide_next() produces VerdictAdvance
         ├── [MOVED]     phase advance → bottom of handler
         ├── [UNCHANGED] extraction block (structured_extract + regex fallback)
         ├── [NEW]       extraction failure gate (2-strike retry)
         ├── [UNCHANGED] cognition validation
         ├── [UNCHANGED] analysis gate
         ├── [UNCHANGED] design gate
         ├── [UNCHANGED] principal gate
         └── [NEW]       if all pass → advance phase

step_monitor_state.py
  StepMonitorState
    └── [NEW] extraction_retry_counts: dict[str, int]

jingu_agent.py
  JinguDefaultAgent._run_attempt()
    └── [NEW] reset extraction_retry_counts per attempt
```

## Detailed Changes

### Change 1: Defer phase advance (step_sections.py ~line 414-428)

Current code (approximate):
```python
# line 421-428
cp_state = dataclasses.replace(cp_state, phase=_new_phase)
state.cp_state = cp_state
```

New: Remove these lines from here. Add at the bottom of the handler, after all gates pass:
```python
# AFTER all gates pass
cp_state = dataclasses.replace(cp_state, phase=_new_phase)
state.cp_state = cp_state
```

Variables `_old_phase` and `_new_phase` still computed at the top. `_eval_phase = _old_phase` (the phase being validated).

### Change 2: Extraction failure gate (step_sections.py, after extraction block ~line 558)

```python
if _pr is None:
    _ext_key = _eval_phase
    _ext_retries = state.extraction_retry_counts.get(_ext_key, 0)
    if _ext_retries < 2:
        state.extraction_retry_counts[_ext_key] = _ext_retries + 1
        state._phase_accumulated_text.pop(_eval_phase, None)
        if hasattr(state, '_phase_record_cache'):
            state._phase_record_cache.pop(_eval_phase, None)
        _emit_limit_triggered(
            state, step_n=_step_n,
            limit_name="extraction_retry",
            configured_value=2,
            actual_value=_ext_retries + 1,
            action_taken="retry_current_phase",
            source_file="step_sections.py",
            source_line=0,  # will be set at impl time
            reason=f"extraction failed for {_eval_phase}, retrying",
        )
        agent_self.messages.append({
            "role": "user",
            "content": (
                f"[Phase Gate: EXTRACTION FAILED]\n"
                f"Your {_eval_phase} phase output could not be parsed.\n"
                f"Restate your {_eval_phase} findings with the required structure:\n"
                f"  PHASE: {_eval_phase.lower()}\n"
                f"  PRINCIPALS: <required principals>\n"
                f"  <phase-specific content>\n"
                f"Retry {_ext_retries + 1}/2."
            ),
        })
        return  # stay in current phase, do not advance
    else:
        _emit_limit_triggered(
            state, step_n=_step_n,
            limit_name="extraction_force_advance",
            configured_value=2,
            actual_value=_ext_retries + 1,
            action_taken="force_advance_no_record",
            source_file="step_sections.py",
            source_line=0,
            reason=f"extraction failed {_ext_retries + 1} times for {_eval_phase}, force advancing",
        )
        # Fall through to advance with no record
```

### Change 3: Simplify existing gate rollbacks

Currently each gate does on reject:
```python
cp_state = dataclasses.replace(cp_state, phase=_old_phase)  # rollback
state.cp_state = cp_state
```

With the new flow, phase was never advanced, so this becomes:
```python
# phase was not yet advanced — no rollback needed
# just inject feedback and return
```

Keep the feedback injection and cached record cleanup unchanged.

### Change 4: Phase advance at the bottom

After all gates pass (all existing gates + new extraction gate):
```python
# All gates passed — now advance
cp_state = dataclasses.replace(cp_state, phase=_new_phase)
state.cp_state = cp_state
# existing post-advance logic (prompt injection for new phase, etc.)
```

### Change 5: StepMonitorState (step_monitor_state.py)

Add field:
```python
self.extraction_retry_counts: dict[str, int] = {}
```

### Change 6: Reset per attempt (jingu_agent.py)

In `_run_attempt()` or equivalent, alongside existing counter resets:
```python
_monitor.extraction_retry_counts = {}
```

## Non-Changes (explicitly preserved)

- `structured_extract()` in JinguModel — unchanged
- `extract_record_for_phase()` regex fallback — unchanged
- `evaluate_admission()` in principal_gate — unchanged
- `_infer_principals()` in principal_inference — unchanged
- All gate thresholds (analysis max 2, principal max 3) — unchanged
- Phase prompt injection logic — unchanged

## Test Plan

| Test | What it verifies |
|------|-----------------|
| extraction_failure_retries | None return → feedback injected, phase stays |
| extraction_force_advance | 2 failures → force advance, limit_triggered event |
| happy_path_advance | extract OK + gates pass → phase advances |
| gate_reject_no_rollback | extract OK + gate rejects → phase stays (no rollback needed) |
| smoke_1_instance | end-to-end on real SWE-bench instance |

## Implementation Order

1. `step_monitor_state.py` — add field (1 line)
2. `step_sections.py` — refactor VerdictAdvance handler (main work)
3. `jingu_agent.py` — reset counter (1 line)
4. Smoke test locally (dry run)
5. Build image + smoke test on ECS
