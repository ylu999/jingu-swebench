# Jingu × SWE-bench — Implementation Plan

## Goal

Prove Jingu improves LLM coding performance on an existing industry benchmark.

> Insert Jingu into the SWE-bench inference loop. Compare `raw` vs `jingu` on resolved rate,
> invalid output rate, and retry recovery rate.

No new benchmark. No new metrics definition. Plug into SWE-bench Lite/Verified and let the
existing leaderboard speak.

---

## Why SWE-bench

SWE-bench task: given a GitHub repo + issue → produce a patch → verify with tests.

This maps directly to Jingu's L2 capability:
- Gate = test result (deterministic pass/fail)
- Reviewer = patch quality check
- Retry loop = failure → structured feedback → re-propose

The resolve metric is objective: either tests pass or they don't. No subjective scoring.

Dataset variants:
- **Lite** (534 instances) — for development and iteration
- **Verified** (500 human-validated instances) — for external claims

---

## Architecture

```
SWE-bench instance
  → workspace bootstrap (checkout repo @ base commit)
  → proposer (LLM, with RPP)
  → patch candidate
  → structural gate (non-empty, parseable)
  → apply gate (git apply succeeds)
  → test gate (pytest delta: fail→pass count)
  → if fail: structured retry feedback → proposer again
  → final patch
  → SWE-bench harness evaluate (official resolved %)
```

**What SWE-bench owns:** dataset, test harness, official resolve scoring.
**What Jingu owns:** proposer, gates, retry loop, event log.

---

## Two Runners (core comparison)

### Runner A: raw
```
instance → build prompt → single LLM call → parse patch → write prediction
```
- No retry
- No gates beyond basic parse
- Establishes baseline

### Runner B: jingu
```
instance → proposer → structural gate → apply gate → test gate
        → if fail: structured feedback → proposer (up to max_attempts=3)
        → final accepted patch → write prediction
```

---

## Module Structure

```
jingu-swebench/
├── src/
│   ├── dataset/
│   │   ├── swebench-loader.ts      # load instances from HuggingFace / local JSONL
│   │   └── instance-types.ts       # BenchmarkInstance type
│   ├── workspace/
│   │   ├── workspace.ts            # Workspace interface + implementation
│   │   ├── git-worktree.ts         # checkout + reset + diff
│   │   └── patch-utils.ts          # apply patch, validate format
│   ├── proposer/
│   │   ├── proposer-adapter.ts     # instance → LLM call → PatchCandidate
│   │   ├── prompt-builder.ts       # system prompt + instance context
│   │   └── response-parser.ts      # extract patch from LLM output
│   ├── admission/
│   │   ├── structural-gate.ts      # patch non-empty, parseable
│   │   ├── apply-gate.ts           # git apply succeeds
│   │   ├── test-gate.ts            # pytest delta check
│   │   └── retry-feedback.ts       # failure → structured feedback for next attempt
│   ├── runner/
│   │   ├── raw-runner.ts           # baseline: single LLM call
│   │   ├── jingu-runner.ts         # governed: gates + retry loop
│   │   └── compare-runner.ts       # run both, produce comparison report
│   ├── output/
│   │   ├── predictions-writer.ts   # write SWE-bench predictions JSONL
│   │   ├── report-writer.ts        # write summary + failure breakdown
│   │   └── eventlog-writer.ts      # Jingu JSONL event log
│   ├── cli/
│   │   └── run.ts                  # CLI entry: --mode raw|jingu|compare --dataset lite|verified --n 20
│   └── types/
│       └── contracts.ts            # all shared types
├── scripts/
│   ├── run-lite.sh
│   ├── run-verified.sh
│   └── compare.sh
├── results/
│   ├── raw/
│   ├── jingu/
│   └── compare/
├── PLAN.md                         # this file
└── README.md
```

---

## Core Types

```typescript
// dataset
export type BenchmarkInstance = {
  instanceId: string
  repo: string
  baseCommit: string
  problemStatement: string
  hintsText?: string
}

// proposer output
export type PatchCandidate = {
  attempt: number
  summary: string
  patchText: string
  filesTouched: string[]
  reasoning?: string
}

// gate result
export type GateResult = {
  status: "pass" | "fail"
  code:
    | "EMPTY_PATCH"
    | "PARSE_FAILED"
    | "PATCH_APPLY_FAILED"
    | "TEST_EXEC_FAILED"
    | "TESTS_NOT_IMPROVED"
    | "ACCEPTED"
  message: string
  details?: Record<string, unknown>
}

// per-attempt
export type AttemptResult = {
  attempt: number
  candidate?: PatchCandidate
  structuralGate: GateResult
  applyGate?: GateResult
  testGate?: GateResult
  accepted: boolean
}

// per-instance final result
export type InstanceRunResult = {
  instanceId: string
  mode: "raw" | "jingu"
  accepted: boolean
  attempts: AttemptResult[]
  finalPatchText?: string
  durationMs: number
}
```

---

## Three Gates (v1)

### Gate 1 — Structural
- patch non-empty (> 10 chars)
- contains at least one `---` / `+++` / `@@` line (looks like a diff)
- fails fast, no workspace needed

### Gate 2 — Apply
- `git apply` succeeds on the workspace
- workspace resets to base on failure

### Gate 3 — Test delta
- run test command (e.g. `pytest -x -q`)
- collect: passed / failed / errored
- accept if: fail→pass count > 0, AND no pass→fail regressions
- reject if: no improvement, or regressions introduced

---

## Retry Feedback (v1)

On gate failure, build structured feedback for the next attempt:

```
Gate failed: PATCH_APPLY_FAILED
Error: patch does not apply cleanly to src/requests/adapters.py
Hunk 3 rejected.

Your previous attempt touched: src/requests/adapters.py
The file at HEAD looks like:
<first 40 lines>

Please produce a corrected patch. Focus only on the failing hunk.
```

On test failure:
```
Gate failed: TESTS_NOT_IMPROVED
Tests still failing after your patch:
- test_redirect_history (test_requests.py:142)
- test_max_redirects (test_requests.py:156)

Error output:
<last 30 lines of pytest output>

Your patch touched: src/requests/models.py
Please revise the patch to fix these specific test failures.
```

---

## Metrics to Report

| Metric | Description |
|--------|-------------|
| `resolved_%` | % instances where final patch passes all target tests (official SWE-bench metric) |
| `valid_patch_%` | % instances where at least one apply-able patch was produced |
| `invalid_output_%` | % instances with empty/unparseable patch on attempt 1 |
| `retry_recovery_%` | % of failed attempt-1 instances recovered by attempt 2-3 |
| `avg_attempts` | average attempts per instance (jingu only) |

### Output tables

**Table 1 — Summary**

| Mode | Dataset | Resolved % | Valid Patch % | Invalid Output % | Avg Attempts |
|------|---------|-----------|--------------|-----------------|-------------|
| raw  | Lite-20 | | | | 1.0 |
| jingu| Lite-20 | | | | |

**Table 2 — Failure breakdown**

| Mode | EMPTY_PATCH | APPLY_FAILED | TEST_EXEC_FAILED | NO_IMPROVEMENT |
|------|------------|-------------|-----------------|---------------|
| raw  | | | | |
| jingu| | | | |

---

## 5-Day Milestones

### Day 1 — Raw baseline works end-to-end
- [ ] Repo init (TypeScript, tsconfig, package.json)
- [ ] `BenchmarkInstance` type + mock loader (1 hardcoded instance)
- [ ] `Workspace` class: exec, applyPatch, diff, reset
- [ ] `raw-runner.ts`: build prompt → LLM call → parse patch → write prediction
- [ ] CLI: `node run.ts --mode raw`
- [ ] Success: runs one case, produces a patch file

### Day 2 — Jingu runner with 3 gates + retry
- [ ] `structural-gate.ts`
- [ ] `apply-gate.ts`
- [ ] `test-gate.ts`
- [ ] `retry-feedback.ts` (minimal — gate code + error tail)
- [ ] `jingu-runner.ts`: gate loop, max_attempts=3
- [ ] Success: can retry, gate failures visible in output

### Day 3 — Real dataset + parallel runs
- [ ] `swebench-loader.ts`: load from HuggingFace `SWE-bench/SWE-bench_Lite` JSONL
- [ ] `compare-runner.ts`: run raw + jingu on same instances
- [ ] Basic summary output to stdout
- [ ] Success: 5–10 cases, both modes, compare visible

### Day 4 — Metrics + event log
- [ ] `report-writer.ts`: both summary tables
- [ ] `eventlog-writer.ts`: JSONL per run (attempt, gate, verdict)
- [ ] Gate failure stats tracked per run
- [ ] Success: results/ dir has raw/, jingu/, compare/ outputs

### Day 5 — 20-case smoke run + first result
- [ ] Fix any workspace reset bugs from Day 3–4
- [ ] Run 20 Lite instances, both modes
- [ ] Produce first comparison report
- [ ] Success: Table 1 + Table 2 filled in, jingu >= raw on resolved %

---

## What NOT to do in v1

- Do NOT submit to official leaderboard yet (local first)
- Do NOT run Full dataset (534 Lite is enough, start with 20)
- Do NOT add LLM reviewer (deterministic gates first)
- Do NOT require p162/PEB (not a prerequisite)
- Do NOT build multi-agent (single proposer + retry is the story)

---

## Dependencies

```json
{
  "@anthropic-ai/sdk": "^0.80.0",
  "@aws-sdk/client-bedrock-runtime": "^3.0.0",
  "@jingu/policy-core": "file:../jingu-policy-core",
  "@jingu/trace": "file:../jingu-trace"
}
```

LLM: BedrockClient (same as benchmark-route1.mjs — no API key needed locally).

---

## Story to Tell (after results)

**If jingu > raw on resolved %:**
> "LLM single-shot is unreliable on real coding tasks. Jingu's deterministic gate + structured
> retry loop recovers X% of initially-failed instances — improving resolve rate from Y% to Z%
> on SWE-bench Lite."

**On invalid output reduction:**
> "Raw LLM produces unparseable/empty patches X% of the time. Jingu's structural gate catches
> these immediately and forces a retry, reducing wasted compute and improving valid patch rate."

**The core claim:**
> Jingu doesn't make the model smarter. It makes the model's output trustworthy by enforcing
> deterministic verification and structured repair — the same governance principle that makes
> production software reliable.

---

## p162 / p163 — Expected Impact on SWE-bench

### p162 — Principle Evidence Binding (PEB)

p162 要求 LLM 的原则声明必须绑定可验证的 repo 证据。

**在 SWE-bench 上的直接影响：**

| SWE-bench 失败类型 | p162 的作用 |
|-------------------|------------|
| Root cause misdiagnosis（诊断错误文件/函数）| PEB 要求 LLM 在 proposer prompt 中 cite 具体文件+行号，迫使它真正定位到正确位置 |
| Reasoning/code misalignment（分析说一套，patch 写另一套）| Evidence binding 把分析和代码修改锁在同一个 evidence anchor 上，漂移立刻可见 |
| Retry not converging（每次 retry 犯相同错误）| structured feedback + PEB 要求下一次 attempt 针对具体 evidence 修正，而不是随机重试 |

**结论：** p162 = "让 player 本身更强"。每次 attempt 的起点质量更高，不靠运气。

---

### p163 — PEB Replay + Drift Detection

p163 是在 p162 之上做 replay 和 drift 检测。

**在 SWE-bench 上的直接影响：**

| SWE-bench 实验痛点 | p163 的作用 |
|------------------|------------|
| 结果噪声大，分不清信号和运气 | replay 同一 instance 多次，确认结果稳定性 |
| 模型升级/prompt 改动后不知道影响范围 | drift detection 自动比对 evidence binding 分布变化 |
| "这个 case 之前过了现在不过了" | replay 给出可追溯的原因（binding 哪里漂移了）|

**结论：** p163 = "让训练和实验系统更强"。不直接改变每次 attempt 的质量，但让实验更可靠、更快迭代。

---

### 优先顺序建议

```
先跑 SWE-bench smoke run（20 cases）
  → 看 failure breakdown（是 root cause misdiagnosis？还是 apply 失败？还是 test 不改善？）
  → 如果主要失败在 "reasoning / code misalignment" 或 "retry not converging"
    → p162 是真正的瓶颈，值得现在做
  → 如果主要失败在 apply-gate（patch format 问题）或 test-gate（逻辑本身就错）
    → p162 影响有限，先把基础 runner 做稳，p162 排后面
```

**当前判断（未验证）：** p162 中等概率是 SWE-bench 的真实瓶颈之一，但需要第一批 smoke run 数据才能确认。

**不要先做 p162 再跑 benchmark** — 那样无法分离变量，看不出 p162 的实际贡献。

---

### 一句话区分

- **p162** = 让模型的每次尝试更扎实（grounded in evidence）
- **p163** = 让实验结果更可信（reproducible, drift-detected）

两者都有价值，但对 SWE-bench resolved% 的直接影响，p162 >> p163。
