# PR 5 — Gate Config Hydration from Bundle

**Week:** 4 (after PR2 provides schema v2)
**Prerequisite:** PR2

---

## Goal

Analysis/design/principal gates truly controlled by bundle config. No more hardcoded thresholds or force-pass limits in code.

---

## Changes

### File: `analysis_gate.py`

1. Gate threshold from bundle:
```python
# Before:
_THRESHOLD = 0.5  # hardcoded

# After:
_THRESHOLD = bundle.gates.analysis_gate.threshold  # from bundle
```

2. `max_rejects_before_force_pass` from bundle (default: 0 = disabled)

3. `required_checks` from bundle:
```python
# Before: hardcoded check list
# After: bundle.gates.analysis_gate.required_checks
```

### File: `design_gate.py`

Same pattern as analysis_gate:
1. Threshold from bundle
2. Force-pass limit from bundle (default: 0)
3. Required checks from bundle

### File: `principal_gate.py`

1. `reject_on_missing_required_principal` from bundle
2. `reject_on_declared_but_unsubstantiated_principal` from bundle

### File: `step_sections.py`

Replace all hardcoded gate limits:
```python
# Before:
_AG_MAX_REJECTS = 2
_DG_MAX_REJECTS = 2
_RETRYABLE_LOOP_LIMIT = 3
_FAKE_LOOP_LIMIT = 3
_EXECUTE_REDIRECT_LIMIT = 3

# After:
_AG_MAX_REJECTS = bundle.limits.analysis_gate_max_rejects  # default 0
_DG_MAX_REJECTS = bundle.limits.design_gate_max_rejects    # default 0
_RETRYABLE_LOOP_LIMIT = bundle.limits.retryable_loop_limit # default 2
_FAKE_LOOP_LIMIT = bundle.limits.fake_loop_limit           # default 0
_EXECUTE_REDIRECT_LIMIT = bundle.limits.execute_redirect_limit  # default 2
```

### File: `principal_inference.py`

All 12+ thresholds from bundle:
```python
# Before:
threshold=0.5  # or 0.7, scattered

# After:
threshold=bundle.principals.causal_grounding.threshold
```

### File: `gate_runner.py` (or equivalent)

Gate verdict events include config snapshot:
```json
{
  "type": "gate_verdict",
  "gate": "analysis_gate",
  "verdict": "reject",
  "config": {
    "threshold": 0.7,
    "max_rejects": 0,
    "checks": ["code_grounding", "causal_chain", "alternative_hypothesis"]
  }
}
```

---

## Key Design Decision: Default `max_rejects_before_force_pass = 0`

The audit proved force-pass destroys governance credibility. Default must be **disabled** (0).

If needed, must be **explicitly configured** in the bundle with justification:
```json
{
  "analysis_gate": {
    "max_rejects_before_force_pass": 3,
    "_justification": "regex-based checks produce false positives in non-structured mode"
  }
}
```

---

## Acceptance Criteria

1. Without code changes, only bundle changes can alter gate behavior
2. `decisions.jsonl` shows gate config snapshot in every verdict
3. Force-pass requires explicit bundle configuration (not default)
4. Principal inference thresholds all from bundle
5. Smoke test: verify different threshold values take effect
