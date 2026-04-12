# P1 — Controlled Verify Fix Plan

**Priority:** CRITICAL (direct cause of NOT RESOLVED)
**Audit items:** C1, C2, C5, C16
**Impact:** Agent gets zero test feedback — writes patch blind, cannot confirm correctness.

---

## Problem

django__django-10097 has FAIL_TO_PASS = 438 tests across 69 classes.

```
69 classes > 20-class limit (C1: controlled_verify.py:89)
  -> falls back to module scope
  -> module scope runs entire auth module
  -> exceeds 60s timeout (C2: controlled_verify.py:343)
  -> TimeoutExpired -> controlled_error
  -> 3x verify all timeout -> agent gets ZERO test feedback
```

Additionally:
- In-loop verify uses `timeout_s=45` (C5: step_sections.py:239) — shorter than final verify's 60s
- Final verify duplicates timeout at `timeout_s=60` (C16: jingu_agent.py:919) — same as C2

## Fix Steps

### Fix 1: Remove arbitrary 20-class limit (C1)

**File:** `controlled_verify.py:88-89`

Current:
```python
if all_labels and len(all_labels) <= 20:
```

Change to: scale timeout with class count instead of hard cutoff.

```python
# No arbitrary class limit — always use targeted scope when labels available.
# Timeout scales with class count (see Fix 2).
if all_labels:
```

**Rationale:** The 20-class limit was a heuristic to avoid slow runs. The correct fix is scaling timeout, not dropping to module scope (which is even slower and less targeted).

### Fix 2: Scale timeout with test scope (C2)

**File:** `controlled_verify.py:339-343`

Current:
```python
def run_controlled_verify(
    ...
    timeout_s: int = 60,
    ...
```

Change to dynamic timeout:
```python
def run_controlled_verify(
    ...
    timeout_s: int | None = None,  # None = auto-scale
    ...
```

Add auto-scaling logic at the start of the function:
```python
if timeout_s is None:
    # Auto-scale: base 60s + 2s per class label beyond 10
    n_classes = len(_extract_f2p_class_labels(instance.get("FAIL_TO_PASS", [])))
    timeout_s = min(60 + max(0, n_classes - 10) * 2, 300)  # cap at 5 min
```

### Fix 3: Unify timeout values (C5, C16)

**File:** `step_sections.py:238-239`

Current:
```python
cv_result = run_controlled_verify(
    patch, state.instance, container, timeout_s=45,
```

Change to:
```python
cv_result = run_controlled_verify(
    patch, state.instance, container, timeout_s=None,  # auto-scale
```

**File:** `jingu_agent.py:919`

Current:
```python
timeout_s=60
```

Change to:
```python
timeout_s=None  # auto-scale
```

### Fix 4: Log when scope/timeout decisions are made

In `controlled_verify.py`, after scope resolution (~line 99):
```python
print(f"    [controlled_verify] scope={_actual} classes={len(all_labels)} "
      f"timeout_s={timeout_s} f2p={len(f2p_labels)} sentinel={len(sentinel_labels)}",
      flush=True)
```

On timeout:
```python
print(f"    [controlled_verify] TIMEOUT: scope={scope_actual} elapsed={timeout_s}s "
      f"classes={len(all_labels)} — agent receives no test feedback",
      flush=True)
```

## Verification

1. django__django-10097 smoke: `controlled_verify` completes (no timeout) with 69 classes
2. Verify output contains test pass/fail counts (not `controlled_error`)
3. Agent receives test feedback and can iterate on patch
4. Log shows scope/timeout decision with class count

## Dependencies

None — independent of P0 (bundle). But P0 + P1 together should significantly improve resolve rate.
