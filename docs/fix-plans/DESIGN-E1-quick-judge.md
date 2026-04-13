# DESIGN-E1: In-Loop Quick Judge — Two-Tier Verification Architecture

Date: 2026-04-12
Origin: smoke-f2p-fix root cause analysis (verify_count=0 across all instances)
Status: DESIGN PHASE

---

## Problem Statement

Agent writes patches blindly. No test feedback reaches agent during execution.

Current state:
- `inner-verify` runs F2P tests in background after patch detected — but results **never inject back to agent**
- `controlled_verify` runs at attempt end — but gated by cognition_gate + in_loop_judge prerequisites, often skipped
- Result: verify_count=0 across all 3 unresolved instances, both attempts

Root cause: verification is post-hoc (end-of-attempt), not in-loop (during execution).

## Design Principle

> Allow agent to do mid-execution verification, but as a governed capability within the execution->judge loop, not as a free bash escape.

Two tiers, never mixed:

| Tier | Name | When | Who triggers | Result goes to | Trust level |
|------|------|------|-------------|----------------|-------------|
| T1 | Quick Judge | Mid-execution, after patch detected | Harness (automatic) | Agent message stream + telemetry | `trust=50` (targeted, partial) |
| T2 | Controlled Verify | End-of-attempt | Harness (automatic) | Retry controller + telemetry | `trust=100` (full F2P/P2P oracle) |

T1 does NOT replace T2. T1 gives agent directional signal mid-loop. T2 gives system ground truth for routing.

## Architecture

### Flow

```
EXECUTE phase:
  agent writes code
    -> harness detects patch (git diff, existing logic)
    -> [NEW] quick_judge fires:
        1. Run targeted F2P subset (max 5 tests, 30s timeout)
        2. Structure result as QuickJudgeResult
        3. Inject as system message into agent's message stream
        4. Record in telemetry (verify_history with tier=quick)
    -> agent sees result, continues editing
    -> agent writes more code...
    -> [EXISTING] inner-verify fires again if patch changed
    ...
  attempt ends
    -> [EXISTING] in_loop_judge (static checks)
    -> [EXISTING] cognition_gate
    -> [EXISTING] controlled_verify (full F2P/P2P oracle, tier=controlled)
```

### Quick Judge Contract

```python
@dataclass
class QuickJudgeResult:
    """Structured result of a mid-execution targeted test run."""
    tier: Literal["quick"] = "quick"
    step: int                          # agent step number when triggered
    tests_targeted: int                # how many tests were selected
    tests_passed: int
    tests_failed: int
    tests_error: int                   # import error, timeout, etc.
    failing_test_names: list[str]      # max 5 names
    stdout_excerpt: str                # max 500 chars of test output
    elapsed_ms: float

    @property
    def all_passed(self) -> bool:
        return self.tests_failed == 0 and self.tests_error == 0

    @property
    def has_signal(self) -> bool:
        """True if the test run produced actionable information."""
        return self.tests_targeted > 0 and self.tests_error < self.tests_targeted
```

### Test Selection Strategy (T1)

Quick judge does NOT run all F2P tests. It selects a **targeted subset**:

1. Parse F2P test list from instance
2. Select up to 5 tests, prioritizing:
   - Tests whose name matches changed files (heuristic: `test_<changed_module>`)
   - Shortest test names first (proxy for simplest/fastest)
3. Run with 30s hard timeout
4. If F2P has <= 5 tests, run all of them

This is intentionally cheap — the goal is directional signal, not exhaustive verification.

### Agent Message Injection Format

Quick judge result is injected as a system message after the agent's last assistant message:

```
[QUICK_CHECK step={step}] {passed}/{targeted} targeted tests passed ({elapsed_ms}ms)
{if failing_test_names:}
  Failing: {', '.join(failing_test_names)}
  Output excerpt: {stdout_excerpt[:500]}
{endif}
{if all_passed:}
  Direction looks correct. Continue or request full verification.
{else:}
  {failed} tests still failing. Review the output and adjust your patch.
{endif}
```

This is a **system message** (role=system or role=user with [SYSTEM] prefix depending on API), not an assistant message. The agent cannot fake it.

### Phase Routing from Quick Judge

Quick judge result feeds into the existing control plane, but with lower authority than controlled_verify:

| Quick Judge Result | Action | Phase Transition |
|-------------------|--------|-----------------|
| all_passed | Continue execution (no transition) | Stay in EXECUTE |
| partial_pass (some fail) | Inject feedback, continue execution | Stay in EXECUTE |
| all_failed + tests_error=0 | Inject feedback with failing test names | Stay in EXECUTE (agent adjusts) |
| all_error (no test could run) | Flag as environment issue | REDIRECT to OBSERVE (env problem) |

Key difference from controlled_verify routing: quick judge **never terminates the attempt**. It only provides signal. Only controlled_verify (T2) can produce SUCCESS or HARD_FAILURE outcomes.

### Telemetry

Every quick judge invocation is recorded:

```python
# In StepMonitorState
state.quick_judge_history.append({
    "step": step,
    "tier": "quick",
    "tests_targeted": result.tests_targeted,
    "tests_passed": result.tests_passed,
    "tests_failed": result.tests_failed,
    "tests_error": result.tests_error,
    "failing_test_names": result.failing_test_names,
    "elapsed_ms": result.elapsed_ms,
    "invoked": True,          # capability was invoked
    "effective": result.has_signal,  # invocation produced actionable signal
})
```

End-of-attempt jingu_body includes:

```python
jingu_body["quick_judge_history"] = state.quick_judge_history
jingu_body["quick_judge_invoked"] = len(state.quick_judge_history)
jingu_body["quick_judge_effective"] = sum(
    1 for qj in state.quick_judge_history if qj["effective"]
)
```

### Invoked / Effective Metrics (mandatory)

Every capability that claims to improve agent behavior must track:

| Metric | Definition |
|--------|-----------|
| `invoked` | Capability code path was entered |
| `effective` | Capability produced a change in agent behavior (agent message injected AND agent's next action differs from pre-injection trajectory) |

For quick judge specifically:
- `invoked` = quick_judge fired at least once during attempt
- `effective` = quick_judge result was injected AND agent modified patch after seeing result (heuristic: git diff changed between step N and step N+2)

## Integration Points

### Files to Modify

| File | Change |
|------|--------|
| `scripts/step_sections.py` | Refactor `_step_verify_if_needed` → split into quick_judge (inject to agent) + background verify (existing) |
| `scripts/step_monitor_state.py` | Add `quick_judge_history: list[dict]`, `record_quick_judge()` |
| `scripts/jingu_agent.py` | Inject quick_judge result as system message after `_step_verify_if_needed` returns |
| `scripts/controlled_verify.py` | Extract `run_targeted_subset()` helper (reusable by quick judge with smaller scope) |
| `scripts/jingu_agent.py` (~line 1150) | Add `quick_judge_history` to jingu_body |

### New Files

| File | Purpose |
|------|---------|
| `scripts/quick_judge.py` | QuickJudgeResult dataclass + `run_quick_judge()` + test selection + message formatting |
| `tests/test_quick_judge.py` | Unit tests for test selection, result formatting, message injection format |

### Files NOT Modified

| File | Why |
|------|-----|
| `scripts/in_loop_judge.py` | Stays as static patch checks (format, weakening). Quick judge is a separate concern (dynamic test execution). |
| `scripts/control/phase_result.py` | Quick judge doesn't produce PhaseResult — only controlled_verify does. Quick judge is advisory, not authoritative. |
| `scripts/retry_controller.py` | Retry decisions still based on controlled_verify. Quick judge data is available for future enrichment. |

## Constraints

1. **Quick judge timeout = 30s hard cap.** If tests don't finish in 30s, result is `tests_error=targeted` (treated as no signal, not failure).
2. **Max 3 quick judge invocations per attempt.** Prevents spending all step budget on verification instead of coding.
3. **Debounce = 10s minimum between invocations.** Prevents rapid-fire on small edits.
4. **Quick judge fires only in EXECUTE phase.** Not in OBSERVE, ANALYZE, or DECIDE.
5. **Quick judge never blocks agent.** Runs in background thread, injects result when ready. Agent continues working. If agent finishes before quick judge completes, result is recorded but not injected.

## Relationship to Existing Systems

| System | Relationship |
|--------|-------------|
| `inner-verify` (step_sections.py) | **Replaced by quick judge.** inner-verify runs tests but never injects results. Quick judge = inner-verify + injection + telemetry. |
| `controlled_verify` | **Untouched.** Remains the T2 oracle. Quick judge is T1 advisory signal. |
| `in_loop_judge` | **Complementary.** in_loop_judge = static patch checks (no test execution). Quick judge = dynamic test execution. Both run, different signals. |
| Phase routing | **Quick judge is advisory only.** It injects information but doesn't trigger phase transitions. Only controlled_verify drives PhaseResult routing. |

## Principles Alignment

| Principle | How this design satisfies it |
|-----------|----------------------------|
| Action-Observation Closure | Every patch action is followed by a test observation (quick judge). Agent sees result before next action. |
| EFR (Execution Feedback Required) | Quick judge provides execution feedback mid-loop, not just at attempt end. |
| P7 (Probe Until Signal) | Quick judge IS signal. If quick judge shows 0/5 pass, agent has concrete signal to change direction. |
| SST | Test selection logic lives in one place (quick_judge.py), not duplicated. controlled_verify.py provides the test runner, quick_judge.py provides the selection + injection. |
| Structure Over Surface | Quick judge result is structured (QuickJudgeResult dataclass), not raw pytest output. Agent receives formatted summary, not terminal dump. |

## Implementation Sequence

1. Create `scripts/quick_judge.py` — QuickJudgeResult + run_quick_judge() + format_agent_message()
2. Add `quick_judge_history` to StepMonitorState
3. Refactor `_step_verify_if_needed` in step_sections.py — replace inner-verify with quick_judge call
4. Add message injection in jingu_agent.py post-step hook
5. Add telemetry fields to jingu_body
6. Tests
7. Smoke test 1 instance — verify quick_judge fires, result appears in agent messages, agent reacts

Steps 1-2 are independent. Step 3 depends on 1. Step 4 depends on 1+3. Step 5 depends on 2. Step 6 depends on all.

## Open Questions

1. **Should quick judge result influence no_progress_steps counting?** Currently a step with no new file write counts as "no progress". If agent reads quick judge feedback and adjusts strategy (but hasn't written yet), should that count as progress? Tentative: yes, receiving quick judge feedback resets no_progress counter.

2. **Should quick judge be opt-in per phase?** Currently hardcoded to EXECUTE only. Future: could enable in JUDGE phase too for "verify before submit" pattern. Defer to post-implementation data.

3. **Should agent be able to REQUEST a quick judge?** Current design: harness auto-triggers on patch detection. Alternative: agent could have a "verify" tool. Recommendation: start with auto-trigger only. Agent-initiated verify is a future extension if data shows auto-trigger timing is suboptimal.
