# PR 4 — Controlled Verify Scheduler v2

**Week:** 3 (after PR1 + PR3)
**Prerequisite:** PR1 (bundle loads), PR3 (limit events)

---

## Goal

Completely remove the "overflow -> module fallback -> timeout" bad chain.
Replace with batched targeted verify that always produces signal.

See `verify-scheduler-v2.md` for full design and pseudocode.

---

## Changes

### New File: `scripts/verify_scheduler.py`

Core scheduling logic:
* `collect_verify_candidates()` — gather F2P + patch-related + sentinel classes
* `choose_class_budget()` — dynamic budget (fraction * total, capped)
* `build_batches()` — split into batches of 10
* `should_early_stop()` — 2 consecutive timeouts -> shrink
* `run_ultra_small_subset()` — last-resort 5-test fallback

### New File: `scripts/verify_types.py`

Data structures:
* `VerifyScopeMode`
* `VerifyBatch`
* `VerifySchedulerPlan`
* `VerifyBatchResult`
* `VerifyFinalResult`

### Modified File: `scripts/controlled_verify.py`

1. **Remove 20-class hard limit** (line 89)

From:
```python
if all_labels and len(all_labels) <= 20:
```

To: use `verify_scheduler.choose_class_budget()` — always targeted when labels exist.

2. **Replace fixed timeout** (line 343)

From:
```python
timeout_s: int = 60
```

To:
```python
timeout_s: int | None = None  # None = auto from scheduler
```

3. **Batched execution**

Replace single subprocess run with batch loop:
```python
for batch in batches:
    result = run_single_batch(batch)
    results.append(result)
    if should_early_stop(results):
        break
```

4. **No-signal recovery**

```python
if not any(r.signal_observed for r in results):
    emit("verify_no_signal", ...)
    fallback_result = run_ultra_small_subset(ctx)
    results.append(fallback_result)
```

5. **Return partial results**

```python
return {
    "verification_kind": "controlled_fail_to_pass",
    "tests_passed": total_passed,
    "tests_failed": total_failed,
    "partial": any(r.timeout for r in results),
    "signal_observed": any(r.signal_observed for r in results),
    "mode": plan.mode,
    "batches_run": len(results),
    "batches_timeout": sum(1 for r in results if r.timeout),
}
```

### Modified File: `scripts/step_sections.py` (line 239)

From:
```python
cv_result = run_controlled_verify(
    patch, state.instance, container, timeout_s=45,
    apply_test_patch=False,
)
```

To:
```python
cv_result = run_controlled_verify(
    patch, state.instance, container, timeout_s=None,
    apply_test_patch=False, in_loop=True,
)
```

### Modified File: `scripts/jingu_agent.py` (line 919)

From:
```python
timeout_s=60
```

To:
```python
timeout_s=None  # auto from scheduler
```

---

## Verify Config (from bundle)

```json
{
  "verify": {
    "strategy": "batched_targeted",
    "require_signal": true,
    "allow_partial_signal": true,
    "scope_selection": {
      "max_class_fraction": 0.3,
      "max_classes_hard_cap": 40,
      "min_classes_floor": 5,
      "fallback_strategy": "shrink_batch"
    },
    "batching": {
      "enabled": true,
      "batch_size": 10,
      "max_batches": 4
    },
    "timeouts": {
      "per_batch_seconds": 20,
      "overall_seconds": 90,
      "docker_subprocess_seconds": 30
    },
    "no_signal_policy": {
      "treat_as_error": true,
      "retry_with_ultra_small_subset": true
    }
  }
}
```

---

## New Events

* `verify_scope_selected` — mode, candidate_count, selected_count, batch_count
* `verify_batch_started` — batch_id, class_count, timeout_seconds
* `verify_batch_completed` — batch_id, passed, failed, timeout, signal_observed
* `verify_no_signal` — mode, action_taken

---

## New Files for Dockerfile COPY

* `scripts/verify_scheduler.py`
* `scripts/verify_types.py`

---

## Tests

### `tests/test_verify_scheduler.py`

* Small candidate set (5 classes) -> single batch, targeted mode
* Over-budget candidate set (69 classes) -> batched mode, 3 batches
* All batches timeout -> ultra-small-subset recovery
* Early stop after 2 consecutive timeouts
* No-signal -> retry with ultra-small subset

### Smoke test

* django__django-10097: 69 classes, expect batched targeted, signal returned
* Small instance: behavior unchanged, timeout ~60s

---

## Acceptance Criteria

1. 69 classes no longer triggers module fallback
2. Timeout returns partial signal (not all-or-nothing)
3. `no signal` triggers secondary ultra-small-subset attempt
4. In-loop and final verify both use scheduler
5. Events show scope/batch/timeout decisions
6. Small instances unchanged (regression check)
