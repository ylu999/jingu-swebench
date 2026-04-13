# DESIGN-E1: In-Loop Quick Judge — Two-Tier Verification Architecture

Date: 2026-04-12
Origin: smoke-f2p-fix root cause analysis (verify_count=0 across all instances)
Status: DESIGN PHASE (v2 — user-reviewed, constraints hardened)

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

Three-way boundary (must never blur):

| Role | Responsibility | Cannot do |
|------|---------------|-----------|
| T1 Quick Judge | Mid-execution directional signal | Cannot terminate attempt, cannot route phases |
| T2 Controlled Verify | Attempt-level formal verdict | Cannot fire mid-execution |
| Agent | Signal consumer | Cannot select tests, cannot trigger verify, cannot schedule verify |

Two tiers, never mixed:

| Tier | Name | When | Who triggers | Result goes to | Trust level |
|------|------|------|-------------|----------------|-------------|
| T1 | Quick Judge | Mid-execution, after patch detected | Harness (automatic only) | Agent message stream + telemetry | `trust=50` (targeted, partial) |
| T2 | Controlled Verify | End-of-attempt | Harness (automatic) | Retry controller + telemetry | `trust=100` (full F2P/P2P oracle) |

T1 does NOT replace T2. T1 gives agent directional signal mid-loop. T2 gives system ground truth for routing.

## Rollout Phases

| Phase | What | When |
|-------|------|------|
| Phase 1 | Automatic quick judge only. Advisory only. No routing authority. No agent test selection. No agent-initiated trigger. | Now |
| Phase 2 | Observe invoked/acknowledged/effective metrics. Analyze which cases auto-trigger is insufficient. | After 50+ instance data |
| Phase 3 | Only if evidence shows value: introduce bounded agent request (request is not execution; it is a governed proposal). | Data-driven decision |

---

## Architecture

### Flow

```
EXECUTE phase:
  agent writes code
    -> harness detects patch (git diff, existing logic)
    -> trigger conditions checked (see Trigger Contract below)
    -> [NEW] quick_judge fires:
        1. Select targeted F2P subset (stable selection, see below)
        2. Run tests (30s hard timeout)
        3. Classify direction signal (improved/regressed/unchanged/inconclusive)
        4. Format minimal structured message
        5. Inject as system message into agent's message stream
        6. Record in telemetry with full attribution fields
    -> agent sees result, continues editing
    ...
  attempt ends
    -> [EXISTING] in_loop_judge (static checks)
    -> [EXISTING] cognition_gate
    -> [EXISTING] controlled_verify (full F2P/P2P oracle, tier=controlled)
```

### Trigger Contract (Hard Constraint 1: Strict Debounce)

Quick judge fires ONLY when ALL conditions are met:

```python
def should_trigger_quick_judge(state: StepMonitorState) -> bool:
    # C1: Must be in EXECUTE phase
    if state.cp_state.phase != "EXECUTE":
        return False
    # C2: Patch must have substantively changed since last quick judge
    if state.last_quick_judge_patch == current_patch:
        return False
    # C3: Minimum step interval (at least 3 agent steps since last quick judge)
    if (state._llm_step - state.last_quick_judge_step) < 3:
        return False
    # C4: Minimum time interval (at least 15s since last quick judge)
    if (time.monotonic() - state.last_quick_judge_time) < 15.0:
        return False
    # C5: Attempt not in terminal path
    if state.early_stop_verdict is not None:
        return False
    # C6: Quota not exhausted (max 3 per attempt)
    if state.quick_judge_count >= 3:
        return False
    return True
```

### Trigger Source Protocol (Future-Proof)

```python
QuickJudgeTriggerSource = Literal[
    "automatic_patch_detected",    # Phase 1: only this enabled
    "automatic_phase_boundary",    # Phase 1: reserved, not enabled
    "agent_requested",             # Phase 3: reserved, not enabled
]
```

Schema reserves the field now. Implementation only enables `automatic_patch_detected` in Phase 1.

### Quick Judge Contract

```python
@dataclass
class QuickJudgeResult:
    """Structured result of a mid-execution targeted test run."""
    tier: Literal["quick"] = "quick"
    trigger_source: QuickJudgeTriggerSource = "automatic_patch_detected"
    step: int                          # agent step number when triggered
    tests_targeted: int                # how many tests were selected
    tests_passed: int
    tests_failed: int
    tests_error: int                   # import error, timeout, etc.
    failing_test_names: list[str]      # max 3 names (Hard Constraint 2: minimal)
    elapsed_ms: float
    # Direction signal (Hard Constraint 4: failure attribution)
    direction: QuickJudgeDirection     # see below

    @property
    def all_passed(self) -> bool:
        return self.tests_failed == 0 and self.tests_error == 0

    @property
    def has_signal(self) -> bool:
        return self.tests_targeted > 0 and self.tests_error < self.tests_targeted
```

### Direction Signal (Failure Attribution — Hard Constraint 4)

Quick judge doesn't just report pass/fail. It classifies the DIRECTION of change relative to the previous quick judge result:

```python
QuickJudgeDirection = Literal[
    "improved",                    # more tests pass than last time
    "regressed",                   # fewer tests pass than last time
    "unchanged",                   # same pass/fail counts
    "inconclusive",                # tests errored, can't determine direction
    "first_signal",                # first quick judge this attempt, no baseline
    "likely_right_direction",      # improved + failing tests are a subset of previous
    "likely_wrong_direction",      # regressed OR new failures appeared
]
```

Direction is computed by comparing with previous quick judge result:

```python
def classify_direction(current: QuickJudgeResult, previous: QuickJudgeResult | None) -> QuickJudgeDirection:
    if previous is None:
        return "first_signal"
    if current.tests_error >= current.tests_targeted:
        return "inconclusive"
    if current.tests_passed > previous.tests_passed:
        # Check if failures are a strict subset
        if set(current.failing_test_names).issubset(set(previous.failing_test_names)):
            return "likely_right_direction"
        return "improved"
    if current.tests_passed < previous.tests_passed:
        return "likely_wrong_direction"
    if current.tests_passed == previous.tests_passed:
        if current.failing_test_names != previous.failing_test_names:
            return "unchanged"  # same count but different tests — no clear direction
        return "unchanged"
    return "inconclusive"
```

### Test Selection Strategy (Hard Constraint 4: Stable, Explainable)

Quick judge does NOT run all F2P tests. It selects a **stable targeted subset**:

1. Parse F2P test list from instance (via `_parse_fail_to_pass()`)
2. If F2P has <= 5 tests: run all of them (most common case)
3. If F2P has > 5 tests, select up to 5 with fixed priority:
   a. Tests whose name matches changed files (heuristic: `test_<changed_module>`)
   b. Shortest test names first (proxy for simplest/fastest)
4. **Stability rule**: once selected for this attempt, the subset is locked. Only re-select if patch file scope significantly expands (new file added to diff that wasn't there before).
5. Run with 30s hard timeout

Selection is recorded in telemetry so it's auditable.

### Agent Message Injection Format (Hard Constraint 2: Minimal Structured Summary)

Quick judge result is injected as a system message. **Extremely short. No raw test output.**

```
[QUICK_CHECK step={step}] {direction} — {passed}/{targeted} tests passed
{if failing_test_names:}
Failing: {', '.join(failing_test_names[:3])}
{endif}
{hint}
```

Where `hint` is ONE sentence derived from direction:

| Direction | Hint |
|-----------|------|
| `first_signal` | "First test signal. {passed}/{targeted} passing." |
| `improved` | "Progress: more tests passing than before." |
| `likely_right_direction` | "Good direction: failures narrowing." |
| `regressed` | "Regression: fewer tests passing. Review your last change." |
| `likely_wrong_direction` | "Wrong direction: new failures appeared. Reconsider approach." |
| `unchanged` | "No change in test results. Try a different approach." |
| `inconclusive` | "Tests could not run. Check for import/syntax errors." |

**No stdout_excerpt.** No raw test output. Agent gets structured signal only.

### Phase Routing from Quick Judge

Quick judge **never terminates the attempt** and **never triggers phase transitions**:

| Quick Judge Result | Action |
|-------------------|--------|
| Any direction | Inject message, continue EXECUTE. No phase change. |
| `inconclusive` (all tests errored) | Inject message + log env_issue flag. No phase change. |

Only controlled_verify (T2) can produce SUCCESS or HARD_FAILURE outcomes.

---

## Effectiveness Measurement (Hard Constraint 3: Three-Layer Metrics)

### Three layers of measurement

| Layer | Metric | Definition | How measured |
|-------|--------|-----------|-------------|
| L1 | `invoked` | Quick judge code path was entered | `len(quick_judge_history) > 0` |
| L2 | `acknowledged` | Agent's next action references or responds to the signal | Post-injection text contains test name from result OR patch changes target the failing area |
| L3 | `effective` | Patch converges toward correct direction after injection | `direction` improves across consecutive quick judges (e.g., `regressed` -> `improved` -> `likely_right_direction`) |

### L2 acknowledged detection (heuristic)

```python
def detect_acknowledged(
    qj_result: QuickJudgeResult,
    post_injection_assistant_text: str,
    post_injection_patch_files: list[str],
) -> bool:
    # Check 1: agent text mentions a failing test name
    for test_name in qj_result.failing_test_names:
        short_name = test_name.split("::")[-1]  # last part of test path
        if short_name in post_injection_assistant_text:
            return True
    # Check 2: agent modifies a file related to failing tests
    # (heuristic: file name overlaps with test module name)
    return False  # conservative default
```

### L3 effective detection

```python
def detect_effective(quick_judge_history: list[dict]) -> bool:
    if len(quick_judge_history) < 2:
        return False
    directions = [qj["direction"] for qj in quick_judge_history]
    # Effective if direction sequence shows convergence
    GOOD = {"improved", "likely_right_direction"}
    BAD = {"regressed", "likely_wrong_direction"}
    # At least one transition from BAD/unchanged/first_signal to GOOD
    for i in range(1, len(directions)):
        if directions[i] in GOOD and directions[i-1] not in GOOD:
            return True
    return False
```

---

## Telemetry

Every quick judge invocation is recorded:

```python
state.quick_judge_history.append({
    "step": step,
    "tier": "quick",
    "trigger_source": "automatic_patch_detected",
    "tests_targeted": result.tests_targeted,
    "tests_passed": result.tests_passed,
    "tests_failed": result.tests_failed,
    "tests_error": result.tests_error,
    "failing_test_names": result.failing_test_names,
    "elapsed_ms": result.elapsed_ms,
    "direction": result.direction,
    "selected_tests": selected_test_ids,  # which tests were chosen (auditable)
    "patch_hash": hash(current_patch),    # which patch version triggered this
    "invoked": True,
    "acknowledged": None,   # filled post-hoc after next agent step
    "effective": None,      # filled at attempt end
})
```

End-of-attempt jingu_body:

```python
jingu_body["quick_judge_history"] = state.quick_judge_history
jingu_body["quick_judge_invoked"] = len(state.quick_judge_history)
jingu_body["quick_judge_acknowledged"] = sum(
    1 for qj in state.quick_judge_history if qj.get("acknowledged")
)
jingu_body["quick_judge_effective"] = detect_effective(state.quick_judge_history)
jingu_body["quick_judge_directions"] = [
    qj["direction"] for qj in state.quick_judge_history
]
```

---

## Integration Points

### Files to Modify

| File | Change |
|------|--------|
| `scripts/step_sections.py` | Replace `_step_verify_if_needed` inner-verify logic with quick_judge call + message injection |
| `scripts/step_monitor_state.py` | Add `quick_judge_history`, `quick_judge_count`, `last_quick_judge_*` fields, `record_quick_judge()` |
| `scripts/jingu_agent.py` | Wire quick_judge message injection into post-step hook; add telemetry to jingu_body (~line 1150) |

### New Files

| File | Purpose |
|------|---------|
| `scripts/quick_judge.py` | QuickJudgeResult + run_quick_judge() + select_targeted_tests() + classify_direction() + format_agent_message() |
| `tests/test_quick_judge.py` | Unit tests: test selection stability, direction classification, message formatting, trigger conditions, effectiveness detection |

### Files NOT Modified

| File | Why |
|------|-----|
| `scripts/in_loop_judge.py` | Stays as static patch checks. Separate concern. |
| `scripts/control/phase_result.py` | Quick judge doesn't produce PhaseResult. Advisory only. |
| `scripts/retry_controller.py` | Retry decisions based on controlled_verify only. Quick judge data available for future. |
| `scripts/controlled_verify.py` | T2 untouched. Quick judge reuses `run_controlled_verify()` with scope override. |

---

## Constraints Summary

| # | Constraint | Rationale |
|---|-----------|-----------|
| C1 | Strict debounce: patch must change + 3 step minimum + 15s minimum | Prevent execution phase drowned in test noise |
| C2 | Agent message = minimal structured summary, no raw stdout, max 3 failing test names, one-sentence hint | Prevent message stream pollution |
| C3 | Effectiveness = three layers (invoked / acknowledged / effective), not just "patch changed" | Distinguish "seen" from "helped" |
| C4 | Test subset selection must be stable within attempt + explainable + recorded | Enable comparable directional signals |
| C5 | Max 3 invocations per attempt | Prevent step budget consumed by verification |
| C6 | 30s hard timeout per invocation | Prevent slow tests blocking execution |
| C7 | EXECUTE phase only | Quick judge has no meaning in OBSERVE/ANALYZE/DECIDE |
| C8 | Never terminates attempt, never triggers phase transition | T1 is advisory; T2 is authoritative |
| C9 | Agent cannot select tests, cannot trigger verify, cannot schedule verify | Agent is signal consumer, not verification scheduler |
| C10 | trigger_source field reserved for future agent_requested | Protocol future-proof without current implementation |

---

## Implementation Sequence

### Task E1-A: `quick_judge.py` core module (independent)
- QuickJudgeResult dataclass
- select_targeted_tests() with stability rule
- run_quick_judge() using run_controlled_verify with scope override
- classify_direction() comparing consecutive results
- format_agent_message() producing minimal structured text
- detect_acknowledged() and detect_effective() metrics

### Task E1-B: StepMonitorState extension (independent)
- Add quick_judge_history, quick_judge_count, last_quick_judge_* fields
- Add record_quick_judge() method
- Add should_trigger_quick_judge() method (trigger contract)
- Update to_checkpoint_dict() / from_checkpoint_dict()

### Task E1-C: Step sections integration (depends on E1-A + E1-B)
- Replace inner-verify in _step_verify_if_needed with quick_judge call
- Wire trigger conditions via should_trigger_quick_judge()
- Inject quick judge result as system message into agent messages
- Post-hoc acknowledged detection after next agent step

### Task E1-D: jingu_agent.py telemetry wiring (depends on E1-B)
- Add quick_judge_history to jingu_body at attempt end
- Add summary metrics (invoked, acknowledged, effective, directions)
- Wire detect_effective() at attempt boundary

### Task E1-E: Tests (depends on E1-A + E1-B)
- test_select_targeted_tests: stability, priority order, <= 5 cap
- test_classify_direction: all 7 direction types
- test_format_agent_message: format correctness, length bounds
- test_should_trigger_quick_judge: all 6 trigger conditions
- test_detect_acknowledged: text match, conservative default
- test_detect_effective: convergence detection

E1-A and E1-B are independent (parallel). E1-C depends on both. E1-D depends on E1-B. E1-E depends on E1-A + E1-B.
