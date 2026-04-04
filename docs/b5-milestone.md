# B5 Milestone — Semantic Boundary Gating for Stagnation Detection

**Date:** 2026-04-04  
**Status:** Stable / verified

---

## What B5 is

B5 introduces `progress_evaluable_event` (pee) as a semantic gate on stagnation tracking.

**Core insight:** stagnation (`no_progress_steps`) should only advance at moments when the
system *could have made progress but didn't* — not at every arbitrary agent step.

Without B5, `no_progress` incremented every step, so the stagnation counter was heavily
polluted by normal read/think/write activity. A patch edit counted the same as a verify.

With B5, `no_progress` only advances at **semantic boundary events**:
1. `inner_verify_new` — inner verify returned a new result (evaluation boundary)
2. `env_error` — environment failure detected (failure is information)
3. `patch_first_write` — patch written for the first time this attempt (phase transition)

All other steps: `no_progress` frozen.

---

## Key design decisions

### Monotone latch (critical fix, commit 4815c7e)

`_prev_patch_non_empty` must be a **monotone state** (once True, stays True), not an event
toggle. Before the fix, any read step reset it to False, and the next write would re-trigger
`patch_first_write=True` — causing pee to fire on every write after any read.

Root cause: tracking "did last step write" (event) vs "has patch ever been written" (state).

Fix:
```python
# Monotone latch — tracks "has a patch ever been written this attempt"
if patch_non_empty:
    self._prev_patch_non_empty = True
# Never set back to False
```

### pee_reason for observability (commit 4f67c66)

`pee:True` logs now include the triggering reason:
```
pee:True(inner_verify_new)
pee:True(patch_first_write)
pee:True(env_error)
pee:True(inner_verify_new,env_error)  # multiple simultaneous triggers
pee:False
```

This upgrades B5 from "implicit control" to "explainable control" — debug traces now
show exactly which semantic boundary triggered stagnation evaluation.

---

## Verified invariants (smoke test b5-smoke5-20260404)

| Invariant | Verified |
|-----------|---------|
| `pee:True` only on boundary events | ✅ |
| Subsequent patch edits: `pee:False` | ✅ |
| `no_progress` frozen on non-boundary steps | ✅ |
| `pee` reason observable in logs | ✅ |
| `patch_first_write` fires at most once per attempt | ✅ step:6 only; steps 36,41,45,77,80,87,94 all pee:False |
| B3 behavior preserved (task_success at verify boundary) | ✅ |
| 86 unit tests pass | ✅ |

---

## Before vs After

**Before B5:**
```
[cp-step] no_progress:0 step:1  pee:True   ← every write
[cp-step] no_progress:0 step:3  pee:True   ← every write after any read
[cp-step] no_progress:1 step:5  pee:True   ← stagnation counter polluted
```

**After B5 (latch fix):**
```
[cp-step] no_progress:0 step:32  signals=['patch'] pee:True(patch_first_write)   ← first write only
[cp-step] no_progress:0 step:33  signals=['patch'] pee:False                     ← subsequent edits
[cp-step] no_progress:0 step:44  signals=['patch'] pee:False                     ← subsequent edits
[cp-step] no_progress:1 step:224 pee:True(inner_verify_new)                      ← verify boundary
```

---

## Architecture position

B5 is the semantic gating layer between raw agent activity and the stagnation counter.

```
agent step
   ↓
swe_signal_adapter.extract_step_signals()
   → signals: evidence_gain, actionability, env_noise, ...
   → pee: bool (semantic boundary gate)
   → pee_reason: str (observability)
   ↓
update_reasoning_state(update_stagnation=pee)
   → no_progress_steps only advances when pee=True
```

**Next:** Connect B5 pee events to B4 cognitive phase awareness — valid progress = correct phase + boundary event.
