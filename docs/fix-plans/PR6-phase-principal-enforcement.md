# PR 6 — Phase/Type/Principal Runtime Enforcement

**Week:** 5 (after PR5 provides gate config)
**Prerequisite:** PR2, PR5

---

## Goal

Upgrade from "surface declaration" to real cognition contract enforcement.

Current: agent writes `PHASE: analysis` in text, system does regex match.
After: phase boundary, required principals, and subtype contracts are enforced structurally.

This aligns with the core principle: "not post-hoc labels, but in-phase enforced contracts."

---

## Changes

### File: `declaration_extractor.py`

Enhanced extraction when bundle is active:
1. Structured JSON extraction (primary path when bundle schema available)
2. PhaseRecord populated with all required fields from bundle.phases
3. Missing required fields -> extraction failure (not silent default)

### New File: `scripts/phase_contract_validator.py`

Validates PhaseRecord against bundle.types contract:
```python
def validate_phase_contract(pr: PhaseRecord, bundle_types: dict) -> ContractResult:
    """Check PhaseRecord against type contract."""
    contract = bundle_types.get(pr.subtype)
    if not contract:
        return ContractResult(valid=False, reason=f"unknown subtype: {pr.subtype}")

    errors = []

    # Required principals check
    for req_p in contract["required_principals"]:
        if req_p not in pr.declared_principals:
            errors.append(f"missing required principal: {req_p}")

    # Forbidden principals check
    for forb_p in contract.get("forbidden_principals", []):
        if forb_p in pr.declared_principals:
            errors.append(f"forbidden principal declared: {forb_p}")

    # Required upstream phases check
    for req_phase in contract.get("required_upstream_phases", []):
        if req_phase not in pr.completed_phases:
            errors.append(f"required upstream phase not completed: {req_phase}")

    return ContractResult(
        valid=len(errors) == 0,
        errors=errors,
    )
```

### New File: `scripts/principal_validator.py`

Validates principal declarations against bundle.principals:
```python
def validate_principals(
    declared: list[str],
    phase: str,
    bundle_principals: dict,
) -> PrincipalValidationResult:
    """Check that declared principals are valid for this phase."""
    errors = []
    for p in declared:
        spec = bundle_principals.get(p)
        if not spec:
            errors.append(f"unknown principal: {p}")
            continue
        if phase not in spec["applies_to_phases"]:
            errors.append(f"principal {p} not applicable to phase {phase}")
    return PrincipalValidationResult(valid=len(errors) == 0, errors=errors)
```

### File: `step_sections.py`

Phase boundary enforcement:
1. Phase contract validation fires on every phase transition
2. Contract failure -> reject (not just score deduction)
3. `FIX_TYPE=execution` can no longer bypass all phases

```python
# On phase_record creation:
contract_result = validate_phase_contract(pr, bundle.types)
if not contract_result.valid:
    emit_decision("phase_contract_violation", errors=contract_result.errors)
    # REJECT — do not allow advance
```

### File: `bundle_compiler.py`

Compile `bundle.types` into runtime contract objects that validators consume.

---

## Phase Enforcement Rules (from bundle.phases)

### OBSERVE
* `required_fields`: phase, claims, evidence_refs
* `forbidden_downstream_actions`: code_patch, submit_patch
* Agent CANNOT write code during OBSERVE

### ANALYZE
* `required_fields`: phase, subtype, principals, claims, from_steps
* `forbidden_downstream_actions`: code_patch, submit_patch
* Agent CANNOT write code during ANALYZE
* Must declare causal_grounding, evidence_linkage

### DESIGN
* `required_fields`: phase, subtype, principals, claims, risks
* Must declare option_comparison, constraint_satisfaction

### EXECUTE
* `required_fields`: phase, subtype, principals, action_refs, from_steps
* Must have completed DESIGN or PLAN upstream
* Must declare minimal_change, action_grounding

---

## Events

* `phase_contract_validated` — pr.subtype, contract found, errors (if any)
* `phase_contract_violation` — which principals missing, which upstream phases incomplete
* `principal_validation_result` — declared principals checked against phase applicability

---

## New Files for Dockerfile COPY

* `scripts/phase_contract_validator.py`
* `scripts/principal_validator.py`

---

## Acceptance Criteria

1. Phase boundary violations produce REJECT (not just score deduction)
2. Missing required principal -> reject, not silent pass
3. Forbidden principal -> reject
4. `FIX_TYPE=execution` without prior DESIGN/PLAN phase -> reject
5. Contract validation events in decisions.jsonl
6. Smoke test: agent forced through proper phase sequence
7. Declaration extraction has real enforcement effect (not just logging)

---

## Impact

This is where Jingu becomes Jingu:

```text
Before: "PHASE:" markers are cosmetic text
After:  phase boundaries are enforced contracts
```

The system transitions from:
```text
Level 0.5 (pseudo-governed) -> Level 2 (enforced cognition)
```
