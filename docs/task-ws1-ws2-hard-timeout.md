# Task: WS-1 + WS-2 — Hard Timeout + Continuous Force

## Goal
Eliminate DECIDE stall (36 steps with no governance). Add Level 3 terminal enforcement
and continuous tool_choice forcing after deadline.

## Changes

### Change 1: Level 3 Terminal Enforcement (step_sections.py)

In checkpoint escalation block (lines 414-496), add after Level 2:

```python
_CHECKPOINT_TERMINAL = _CHECKPOINT_HARD + 3  # 18 steps = terminal

if (
    state._steps_without_submission >= _CHECKPOINT_TERMINAL
    and state._submission_escalation_level < 3
    and _current_phase_str not in ("UNDERSTAND",)
):
    state._submission_escalation_level = 3
    state.early_stop_verdict = VerdictStop(
        reason=f"step_governance_timeout_{_current_phase_str.lower()}",
    )
    print(f"    [step-governance] TERMINAL: {_current_phase_str} "
          f"steps_without_submission={state._steps_without_submission} → STOP")
```

### Change 2: Continuous Force After Deadline (step_sections.py)

Replace the one-shot Level 2 force with continuous re-arming:

Current (Level 2, fires once):
```python
if state._steps_without_submission >= _CHECKPOINT_HARD and state._submission_escalation_level < 2:
    state._submission_escalation_level = 2
    # ... warning message ...
    _model_peek.set_force_phase_record(True)  # one-shot
```

New (fires every step after deadline):
```python
if state._steps_without_submission >= _CHECKPOINT_HARD and _current_phase_str not in ("UNDERSTAND",):
    if state._submission_escalation_level < 2:
        state._submission_escalation_level = 2
        # ... warning message (only once) ...
    # Re-arm force on EVERY step until agent submits
    if _model_peek is not None and hasattr(_model_peek, "set_force_phase_record"):
        _model_peek.set_force_phase_record(True)
```

### Change 3: Phase-Specific Deadlines (step_sections.py)

Replace fixed constants with phase-specific values:

```python
_PHASE_DEADLINES = {
    "OBSERVE": 15,
    "ANALYZE": 12,
    "DECIDE": 8,
    "EXECUTE": 10,
    "DESIGN": 10,
}
_DEFAULT_DEADLINE = 12
```

Use: `_deadline = _PHASE_DEADLINES.get(_current_phase_str, _DEFAULT_DEADLINE)`

## Verification

1. `python -m pytest tests/ -x` — all tests pass
2. Smoke test django__django-10999 — DECIDE stall terminates within deadline+3 steps
3. Smoke test django__django-11095 — no regression
