# RCA: Phase Admission Bottleneck

## Date: 2026-04-14
## Trigger: DQv1 smoke test — 3/5 unresolved, all sharing the same first-order failure

---

## Problem Statement

**One-line:** 当前 unresolved 的主因不是 reasoning wrong，而是 cognition-to-admission pipeline broken。

Phase 存在于设计里，但还没有稳定存在于 runtime 里。

DQv1 smoke test (smoke-dqv1-5case, commit ad4fe5f) 5 个 case 中 3 个 unresolved：

| Instance | phase_records_count | Reached DECIDE? | Reached EXECUTE? | Had controlled_verify? |
|----------|-------------------|-----------------|-----------------|----------------------|
| django__django-10097 | 0 | No | Yes (empty) | No |
| django__django-10554 | 0 | No | Yes (empty) | No |
| django__django-10999 | 0 | No | No | No |

三个 case 的共同特征：**phase_records_count=0 贯穿全部 attempt**。
Agent 从未成功产出被系统 admit 的 phase record，因此：
- DQv1 的 testable_hypothesis 从未被激活
- DECIDE phase 从未被到达
- Prediction error feedback 从未被触发
- 整个 governed cognition pipeline 处于 "present but not invoked" 状态

---

## Invariant Violations

### IV-1: Phase transition without admitted record

**Invariant:** No phase transition without an admitted PhaseRecord.

**Violation:** `step_sections.py:613-677` — 当 `_pr is None`（no submission）时 transition 被 block，
但当 analysis_gate reject 时，transition 仍然放行（rejection does NOT block advance）。
这意味着低质量 record 可以通过，而缺少 record 会卡死。

**Evidence:** django__django-10097 — gate 以空 signal 放行 OBSERVE→ANALYZE→EXECUTE，
`phase_records_count=0` 始终为零（records 未被 append 到 state）。

### IV-2: Silent submission failure

**Invariant:** Force-armed submission must either succeed or produce typed failure.

**Violation:** `jingu_model.py:303-307` — `json.loads` parse error 被 `logger.error` 吞掉，
`_submitted_phase_record` 保持 None，downstream 无法区分"agent 没调用 submit"和"agent 调用了但 JSON 无效"。

**Evidence:** 三个 case 的 force 机制多次 armed，但 phase_records_count=0 说明 submission 从未被系统接收。

### IV-3: Gate rejection without structured repair route

**Invariant:** Gate rejection must route to phase-specific repair, not generic retry.

**Violation:** `step_sections.py:1014+` — analysis_gate reject 后，feedback 注入 agent messages，
force 重新 armed，但 agent 被打回同一 phase 做 generic retry。
django__django-10999 的 `invariant_capture=0.0` rejection 导致 redirect_observe，
agent 在 OBSERVE 循环到 step 103。

### IV-4: DECIDE phase exists in graph but bypassed at runtime

**Invariant:** Every phase in the advance table must be reachable.

**Violation:** `control/reasoning_state.py:191-198` — `_ADVANCE_TABLE` 定义 ANALYZE→DECIDE→EXECUTE，
但实际 control flow (`decide_next()` line 267) 在 `actionability > 0` 时直接 ANALYZE→EXECUTE，
跳过 DECIDE。DQv1 的 testable_hypothesis 字段永远不会被触发。

---

## Required Runtime Changes

### RC-1: Admitted record as sole phase transition gate

**Current:** `_pr is None` blocks transition; `_pr exists but gate rejects` does NOT block.
**Required:** Phase transition = `_pr is not None AND gate.passed`. Both conditions mandatory.

```
submit_phase_record called?
  No  → BLOCKED (retry, max 2)
  Yes → parse OK?
    No  → BLOCKED (typed error: SUBMISSION_PARSE_FAILURE)
    Yes → gate.passed?
      No  → BLOCKED (typed error: GATE_REJECTION with repair_route)
      Yes → ADVANCE
```

**Files:** `step_sections.py:828-900`, `analysis_gate.py`

### RC-2: Typed submission failure (no silent drops)

**Current:** Parse error → `logger.error`, continue. Submission silently lost.
**Required:** Parse error → store typed failure in `_submission_failure_reason`, expose to downstream.

```python
# jingu_model.py _parse_actions
except json.JSONDecodeError as e:
    self._submission_failure = {"type": "parse_error", "detail": str(e)}
    logger.error("submit_phase_record parse error: %s", e)
```

**Files:** `jingu_model.py:303-307`

### RC-3: Gate rejection → phase-specific repair route

**Current:** Gate rejects → generic "resubmit" message → agent loops.
**Required:** Gate rejection carries typed repair target:

| Gate failure | Repair route | Repair hint |
|-------------|-------------|-------------|
| `invariant_capture=0.0` | Stay ANALYZE | "Identify the behavioral constraint being violated. Look for: assertion, validation, type check, contract." |
| `alternative_hypothesis=0.0` | Stay ANALYZE | "You must form at least 2 competing hypotheses. What else could cause this behavior?" |
| `code_grounding=0.0` | Redirect OBSERVE | "Your analysis references no specific code. Go back and read the relevant source files." |
| `causal_chain=0.0` | Stay ANALYZE | "Trace the execution path: input → function → failure point. Name each step." |

**Files:** `step_sections.py:988-1300`, `analysis_gate.py`

### RC-4: DECIDE phase activation at runtime

**Current:** `decide_next()` skips DECIDE, goes ANALYZE→EXECUTE directly.
**Required:** ANALYZE→DECIDE→EXECUTE as mandatory path. DECIDE record must contain:
- `chosen_direction` (required)
- `rejected_alternatives` (required, ≥1)
- `testable_hypothesis` (required)
- `evidence_refs` (required)

**Files:** `control/reasoning_state.py`, `step_sections.py` (advance logic)

---

## Acceptance Metrics

4 个 runtime 指标，用于判断 pipeline 是否真正激活：

### M-1: phase_record_emitted_rate

**Definition:** `count(submit_phase_record called) / count(phase transitions attempted)`
**Target:** ≥ 0.9 (90% of phase transitions have a submission attempt)
**Current baseline:** ~0.0 (三个 unresolved case 无一成功 emit)

### M-2: phase_record_admitted_rate

**Definition:** `count(phase_record admitted by gate) / count(submit_phase_record called)`
**Target:** ≥ 0.6 (60% of submissions pass gate on first or second try)
**Current baseline:** N/A (no emissions to measure)

### M-3: phase_transition_with_admitted_record_rate

**Definition:** `count(transitions with admitted record) / count(all phase transitions)`
**Target:** 1.0 (every transition must have an admitted record — this is the invariant)
**Current baseline:** 0.0 for unresolved cases

### M-4: generic_retry_rate

**Definition:** `count(retry without typed repair route) / count(all retries)`
**Target:** ≤ 0.1 (90%+ of retries should have typed repair routes)
**Current baseline:** ~1.0 (all retries are currently generic)

---

## Architectural Alignment

This RCA confirms the separation of concerns:
- **jingu-trust-gate** → "输出能不能被系统接收" (admission)
- **jingu-cognition** → "认知过程如何被结构化声明、约束、验证、归因" (cognition contracts)
- **Runtime (jingu-swebench)** → these contracts 的 embodiment，不应反过来主导定义

当前问题：runtime 的 embodiment 不完整。Contracts 存在于 jingu-cognition 的 taxonomy 和 schema 里，
但 runtime 没有忠实执行它们。Fix 方向是让 runtime 更忠实地 embody contracts，不是在 runtime 里发明新机制。

---

## Failure Classification Summary

| Level | Failure | Instances |
|-------|---------|-----------|
| **一级** | Phase record admission failure (pipeline broken) | All 3 |
| **二级** | Exploration loop without typed handoff | 10097, 10554 |
| **三级** | Gate rejection without repair route (post-hoc, not process-shaping) | 10999 |

**一句话结论：** 不是 patch 错，不是 DQv1 不够聪明，不是 verify 不够强。
是 phase-bound execution pipeline 没被真正激活。
