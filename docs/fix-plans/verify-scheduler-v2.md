# Verify Scheduler v2 — Complete Design

Goal: Verify must always return useful signal. No more module fallback + timeout + blind retry.

Audit problem chain:
```
69 classes > 20 limit -> module fallback -> 60s timeout -> controlled_error -> ZERO signal
```

---

## Design Principles (5 iron rules)

### 1. No `overflow -> module fallback`
Too crude, highest risk.

### 2. Prioritize targeted subset, not coverage
Produce signal first, worry about full coverage later.

### 3. `partial signal > no signal`
Partial results are infinitely better than total timeout.

### 4. Timeout must scale with batch and scope
No fixed 60s across all scenarios.

### 5. Verify scheduling decisions must be visible
Every scope shrink / batching / timeout emits an event.

---

## Core Data Structures

```typescript
type VerifyScopeMode =
  | "targeted_classes"
  | "batched_classes"
  | "ultra_small_subset"
  | "module_scope";

interface VerifyTarget {
  classes: string[];
  estimated_test_count?: number;
  source: "fail_to_pass" | "patch_diff" | "sentinel" | "fallback";
}

interface VerifyBatch {
  batch_id: number;
  classes: string[];
  timeout_seconds: number;
}

interface VerifySchedulerPlan {
  mode: VerifyScopeMode;
  selected_classes: string[];
  batches: VerifyBatch[];
  overall_timeout_seconds: number;
  partial_allowed: boolean;
}

interface VerifyBatchResult {
  batch_id: number;
  passed: number;
  failed: number;
  timeout: boolean;
  signal_observed: boolean;
}

interface VerifyFinalResult {
  passed: number;
  failed: number;
  timed_out_batches: number;
  partial: boolean;
  signal_observed: boolean;
  mode: VerifyScopeMode;
}
```

---

## Scheduling Algorithm

### Step A: Collect candidate classes

Source priority:
1. FAIL_TO_PASS corresponding classes
2. Patch touched files related classes
3. Sentinel / regression classes
4. Fallback candidates

```typescript
function collectVerifyCandidates(ctx): string[] {
  return dedupe([
    ...ctx.failToPassClasses,
    ...ctx.patchRelatedClasses,
    ...ctx.sentinelClasses,
  ]);
}
```

### Step B: Dynamic budget (not fixed 20)

```typescript
function chooseClassBudget(total: number, cfg): number {
  const fractionCap = Math.ceil(total * cfg.max_class_fraction);
  return Math.max(
    cfg.min_classes_floor,
    Math.min(cfg.max_classes_hard_cap, fractionCap)
  );
}
```

Example:
* total = 69
* fraction = 0.3 -> 21
* hard_cap = 40
* Result: budget = 21

This is slightly flexible compared to fixed 20, and never triggers module fallback.

### Step C: Over-budget means batching, not fallback

```typescript
function buildBatches(
  classes: string[],
  batchSize: number,
  maxBatches: number
): VerifyBatch[] {
  return chunk(classes, batchSize)
    .slice(0, maxBatches)
    .map((group, i) => ({
      batch_id: i + 1,
      classes: group,
      timeout_seconds: 20,
    }));
}
```

Example with 21 classes:
* batch 1: 10 classes
* batch 2: 10 classes
* batch 3: 1 class

### Step D: Early stop during execution

Rules:
* 2 consecutive batch timeouts -> shrink to ultra-small subset
* Already got clear failing signal -> can return early
* Already got sufficient pass/fail signal -> don't need to run all

```typescript
function shouldEarlyStop(results: VerifyBatchResult[]): boolean {
  const consecutiveTimeouts = countTrailingTimeouts(results);
  const hasSignal = results.some(r => r.signal_observed);
  return consecutiveTimeouts >= 2 || hasSignalEnough(results);
}
```

### Step E: `no signal` is a system error, not a normal result

If all batches timeout or produce 0 signal:

1. Emit `verify_no_signal`
2. Automatically shrink to ultra-small subset (e.g., 5 tests)
3. Run one more time
4. Still no signal -> return `signal_observed=false` and mark as system error

This is infinitely better than "all timeout then blind retry".

---

## Pseudocode: Complete Flow

```typescript
function runControlledVerifyV2(ctx, cfg): VerifyFinalResult {
  const candidates = collectVerifyCandidates(ctx);
  const budget = chooseClassBudget(candidates.length, cfg.scope_selection);
  const selected = candidates.slice(0, budget);

  emit("verify_scope_selected", {
    mode: selected.length <= cfg.batching.batch_size
      ? "targeted_classes"
      : "batched_classes",
    candidate_count: candidates.length,
    selected_count: selected.length,
  });

  const batches = buildBatches(
    selected,
    cfg.batching.batch_size,
    cfg.batching.max_batches
  );

  const results: VerifyBatchResult[] = [];

  for (const batch of batches) {
    emit("verify_batch_started", {
      batch_id: batch.batch_id,
      class_count: batch.classes.length,
      timeout_seconds: batch.timeout_seconds,
    });

    const res = runBatch(batch);
    results.push(res);

    emit("verify_batch_completed", {
      batch_id: batch.batch_id,
      passed: res.passed,
      failed: res.failed,
      timeout: res.timeout,
      signal_observed: res.signal_observed,
    });

    if (shouldEarlyStop(results)) {
      break;
    }
  }

  if (!results.some(r => r.signal_observed)) {
    emit("verify_no_signal", {
      mode: "batched_classes",
      action_taken: "retry_ultra_small_subset",
    });

    const fallbackRes = runUltraSmallSubset(ctx, cfg);
    return mergeResults([...results, fallbackRes]);
  }

  return mergeResults(results);
}
```

---

## Events

### `verify_scope_selected`

```json
{
  "type": "verify_scope_selected",
  "mode": "batched_classes",
  "candidate_count": 69,
  "selected_count": 21,
  "batch_count": 3
}
```

### `verify_batch_started`

```json
{
  "type": "verify_batch_started",
  "batch_id": 1,
  "class_count": 10,
  "timeout_seconds": 20
}
```

### `verify_batch_completed`

```json
{
  "type": "verify_batch_completed",
  "batch_id": 1,
  "passed": 4,
  "failed": 1,
  "timeout": false,
  "signal_observed": true
}
```

### `verify_no_signal`

```json
{
  "type": "verify_no_signal",
  "mode": "batched_classes",
  "action_taken": "retry_ultra_small_subset"
}
```

---

## Core Principle

```text
NO SIGNAL = system-level error
PARTIAL SIGNAL = acceptable
```

---

## Expected Impact

### Before (current):

```text
agent:
  -> writes patch
  -> no feedback
  -> retry
  -> random guessing
```

### After:

```text
agent:
  -> writes patch
  -> gets partial signal (4 passed, 1 failed)
  -> knows direction is right/wrong
  -> corrects
```
