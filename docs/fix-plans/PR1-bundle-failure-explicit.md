# PR 1 — Bundle Failure Must Be Explicit

**Week:** 1 (do first, blocks everything)
**Prerequisite:** none

---

## Goal

Make governance-offline visible, controllable, and impossible to fake normal.

Current state:
```python
try:
  compile_bundle()
except:
  fallback()  # silent, invisible, everything pretends to be fine
```

Must become:
```python
try:
  compile_bundle()
except Exception as e:
  emit_event("bundle_load_failure", e)
  raise FatalError("Governance offline")
```

---

## Root Cause (from p236 audit + P0 agent investigation)

`bundle_compiler.py` is **missing from the Dockerfile COPY list** (lines 56-109).
Container does `from bundle_compiler import compile_bundle` -> `ModuleNotFoundError`.
Caught by bare `except Exception` at `jingu_agent.py:665`. Silent fallback.

Also missing: `design_gate.py`.

---

## Changes

### File: `Dockerfile`

1. Add `scripts/bundle_compiler.py` to COPY list (line ~64)
2. Add `scripts/design_gate.py` to COPY list
3. Add `ENV JINGU_BUNDLE_PATH=/app/bundle.json` (after line 127)

### File: `jingu_agent.py` (lines 626-707)

1. `compile_bundle()` exception must NOT be silently caught

Replace lines 665-667:
```python
except Exception as _onb_exc:
    print(f"    [jingu_onboard] prompt load error (fallback): {_onb_exc}", flush=True)
```

With:
```python
except Exception as _onb_exc:
    import traceback as _tb
    _bundle_error_msg = (
        f"[BUNDLE_LOAD_FAILURE] compile_bundle() failed: {_onb_exc}\n"
        f"{''.join(_tb.format_exception(type(_onb_exc), _onb_exc, _onb_exc.__traceback__))}"
    )
    print(f"    {_bundle_error_msg}", flush=True)
    _bundle_load_error: str | None = _bundle_error_msg
```

2. Introduce `BundleLoadResult` concept:

```python
# After successful compile_bundle() (line 629):
_bundle_activation_proof = {
    "bundle_loaded": True,
    "bundle_version": _report.bundle_version,
    "compiler_version": _report.compiler_version,
    "generator_commit": _report.generator_commit,
    "phases_compiled": _report.phases_compiled,
    "contracts_compiled": _report.contracts_compiled,
    "principals_total": _report.principals_total,
    "principals_inference_eligible": _report.principals_inference_eligible,
    "principals_fake_check_eligible": _report.principals_fake_check_eligible,
    "activation_ok": _report.activation_ok,
    "prompt_warnings_count": len(_report.prompt_warnings),
}

# In the fallback path:
_bundle_activation_proof = {
    "bundle_loaded": False,
    "error": str(_onb_exc),
    "fallback_active": True,
}
```

3. Runtime context must carry `degraded_mode`:

```python
runtimeContext.degraded_mode = True  # when bundle fails
```

4. Prompt must explicitly show degradation:

```text
[WARNING] Governance degraded: bundle not loaded
```

5. Kill-switch for benchmark mode (strongly recommended):

```python
if degraded_mode and mode == "benchmark":
    ABORT RUN
```

Because in benchmark, fallback = noise data / invalid results.

### File: `run_with_jingu_gate.py` (line ~1067)

Add `bundle_activation` to `run_report.json`:
```python
"bundle_activation": getattr(JinguProgressTrackingAgent, '_bundle_activation_proof',
                             {"bundle_loaded": "unknown"}),
```

### File: `decision_logger.py`

Emit `DecisionEvent` at attempt start:
```python
self._decision_logger.log(DecisionEvent(
    decision_type="bundle_activation",
    step_n=0,
    timestamp_ms=time.time() * 1000,
    verdict="active" if _bundle_activation_proof.get("bundle_loaded") else "fallback",
    signals_evaluated=_bundle_activation_proof,
    reason_text="Bundle compilation result",
))
```

### File: `prompt_snapshot.py` (or prompt assembly in jingu_agent.py ~line 724)

Add:
```python
"bundle_status": "active" if _phase_prompt_parts else "fallback",
"bundle_error": _bundle_error if not _phase_prompt_parts else None,
```

---

## New Events

* `bundle_load_success` — bundle compiled successfully, activation proof attached
* `bundle_load_failure` — bundle failed, error + fallback flag
* `bundle_fallback_activated` — degraded mode entered

---

## Acceptance Criteria

1. Bundle failure: stdout AND decisions.jsonl both show structured event
2. Benchmark mode: bundle failure -> run aborted (not silent fallback)
3. prompt_snapshot: can tell whether fallback or bundle prompts were used
4. run_report.json: contains `bundle_activation` with version info
5. Smoke test 1 instance: `phase_injection != NONE`, `principal_section != NONE`
6. decisions.jsonl has `bundle_activation` record at attempt start
7. `extraction_metrics.structured > 0` when bundle loads successfully

---

## Why This Is PR 1

The audit conclusively proved: without bundle loading, ALL other governance is offline.
No cognition, no phase enforcement, no principal gates, no structured extraction.
Nothing else matters until this works.
