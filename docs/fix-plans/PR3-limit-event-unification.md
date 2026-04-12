# PR 3 — Limit Event Unification

**Week:** 1 (parallel with PR1)
**Prerequisite:** none (uses existing DecisionLogger)

---

## Goal

Every configured limit that changes system behavior must emit a structured event. No more silent behavior changes.

Current: 51 hardcoded limits, NONE emit structured events when triggered.
After: every trigger is observable in decisions.jsonl + stdout.

---

## Event Schema

```json
{
  "type": "limit_triggered",
  "limit_name": "controlled_verify_class_limit",
  "configured_value": 20,
  "actual_value": 69,
  "action_taken": "fallback_scope",
  "phase": "EXECUTE",
  "attempt_id": "attempt_1",
  "file": "controlled_verify.py",
  "line": 89
}
```

### `action_taken` vocabulary:

* `fallback_scope` — scope changed due to limit
* `force_pass` — gate gave up enforcing
* `truncate` — output was cut
* `abort` — execution stopped
* `skip` — check was skipped
* `timeout` — operation timed out
* `bypass` — enforcement bypassed

---

## Unified Wrapper

```python
def emit_limit_triggered(
    state,
    *,
    step_n: int,
    limit_name: str,
    configured_value: int | float,
    actual_value: int | float,
    action_taken: str,
    source_file: str,
    source_line: int,
    reason: str = "",
) -> None:
    """Emit to BOTH stdout ([limit-triggered] prefix) and decisions.jsonl."""
    # stdout (for peek)
    print(
        f"    [limit-triggered] {limit_name}: configured={configured_value}"
        f" actual={actual_value} action={action_taken}"
        f" source={source_file}:{source_line}"
        f" reason={reason}",
        flush=True,
    )
    # decisions.jsonl (for replay)
    _emit_decision(
        state,
        decision_type="limit_triggered",
        step_n=step_n,
        verdict=action_taken,
        reason=f"{limit_name}: configured={configured_value} actual={actual_value} -- {reason}",
        signals={
            "limit_name": limit_name,
            "configured_value": configured_value,
            "actual_value": actual_value,
            "action_taken": action_taken,
            "source_file": source_file,
            "source_line": source_line,
        },
    )
```

For locations without `state` access: stdout-only print, caller emits structured event.

---

## Changes by File

### `step_sections.py` (highest priority — force-pass events)

| Audit ID | Line | limit_name | action_taken |
|----------|------|------------|-------------|
| C6/F4 | 1013-1014 | `fake_loop_force_pass` | `bypass` |
| C7 | 328-340 | `execute_redirect_limit` | `stop` |
| C8/F1 | 597, 613-614 | `analysis_gate_force_pass` | `force_pass` |
| C9/F2 | 676, 689-690 | `design_gate_force_pass` | `force_pass` |
| C10/F3 | 872, 883 | `retryable_loop_force_pass` | `force_pass` |

Note: C8/F1, C9/F2, C10/F3, C6/F4 are same event from different audit perspectives. Single emit per trigger point.

### `controlled_verify.py` (scope + timeout)

| Audit ID | Line | limit_name | action_taken |
|----------|------|------------|-------------|
| C1 | 89 | `cv_targeted_scope_max_classes` | `fallback_scope` |
| C2 | 343, 562 | `cv_total_timeout` | `timeout` |
| C3 | 83 | `cv_sentinel_max_classes` | `truncate` |
| C4 | 414+ (9 sites) | `cv_subop_timeout_<op>` | `timeout` |

### `jingu_agent.py`

| Audit ID | Line | limit_name | action_taken |
|----------|------|------------|-------------|
| C16 | 919 | `final_verify_timeout` | `timeout` |

### `jingu_adapter.py`

| Audit ID | Line | limit_name | action_taken |
|----------|------|------------|-------------|
| C12 | 27, 213 | `pytest_feedback_truncation` | `truncate` |

### `retry_controller.py`

| Audit ID | Line | limit_name | action_taken |
|----------|------|------------|-------------|
| C11 | 37 | `no_signal_threshold` | `stop` |

### `principal_inference.py`

| Audit ID | Line | limit_name | action_taken |
|----------|------|------------|-------------|
| C13 | 106 | `minimal_change_max_lines` | `skip` |

### `patch_reviewer.py`

| Audit ID | Line | limit_name | action_taken |
|----------|------|------------|-------------|
| C14 | 35 | `reviewer_max_tokens` | `truncate` |

### `run_with_jingu_gate.py`

| Audit ID | Line | limit_name | action_taken |
|----------|------|------------|-------------|
| C15 | 681 | `docker_pull_timeout` | `timeout` |

### `ops.py` (line 2325)

Add `"[limit-triggered]"` to `_PEEK_SIGNALS`.

---

## Implementation Order

1. Create `emit_limit_triggered()` helper in `step_sections.py`
2. Add `[limit-triggered]` to `_PEEK_SIGNALS` in `ops.py`
3. Instrument `step_sections.py` — C6-C10, F1-F4 (force-pass/bypass, highest priority)
4. Instrument `controlled_verify.py` — C1-C4 (scope + timeout)
5. Instrument `jingu_agent.py` — C16
6. Instrument remaining files — C11, C12, C13, C14, C15

---

## Acceptance Criteria

1. Run with django__django-10097 (triggers C1, C2, C5, C16)
2. `decisions.jsonl` contains `limit_triggered` events for every trigger
3. `ops.py peek` output shows `[limit-triggered]` lines
4. No limit trigger goes unlogged
5. Force-pass events (F1-F4) have full context (reject_count, scores, failed_rules)

---

## Effect

After this PR:

```json
{
  "limit_name": "CONTROLLED_VERIFY_CLASS_LIMIT",
  "actual_value": 69,
  "configured_value": 20,
  "action_taken": "fallback_scope"
}
```

This is 10x better than the current debugging experience.
