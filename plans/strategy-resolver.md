# Plan: Strategy Resolver — Principle Dependency Graph + Runtime Resolution

## Goal
Implement `resolveStrategy()` + `selectStrategies()` in `src/runner/strategy-resolver.ts`.
Wire into `runMultiStrategy()`. Record resolution status in summary.json.

## Why
p-defensive PATCH_APPLY_FAILED reveals a Layer A → Layer C dependency:
  no grounding → strict-observed-only = structurally invalid
This is not a bug — it's an invalid principle combination at runtime.
Fix: degrade verificationPolicy instead of letting it fail silently.

## Components

### 1. RuntimeContext (injected at runner level)
```ts
type RuntimeContext = {
  injectedFiles: string[]
}
```
Source: jingu-runner already knows injectedFiles — pass it out to multi-strategy-runner.

### 2. resolveStrategy(strategy, ctx) → ResolutionResult
Cases:
- strict-observed-only + no injected files → downgrade to "standard", status="degraded"
- all other cases → status="valid"
Returns: { effectiveStrategy, status, reason? }

### 3. scoreStrategy(strategy, ctx, resolution) → number
- invalid → -Infinity
- degraded → -50 base
- hasGrounding + test-driven → +20
- hasGrounding + symbol-first → +15
- !hasGrounding + file-first → -30
- injectedFiles.length <= 1 + minimal-fix → +10
- injectedFiles.length > 1 + root-cause → +10

### 4. selectStrategies(strategies, ctx) → SelectedStrategy[]
- resolve + score all
- filter score > -Infinity (keep degraded, drop invalid-equivalent)
- sort desc by score
- slice(0, maxK=3)
- return with { strategy, score, status, reason }

## Integration
- InstanceRunResult needs injectedFiles? NO — expose from runJingu return value
- jingu-runner.ts: add injectedFiles to InstanceRunResult
- multi-strategy-runner.ts: build ctx from first strategy run? NO
  → ctx must be built BEFORE running strategies (pre-run context)
  → split jingu-runner into: (a) buildContext(instance) → files, (b) runWithContext(files, strategy)
  → OR: resolve strategy eagerly using a cheap file-list heuristic

## Simpler approach (avoids runner refactor)
Build ctx from instance metadata only (no actual file read needed for resolution):
  hasGrounding = true (assume files will be injected; resolver only fires at strategy select time)
  ACTUAL ctx built after first file injection pass

Wait — the real issue is simpler:
  p-defensive fails because file-first localization finds NO files to inject.
  The resolver needs to know injectedFiles COUNT at patch-generation time.

Best approach: resolve inside jingu-runner, AFTER readWorkspaceFiles(), BEFORE buildUserPrompt().
  → pass effectiveStrategy (potentially downgraded) to prompt-builder
  → log resolution result in AttemptResult

## Revised integration plan
1. Add ResolutionResult to AttemptResult (optional field)
2. In jingu-runner, after readWorkspaceFiles():
   - build ctx = { injectedFiles: Object.keys(fileContents) }
   - resolveStrategy(strategy, ctx) → effectiveStrategy
   - use effectiveStrategy for buildUserPrompt
   - record resolution in attempt log
3. multi-strategy-runner: collect resolution status from attempts for summary

## Steps
1. Write src/runner/strategy-resolver.ts (resolveStrategy + scoreStrategy + selectStrategies)
2. Add ResolutionResult type to contracts.ts (or strategy.ts)
3. Add injectedFiles/resolution to AttemptResult
4. Wire resolveStrategy into jingu-runner (post file-read, pre prompt-build)
5. Wire selectStrategies into runMultiStrategy (pre-run, using instance-level heuristic ctx)
6. Add resolution stats to run-multi summary output
7. Build + validate on django__django-11099

## Verification
Run: npm run run:multi -- --n 300 --instance-ids django__django-11099
Expected:
  p-defensive: status=degraded reason=strict_requires_files → no longer PATCH_APPLY_FAILED
  p-minimal, p-root-cause: status=valid (unchanged)
