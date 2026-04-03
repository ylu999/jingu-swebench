# Cognitive Failure Cases — jingu-swebench

Cases extracted from real incidents during p169 experimental work.
These are candidates for Jingu CDP taxonomy / policy / benchmark.

---

## CF-ENV-001: ENV_LEAKAGE_HARDCODE_PATH

**Date**: 2026-04-03
**Context**: p169 runner EC2 setup — gate_runner.js crashes with ERR_MODULE_NOT_FOUND

### What Happened

`gate_runner.js` and `patch_admission_policy.js` both had hardcoded local paths:

```js
const _GATE_DIST = process.env.JINGU_TRUST_GATE_DIST
  ?? "/Users/ysl/jingu/repo/jingu-trust-gate/dist/src";
```

On EC2 via SSM, `$HOME` is empty. Path resolved to `/jingu-swebench/...` -> module not found.

Fix was applied reactively: one file at a time, triggered by crashes.

### Root Cause

```
LOCAL_ENV_LEAKAGE
= violation of Environment Independence
+ missing Global Invariant Enforcement
```

### CDP Failure Decomposition

| Phase | Actual | Correct |
|-------|--------|---------|
| OBSERVE | Only saw ERR_MODULE_NOT_FOUND | Should have: grep `/Users/` across repo first |
| ANALYZE | Reactive debugging (fix one crash) | Invariant-based: identify pattern class |
| DECIDE | Fix current failing file | Eliminate pattern from all files |
| EXECUTE | Fix one -> crash -> fix another | grep -> fix all -> add CI check |

### Violated Principles

- **P-ENV-001**: No hardcoded absolute local paths in code
- **P-ENV-002**: Build artifacts must not contain local state
- **P-SYS-003**: Pattern-class errors must be eliminated globally (not instance-by-instance)
- **P-SYS-004** (weak): Invalid assumptions (empty HOME) should fail fast with clear message

### Correct Fix Pattern

```js
// Step 1: runtime-safe resolution
import { homedir } from "os";
const HOME = process.env.HOME || homedir();

// Step 2: scan before fixing
// grep -rE "/Users/|/home/" scripts/ first

// Step 3: add CI invariant (future)
// reject if code contains "/Users/" or "/home/"
```

### Failure Attribution (p173 format)

```json
{
  "primary": {
    "code": "ENV_LEAKAGE_HARDCODE_PATH",
    "phase": "analyze",
    "type": "root_cause_analysis",
    "principal": "environment_independence",
    "confidence": 0.92,
    "reason": "Hardcoded absolute local path leaked into runtime code; reactive fix applied per-instance instead of eliminating the pattern"
  }
}
```

### Fix Quality

| Aspect | This Fix | Ideal Fix |
|--------|----------|-----------|
| Runtime correctness | PASS: homedir() fallback | PASS |
| Coverage | PARTIAL: 2 files fixed reactively | FULL: repo-wide scan first |
| Invariant enforcement | MISSING: no CI check | NEEDED: grep check in CI |
| Pattern elimination | REACTIVE | PROACTIVE |

### Key Lesson

> Don't fix instances, eliminate patterns.
> Local fix is fixing a bug. Invariant is fixing the system.

### Future Jingu Components

- New principal: `environment_independence`
- New validator: `validateNoLocalPath(code)` -> reject if `/Users/` or `/home/`
- New type: `environment_assumption_analysis`
- CI check: `grep -rE "/Users/|/home/" scripts/` in pre-commit

### Relevance to p169

This failure blocked p169 treatment batch launch by ~2 hours.
The error pattern (reactive debugging vs invariant enforcement) is exactly what CDP v1 is designed to detect:
if the agent had declared `type=diagnosis, principals=[causality, hypothesis_testing]`,
a validator could have flagged that the diagnosis stopped at the symptom (module not found)
instead of tracing to the root cause (environment assumption violation).
