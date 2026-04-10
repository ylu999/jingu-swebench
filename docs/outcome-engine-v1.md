# Outcome Engine v1 — Design Doc

## Problem

BUG-10 fix gives us `f2p_passed/f2p_failed/p2p_passed/p2p_failed/eval_resolved` in
controlled_verify. But the retry controller doesn't use these signals — it still relies
on `tests_passed/tests_failed` delta which doesn't distinguish F2P from P2P.

Result: retry routing is blind to whether the agent is making progress on the actual
failing tests vs just not regressing.

## Solution

Add **outcome classification** based on F2P/P2P decomposition, with outcome-specific
retry instructions that tell the agent exactly what kind of failure it hit.

## Outcome Taxonomy

| Outcome | Condition | Meaning |
|---------|-----------|---------|
| `resolved` | f2p_rate == 1.0 AND p2p_rate == 1.0 | All tests pass |
| `partial_fix` | f2p_passed > 0 AND f2p_passed < total_f2p AND p2p_failed == 0 | Some F2P fixed, no regression |
| `wrong_direction` | f2p_passed == 0 AND total_f2p > 0 | No F2P tests fixed |
| `regression` | p2p_failed > 0 | Broke existing tests |
| `no_signal` | total_f2p == 0 OR cv unavailable | Can't classify |

## Routing

| Outcome | Action | Phase hint |
|---------|--------|-----------|
| `resolved` | STOP_OK | — |
| `partial_fix` | CONTINUE | Refine existing patch |
| `wrong_direction` | ADJUST | Rethink root cause |
| `regression` | STOP_FAIL | Revert approach |
| `no_signal` | fall through to existing classify_failure_v2 | — |

## Integration Points

1. **retry_controller.py** — add `classify_outcome()` + `_OUTCOME_INTERVENTIONS` + wire into `build_retry_plan()`
2. **run_with_jingu_gate.py** — pass cv f2p/p2p fields to `build_retry_plan()` call
3. **classify_failure_v2** — use `eval_resolved` as primary signal for `verified_pass`

## Implementation

### T1: Add outcome classifier to retry_controller.py

New function `classify_outcome(cv: dict) -> str` that reads f2p/p2p from controlled_verify.
New `_OUTCOME_INTERVENTIONS` dict mapping outcome -> must_do/must_not_do/hint_prefix.
Modify `build_retry_plan()` to accept `controlled_verify: dict` param, call classify_outcome,
and use outcome interventions when available (override failure_type interventions).

### T2: Wire cv into build_retry_plan call site

In run_with_jingu_gate.py ~line 3906, pass `controlled_verify=jingu_body.get("controlled_verify", {})`
to `build_retry_plan()`.

### T3: Use eval_resolved in classify_failure_v2

Replace `cv_failed == 0` check with `cv.get("eval_resolved", False)` as the primary
verified_pass signal. This aligns with SWE-bench official eval.

## Non-goals

- No multi-attempt trajectory tracking (v2)
- No principal attribution per outcome (v2)
- No strategy table update for outcomes (v2)
