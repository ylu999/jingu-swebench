# PR 2 — Bundle Schema v2 and Compiler Support

**Week:** 2 (after PR1 makes bundle load)
**Prerequisite:** PR1

---

## Goal

Move all behavior-affecting hardcoded values into the bundle. Compiler validates schema. Runtime reads from compiled bundle, not scattered code constants.

---

## Changes

### File: `bundle.schema.json` (new or extended)

Add top-level fields as specified in `bundle-schema-v2.md`:
* `runtime`
* `phases`
* `principals`
* `types`
* `gates`
* `verify`
* `limits`
* `logging`
* `fallback_policy`

### File: `bundle_compiler.py`

1. Parse schema v2 sections
2. Validate required fields (missing required field -> compile error, not silent default)
3. Compile into runtime contract objects
4. Emit compilation warnings for:
   * Missing `runtime_config` section (warns, does not fail — backward compat with v1)
   * Type mismatches in values
   * Negative values for limits

### File: `bundle_loader.py` (or equivalent Python loader)

Add accessors:
```python
def get_gate_config(phase: str) -> dict: ...
def get_verify_config() -> dict: ...
def get_limit(name: str, default: int) -> int: ...
def get_principal_threshold(name: str, default: float) -> float: ...
```

### Files: `phase_contracts.py`, `gate_config.py`, `verify_config.py`

Remove scattered defaults. Replace with:
```python
from bundle_loader import get_limit
_AG_MAX_REJECTS = get_limit("analysis_gate_max_rejects", 2)
```

---

## Acceptance Criteria

1. All gate/verify/limit values readable from compiled bundle
2. Bundle missing required field -> compile failure (not silent default)
3. Runtime output shows `bundle_version` in reports
4. Changing a value in bundle JSON changes runtime behavior (no code change needed)
5. v1 bundles still work (graceful degradation with warnings)

---

## Note on Scope

This PR is about **schema + compiler + loader**. The actual migration of each hardcoded value happens in PR3 (limits), PR5 (gates), and PR4 (verify). This PR provides the infrastructure they depend on.
