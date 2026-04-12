# PLAN A: structured_extract() → Phase Transition Admission Gate

**Status**: PLAN COMPLETE — ready for design/implementation
**Priority**: HIGH (max leverage, minimal change)
**Depends on**: nothing
**Blocks**: Plan B (main loop structuring)

---

## Problem Statement

Phase advance happens BEFORE extraction and validation. Extraction failure = unvalidated phase advance.

Current flow at `step_sections.py:414-558`:

```
VerdictAdvance received
  1. cp_state.phase = new_phase          ← PHASE ALREADY ADVANCED (line 424)
  2. Extract PhaseRecord for OLD phase   ← extraction happens AFTER advance
  3. Cognition validation                ← if extraction failed, this is SKIPPED
  4. Analysis/Design gate                ← SKIPPED
  5. Principal gate                      ← SKIPPED
  → extraction failure = silent pass-through
```

When `_pr is None` (extraction failed):
- Cognition validation: skipped (`if _pr is not None:` at line 562)
- Analysis gate: skipped (`if _eval_phase == "ANALYZE" and _pr is not None`)
- Principal gate: skipped (`if _pr is None: raise RuntimeError(...)`)
- **Phase advance STANDS** — no rollback

## Proposed Flow

```
VerdictAdvance received
  1. DO NOT advance phase yet
  2. Extract PhaseRecord for current phase
     ├── structured_extract (LLM call)
     ├── Fallback: regex extraction
     └── BOTH fail → EXTRACTION_FAILED gate
           ├── retry_count < 2 → inject feedback, stay in phase
           └── retry_count >= 2 → FORCE_ADVANCE (emit limit_triggered)
  3. Validate (existing gates, unchanged):
     ├── Cognition validation
     ├── Analysis gate (ANALYZE only)
     ├── Design gate (DESIGN only)
     └── Principal gate
  4. ALL PASS → advance cp_state.phase = new_phase
  5. ANY REJECT → stay in current phase (existing feedback injection)
```

**Key change: swap advance and extract+validate. Advance only after all gates pass.**

## Exact Code Changes

### step_sections.py (core)

| Location | Current | Proposed |
|----------|---------|----------|
| Line 414-428 | VerdictAdvance immediately sets `cp_state.phase = new_phase` | Move advance to AFTER all gates pass |
| Line 447-558 | Extraction wrapped in non-fatal try/except | Add extraction failure gate with retry |
| Line 557-558 | `except: print("non-fatal")` | Extraction failure → retry or force-advance |
| ~Line 820-823 | `if _pr is None: raise RuntimeError` | Remove — handled upstream |

### step_monitor_state.py

| Location | Change |
|----------|--------|
| ~Line 120 | Add `extraction_retry_counts: dict[str, int] = {}` |

### jingu_agent.py

| Location | Change |
|----------|--------|
| ~Line 1046 | Reset `extraction_retry_counts = {}` per attempt |
| ~Line 1227 | Add extraction failure count to run report |

## Extraction Failure Handling

2-strike retry, then force advance:

```python
_MAX_EXTRACTION_RETRIES = 2

if _pr is None:
    _retry = state.extraction_retry_counts.get(_eval_phase, 0)
    if _retry < _MAX_EXTRACTION_RETRIES:
        state.extraction_retry_counts[_eval_phase] = _retry + 1
        state._phase_accumulated_text.pop(_eval_phase, None)
        # inject feedback asking for structured output
        # DO NOT advance phase
        return
    else:
        _emit_limit_triggered(...)  # force advance with no record
```

## Existing Retry Patterns (reference)

All three existing gates already use retry-as-rollback:
- **Cognition reject** (line 592-621): rollback + feedback + remove cached record
- **Analysis gate reject** (line 669-723): rollback + feedback, max 2 then FORCE_PASS
- **Principal gate RETRYABLE** (line 919-991): rollback + feedback, max 3 then contract_bypass

With the new flow, "rollback" simplifies to no-op (phase was never advanced).

## Budget Impact

- Extraction retry: max 2 extra LLM calls per phase boundary (structured_extract only)
- Expected failure rate: ~10% of extractions
- Net: ~0.2-0.4 extra LLM calls per attempt average
- Validation gates: zero extra LLM calls (pure Python checks)

## Risk: Gate Too Strict

**Mitigations:**
1. 2-strike limit → force advance (prevents stuck)
2. Regex fallback almost never returns None (constructs PhaseRecord from whatever it finds)
3. Existing `_RETRYABLE_LOOP_LIMIT = 3` on principal gate ensures eventual breakthrough
4. Telemetry counters already track extraction method and failure rate

## Verification Criteria

1. Extraction failure → retry (phase stays, feedback injected)
2. 2 extraction failures → force advance (limit_triggered event)
3. Successful extraction + gates pass → advance
4. Successful extraction + gate reject → stay (existing behavior preserved)
5. Smoke test: 1 SWE-bench instance, no infinite loops
6. Regression: existing test suite passes

## Implementation Steps (ordered)

1. Add `extraction_retry_counts` to StepMonitorState
2. Refactor VerdictAdvance handler: move advance to after gates
3. Add extraction failure gate (2-strike retry)
4. Simplify existing gate rollback (phase never advanced = no rollback needed)
5. Initialize counter per attempt in jingu_agent.py
6. Update telemetry in run report
7. Smoke test
