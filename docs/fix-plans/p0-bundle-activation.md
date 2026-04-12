# P0 — Bundle Activation Fix Plan

**Priority:** CRITICAL (blocks ALL governance)
**Audit items:** S2, S3, S4, S5
**Impact:** Without bundle, entire governance system offline — no phase injection, no principal enforcement, no structured extraction.

---

## Problem

`compile_bundle()` at `jingu_agent.py:627` throws an exception. The `except` at line 665 silently catches it, falls through to hardcoded fallback prompts (line 681). Zero structured visibility — error only in stdout, not in `decisions.jsonl` or `step_events.jsonl`.

Evidence from smoke run:
- `phase_injection = NONE`
- `principal_section = NONE`
- `extraction_metrics = {structured: 0, regex_fallback: 0, no_schema: 0, total: 0}`
- `prompt_snapshot.reasoning_protocol` = fallback text ("STEP 1 — before writing any code")

## Root Cause Candidates

1. `from bundle_compiler import compile_bundle` — module not on Python path in container
2. `bundle.json` exists at `/app/bundle.json` but `JINGU_BUNDLE_PATH` not set
3. Bundle JSON schema mismatch (compiled by newer jingu-cognition, loader expects older format)
4. Missing dependency in `bundle_compiler.py`

## Fix Steps

### Step 1: Reproduce locally and identify exact error

```bash
# On EC2 or in Docker container:
docker run --rm -it jingu-swebench:latest python3 -c "
import sys; sys.path.insert(0, '/app/scripts')
from bundle_compiler import compile_bundle
b = compile_bundle()
print('OK:', b.activation_report)
"
```

This will show the exact exception message.

### Step 2: Fix the import/path issue

**File:** `jingu_agent.py:627`

Likely fixes depending on root cause:
- If module not found: ensure `bundle_compiler.py` is in `scripts/` and Dockerfile copies it
- If bundle path wrong: set `JINGU_BUNDLE_PATH=/app/bundle.json` in Dockerfile ENV or in code
- If schema mismatch: update `jingu_loader` package version in requirements

### Step 3: Make bundle failure a HARD error with structured logging

**File:** `jingu_agent.py:665-667`

Change from:
```python
except Exception as _onb_exc:
    print(f"    [jingu_onboard] prompt load error (fallback): {_onb_exc}", flush=True)
```

To:
```python
except Exception as _onb_exc:
    _bundle_error = str(_onb_exc)
    print(f"    [jingu_onboard] BUNDLE_LOAD_FAILURE: {_bundle_error}", flush=True)
    # Emit structured event for decisions.jsonl
    if hasattr(self, '_decision_logger') and self._decision_logger:
        self._decision_logger.log({
            "type": "bundle_load_failure",
            "error": _bundle_error,
            "fallback_active": True,
            "impact": "governance_offline",
        })
```

### Step 4: Add activation proof to run_report

After successful bundle load (line 639), emit activation proof:
```python
print(f"    [jingu_onboard] BUNDLE_ACTIVATED: version={_report.bundle_version} "
      f"phases={_report.phases_compiled} contracts={_report.contracts_compiled} "
      f"principals={_report.principals_total}", flush=True)
```

### Step 5: Add bundle status to prompt_snapshot

In the prompt snapshot assembly (~line 724), add:
```python
"bundle_status": "active" if _phase_prompt_parts else "fallback",
"bundle_error": _bundle_error if not _phase_prompt_parts else None,
```

## Verification

1. `compile_bundle()` succeeds — `prompt_snapshot.phase_injection != NONE`
2. `extraction_metrics.structured > 0` — structured schema available
3. `decisions.jsonl` contains bundle activation record (or failure record)
4. Run report contains `bundle_version` field
5. Smoke test 1 instance — confirm phase prompts from bundle, not fallback

## Dependencies

None — this is the highest priority fix. All other fixes (P1-P5) are more effective when bundle is active.
