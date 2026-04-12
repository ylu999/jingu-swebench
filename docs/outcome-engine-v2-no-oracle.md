# Outcome Engine v2 — No Oracle Design

## Problem

v1 outcome classification uses controlled_verify results which apply `test_patch`
(agent-invisible oracle information). This makes the retry routing dependent on
signals the agent cannot obtain in a real scenario.

## Signal Audit

| Signal | Agent-visible? | Used in v1? | Used in v2? |
|--------|---------------|-------------|-------------|
| tests_passed/failed (no test_patch) | YES | no | YES |
| tests_delta | YES | no | YES |
| patch exists (git diff non-empty) | YES | no | YES |
| patch changed vs previous attempt | YES | no | YES |
| exit_code | YES | yes | YES |
| f2p_passed/f2p_failed | NO (needs test_patch) | yes | NO |
| p2p_passed/p2p_failed | NO (needs test_patch) | yes | NO |
| eval_resolved | NO (needs f2p+p2p) | yes | NO |

## Design

### Split run_controlled_verify into two modes

Add `apply_test_patch: bool = True` parameter to `run_controlled_verify()`.

- **inner-verify (mid-loop)**: `apply_test_patch=False` — agent-visible signal
- **final-verify (end of attempt)**: `apply_test_patch=True` — eval metric only

### Outcome classification v2 (agent-visible only)

Uses only signals available without test_patch:

```python
def classify_outcome_v2(
    tests_passed: int,       # from inner-verify (no test_patch)
    tests_failed: int,
    prev_tests_passed: int,  # from previous inner-verify
    prev_tests_failed: int,
    patch_exists: bool,
    patch_changed: bool,     # vs previous attempt
) -> str:
    # No patch produced
    if not patch_exists:
        return "no_patch"

    # Tests got worse (regression signal)
    if prev_tests_passed >= 0 and tests_passed >= 0:
        if tests_passed < prev_tests_passed:
            return "regression"

    # Tests improved (positive delta)
    if prev_tests_passed >= 0 and tests_passed >= 0:
        if tests_passed > prev_tests_passed:
            return "positive_delta"

    # Tests unchanged, same patch
    if not patch_changed and tests_passed == prev_tests_passed:
        return "stuck"

    # Tests unchanged, different patch
    if patch_changed and tests_passed == prev_tests_passed:
        return "no_effect"

    # All tests pass
    if tests_failed == 0 and tests_passed > 0:
        return "all_pass"

    return "unknown"
```

### controlled_verify role changes

- **Before (v1)**: participates in retry routing via f2p/p2p
- **After (v2)**: final evaluation metric only, logged but not fed back to agent

### Two-column logging

Every attempt logs both:
```
[outcome-agent] outcome=no_effect  passed=26  failed=3  delta=0   # agent-visible
[outcome-eval]  outcome=partial_fix  f2p=3/26  p2p=548/0          # oracle (eval only)
```

## Implementation Steps

1. Add `apply_test_patch` param to `run_controlled_verify()`
2. Inner-verify calls with `apply_test_patch=False`
3. Final-verify calls with `apply_test_patch=True` (eval metric)
4. Outcome engine uses inner-verify results (no test_patch)
5. Final controlled_verify logged but NOT fed to retry_controller
6. Log both agent-visible and oracle outcomes for comparison

## Non-goals

- Not changing the BUG-10 fix (test_patch still applied in final eval)
- Not removing f2p/p2p fields from traj (useful for analysis)
- eval_resolved still computed and stored (just not used for routing)
