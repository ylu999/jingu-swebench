# P3 — Bundle Externalization Fix Plan

**Priority:** MEDIUM (after P0 makes bundle work, move hardcoded values into it)
**Audit items:** C2-C16, T1-T13, T15
**Impact:** All configurable values in one place, versionable, no code change needed to tune.

---

## Problem

51 hardcoded values scattered across 8 files. Changing any value requires code change + image rebuild + ECR push. No way to A/B test different configurations without code branches.

## Design

### Bundle Config Section

Add `runtime_config` section to compiled bundle (output of jingu-cognition compiler):

```json
{
  "runtime_config": {
    "controlled_verify": {
      "targeted_scope_max_classes": null,
      "timeout_base_s": 60,
      "timeout_per_class_s": 2,
      "timeout_max_s": 300,
      "sentinel_max": 3,
      "subop_timeout_s": 30
    },
    "gates": {
      "analysis_max_rejects": 2,
      "design_max_rejects": 2,
      "retryable_loop_limit": 3,
      "fake_loop_limit": 3,
      "execute_redirect_limit": 3
    },
    "agent": {
      "no_signal_threshold": 15,
      "max_pytest_feedback_bytes": 4096,
      "small_patch_max_lines": 30,
      "reviewer_max_tokens": 1024,
      "docker_pull_timeout_s": 600
    },
    "principal_thresholds": {
      "causal_grounding": 0.5,
      "evidence_linkage": 0.5,
      "minimal_change": 0.7,
      "ontology_alignment": 0.7,
      "phase_boundary_discipline": 0.7,
      "action_grounding": 0.7,
      "constraint_satisfaction": 0.7,
      "result_verification": 0.7,
      "option_comparison": 0.7,
      "uncertainty_honesty": 0.7,
      "evidence_completeness": 0.7,
      "differential_diagnosis": 0.7
    }
  }
}
```

### Python Accessor

Add to `scripts/bundle_config.py`:
```python
"""Runtime config accessor — loads from bundle, falls back to defaults."""

_DEFAULTS = {
    "controlled_verify.targeted_scope_max_classes": None,  # no limit
    "controlled_verify.timeout_base_s": 60,
    "controlled_verify.timeout_per_class_s": 2,
    "controlled_verify.timeout_max_s": 300,
    # ... all defaults
}

_config = {}  # populated from bundle at startup

def init_from_bundle(bundle):
    """Called after successful compile_bundle()."""
    global _config
    rc = getattr(bundle, 'runtime_config', None)
    if rc:
        _config = _flatten(rc)

def get(key: str, default=None):
    """Get config value. Falls back to _DEFAULTS if not in bundle."""
    if key in _config:
        return _config[key]
    if key in _DEFAULTS:
        return _DEFAULTS[key]
    return default
```

### Migration Pattern

For each hardcoded value, change from:
```python
_AG_MAX_REJECTS = 2
```

To:
```python
from bundle_config import get as _cfg
_AG_MAX_REJECTS = _cfg("gates.analysis_max_rejects", 2)
```

## Migration Order

1. **Phase 1** — Create `bundle_config.py` with all defaults matching current hardcoded values
2. **Phase 2** — Replace hardcoded values with `_cfg()` calls (pure refactor, no behavior change)
3. **Phase 3** — Add `runtime_config` to bundle compiler (jingu-cognition side)
4. **Phase 4** — Test: change a value in bundle, verify it takes effect without code change

## Items to Migrate

### Timeouts (6 items)
C2, C4, C5, C15, C16 + timeout scaling params

### Loop Limits (5 items)
C6, C7, C8, C9, C10, C11

### Size Limits (3 items)
C1, C12, C13, C14

### Score Thresholds (13 items)
T1-T13, T15

**Total: 27 unique values to externalize.**

## Verification

1. All tests pass with `bundle_config.py` returning same defaults as current hardcoded values
2. Changing a value in bundle JSON changes runtime behavior
3. `log_limit()` (from P2) reports configured value from bundle, not hardcoded

## Dependencies

- **P0** must be done first (bundle must load successfully)
- **P2** should be done first (logging shows which values are actually being used)
- jingu-cognition compiler needs `runtime_config` support (separate task)
