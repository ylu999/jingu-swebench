# DESIGN-D3: SST Audit — Shadow Contracts & Projection Chains

Date: 2026-04-12
Source: Automated codebase audit by design-sst-audit agent

---

## Canonical Sources (Authoritative Definitions)

| Source | File | What it owns |
|--------|------|-------------|
| Bundle master | `bundle.json` | All phase/subtype contracts, field schemas, principals, routing |
| Analysis contract | `cognition_contracts/analysis_root_cause.py` | analysis.root_cause gate rules, prompt guidance, field specs |

## Confirmed Projection Chains (Source -> Renderer -> Surface)

### Chain 1: Schema Field Descriptions
```
bundle.json contracts.*.schema.properties.*.description
  -> schema_field_guidance.render_schema_field_guidance()
    -> jingu_model._build_phase_record_tool() [tool description]
    -> phase_prompt.build_phase_prefix() [phase prefix]
```
**Status**: GOVERNED by test_replay_gate.py + test_contract_consistency.py

### Chain 2: Analysis Prompt Guidance
```
analysis_root_cause.PROMPT_GUIDANCE
  -> phase_prompt.build_phase_prefix("ANALYZE")
    -> agent system message prefix
```
**Status**: GOVERNED by test_replay_gate.py

### Chain 3: Analysis Schema
```
analysis_root_cause.SCHEMA_PROPERTIES
  -> phase_schemas.py ANALYZE_SCHEMA (line 251, direct reference)
    -> Bedrock structured output tool schema
```
**Status**: PARTIALLY GOVERNED (ANALYZE derived, others shadow)

### Chain 4: Principal Requirements
```
bundle.json contracts.*.principals
  -> subtype_contracts.py (dynamic load)
    -> principal_gate.evaluate_admission()
```
**Status**: GOVERNED by existing tests

## Shadow Contracts (Independently Defined -- NOT Derived)

### SHADOW-1: phase_schemas.py — Non-ANALYZE schemas (P0 priority)
- **File**: `scripts/phase_schemas.py:29-313`
- **What**: OBSERVE, UNDERSTAND, JUDGE schemas are hardcoded JSON, not compiled from bundle
- **Risk**: Field names/descriptions drift from bundle.json
- **Fix**: Derive all phase schemas from bundle.json via compiler, like ANALYZE

### SHADOW-2: principal_gate.py — Feedback strings (P1 priority)
- **File**: `scripts/principal_gate.py:86-100, 328-350`
- **What**: `missing_root_cause`, `plan_not_grounded_in_root_cause` etc. hardcoded
- **Risk**: Feedback text drifts from actual contract requirements
- **Fix**: Import feedback templates from bundle or cognition_contracts

### SHADOW-3: strategy_prompts.py — Routing strategy names (P2 priority)
- **File**: `scripts/strategy_prompts.py:21-78`
- **What**: `complete_causal_chain`, etc. hardcoded mapping
- **Risk**: Strategy names drift from bundle.json routing section
- **Fix**: Wire to bundle.json routing section

### SHADOW-4: phase_prompt.py — Template constants (P2 priority)
- **File**: `scripts/phase_prompt.py:43-132`
- **What**: `_UNDERSTAND_GUIDANCE`, `_OBSERVE_GUIDANCE`, etc. (7 independent templates)
- **Risk**: Phase guidance drifts from bundle prompts
- **Fix**: Replace with bundle-derived prompts (same pattern as ANALYZE)

### SHADOW-5: analysis_gate.py — Field trimming rules (P3 priority)
- **File**: `scripts/analysis_gate.py:450-467`
- **What**: Embedded truncation rules and reason codes
- **Risk**: Low (internal implementation detail, not contract)
- **Fix**: Optional -- extract to shared config if trimming rules need to be consistent

## Projection Drift Risk Matrix

| Shadow | Drift Probability | Impact if Drifts | Fix Effort |
|--------|-------------------|-----------------|------------|
| SHADOW-1 (phase_schemas) | HIGH | Agent sees wrong field requirements | MEDIUM |
| SHADOW-2 (feedback strings) | MEDIUM | Agent gets misleading retry hints | LOW |
| SHADOW-3 (strategy routing) | LOW | Wrong repair strategy selected | MEDIUM |
| SHADOW-4 (phase templates) | MEDIUM | Agent gets stale guidance | MEDIUM |
| SHADOW-5 (trim rules) | LOW | Truncation inconsistency | LOW |

## Prioritized Elimination Sequence

1. **P0**: Consolidate phase_schemas.py -- all schemas from bundle via compiler
2. **P1**: Eliminate principal_gate feedback shadows -- import from bundle/contracts
3. **P2**: Wire strategy_prompts + phase_prompt templates to bundle
4. **P3**: Extract analysis_gate trim rules (optional)

## Current Governance Coverage

| What | Governed? | By |
|------|-----------|-----|
| Schema field descriptions -> tool desc | YES | test_replay_gate.py |
| Schema field descriptions -> phase prefix | YES | test_replay_gate.py |
| Bundle schema <-> gate rules alignment | YES | test_contract_consistency.py |
| Non-ANALYZE phase schemas | NO | Shadow (SHADOW-1) |
| Feedback strings | NO | Shadow (SHADOW-2) |
| Strategy routing | NO | Shadow (SHADOW-3) |
| Phase guidance templates | NO | Shadow (SHADOW-4) |
