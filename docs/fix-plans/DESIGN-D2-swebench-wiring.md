# DESIGN-D2: jingu-swebench Python Wiring

Date: 2026-04-12
Source: design-swebench agent analysis

---

## Key Design Decision

Implementation Governance is a **compile-time meta-governance layer**, NOT a runtime agent phase.

- The 5 subtypes describe audit/verification activities, not agent reasoning phases
- Principals are CI-level principals, not agent-declared principals
- Consumed by replay gate + CI scripts, not by the runtime agent loop
- No inference rules initially (all principals at stage 0: ontology_registered)

## Files to Modify

### 1. `scripts/cognition_contracts/__init__.py`
- Add import of new `implementation_governance` module

### 2. `scripts/subtype_contracts.py` (lines 76-186)
- Add 5 new implementation governance subtypes to `SUBTYPE_CONTRACTS`
- Each maps to phase "IMPLEMENTATION_GOVERNANCE"
- No required_principals initially (all stage 0)
- Import from `cognition_contracts.implementation_governance`

### 3. `scripts/principal_gate.py` (lines 28-33)
- Add `"IMPLEMENTATION_GOVERNANCE": []` to `PHASE_REQUIRED_PRINCIPALS`

### 4. `bundle.json`
- Add `"IMPLEMENTATION_GOVERNANCE"` to `phases[]` array
- Add 5 contract entries (one per subtype)

## New Files

### 1. `scripts/cognition_contracts/implementation_governance.py`
SST module following `analysis_root_cause.py` pattern. Exports:
- `PHASE = "IMPLEMENTATION_GOVERNANCE"`
- `SUBTYPES` — list of 5 subtype strings
- `PRINCIPALS` — list of 6 principal strings
- `POLICIES` — list of 6 policy definitions
- `FAILURE_TAXONOMY` — failure type -> trigger -> repair target
- `REPAIR_ROUTING` — which layer to fix for each failure

### 2. `tests/test_implementation_governance.py`
Tests:
1. All 5 subtypes exist in SUBTYPE_CONTRACTS
2. Phase mapping: all map to "IMPLEMENTATION_GOVERNANCE"
3. Principal lifecycle: all 6 at stage 0, fakeCheckEligible=false
4. No required principals (no runtime enforcement yet)
5. Accessor functions work for new phase
6. Bundle.json contains implementation governance contracts
7. Schema properties have descriptions (SST completeness)
8. Cross-surface consistency between bundle and cognition_contracts

### 3. `scripts/implementation_governance_gate.py` (P2, optional)
Compile-time gate with 5 checks:
- Canonical source exists
- Projection chain integrity
- No shadow contracts
- Cross-surface consistency
- Injection path traceable

## subtype_contracts.py Integration

```python
"implementation.source_of_truth_identification": {
    "phase": "IMPLEMENTATION_GOVERNANCE",
    "required_principals": [],
    "expected_principals": ["single_source_of_truth_preservation"],
    "forbidden_principals": [],
    "required_fields": [],
    "allowed_next": ["IMPLEMENTATION_GOVERNANCE"],
    "repair_target": "IMPLEMENTATION_GOVERNANCE",
},
# ... 4 more subtypes same pattern
```

## principal_inference.py Integration
None initially. All 6 new principals at stage 0. Future work item.

## Replay Gate Extension
- Stage 1: Bundle compiles with implementation governance contracts
- Stage 2: Implementation governance schema descriptions complete
- New Stage 7: Cross-surface shadow contract detection at CI time

## Test Plan Summary

| Test file | What it verifies |
|-----------|-----------------|
| `test_implementation_governance.py` | Contract structure, lifecycle, accessors |
| `test_replay_gate.py` (extend) | Bundle compiles with new contracts |
| `test_contract_consistency.py` (extend) | Cross-surface alignment for new contracts |
