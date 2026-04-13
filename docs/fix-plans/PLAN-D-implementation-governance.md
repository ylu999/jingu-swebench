# PLAN-D: Implementation Governance Policy Pack

Date: 2026-04-12
Origin: p236 audit + SST refactoring self-audit
Status: DESIGN PHASE

---

## Problem Statement

The SST (Single Source of Truth) refactoring revealed a systemic pattern:
implementation drift occurs when multiple surfaces (tool description, phase prompt,
gate rules, bundle schema) independently define the same contract instead of
deriving from a single authoritative source.

Current state: SST fix was applied manually (bundle.json schema descriptions as
single source, renderer projects to tool desc + phase prefix). But there is no
**governance system** to prevent future implementation drift.

## Goal

Build a compile-time + runtime governance system that:
1. Detects when implementation surfaces diverge from their canonical source
2. Classifies the failure type (shadow contract, projection drift, injection gap)
3. Routes repair to the correct layer
4. Prevents new shadow contracts from being introduced

## Architecture: Two Layers

### Layer 1: jingu-policy-core (TypeScript — machine-checkable principles)

Module: `src/implementation-governance/`

Files:
- `types.ts` — Core type definitions (ImplementationSurface, ProjectionChain, CanonicalSource, ShadowContract)
- `evidence.ts` — Evidence types (CanonicalSourceRef, ProjectionDerivationRef, InjectionPathRef, CrossSurfaceAuditRef)
- `cognition.ts` — Cognition subtypes for implementation governance phase
- `policies.ts` — Policy definitions (canonical_source_required, projection_files_cannot_define_contracts, etc.)
- `failures.ts` — Failure taxonomy (shadow_contract_detected, projection_drift, injection_gap, cross_surface_inconsistency)
- `repair.ts` — Repair routing (which layer to fix for each failure type)
- `validators.ts` — Compile-time validators (check projection chain integrity, detect shadow contracts)
- `index.ts` — Public API

### Layer 2: jingu-swebench (Python — runtime enforcement)

Wiring into existing harness:
- Phase/subtype registry: Add IMPLEMENTATION_GOVERNANCE subtypes to `subtype_contracts.py`
- Phase record schema: Add implementation governance fields to bundle.json
- Prompt injection: Compile implementation governance prompts from bundle
- Gate stage: Add IMPLEMENTATION_GOVERNANCE gate in pipeline (like replay gate)
- Retry router: Route implementation governance failures to correct repair
- Telemetry: Log implementation governance signals
- Tests: test_implementation_governance.py

## Cognition Subtypes

| Subtype | Purpose |
|---------|---------|
| `implementation.source_of_truth_identification` | Identify the canonical source for a contract |
| `implementation.injection_path_audit` | Verify the injection path from source to all surfaces |
| `implementation.projection_derivation` | Verify each surface derives from (not copies) the source |
| `implementation.cross_surface_consistency` | Check all surfaces agree on the same contract |
| `implementation.shadow_contract_detection` | Detect surfaces that define contracts independently |

## Principals

| Principal | Description |
|-----------|-------------|
| `single_source_of_truth_preservation` | Every contract has exactly one authoritative definition |
| `projection_not_definition` | Consumer surfaces project from source, never redefine |
| `injection_path_accountability` | Every injection from source to surface is traceable |
| `cross_surface_consistency` | All surfaces presenting the same contract must agree |
| `no_shadow_contracts` | No surface may independently define a contract |
| `compile_over_handwrite` | Prefer compiled/generated artifacts over hand-written copies |

## Policies

| Policy ID | Rule |
|-----------|------|
| `canonical_source_required` | Every contract must have a declared canonical source file |
| `projection_files_cannot_define_contracts` | Files that project/render contracts cannot add new fields |
| `injection_path_must_be_traceable` | From source to every surface, the path must be code-traceable |
| `cross_surface_delta_zero` | All surfaces for the same contract must produce identical field sets |
| `shadow_contract_ci_detection` | CI must detect when a new hardcoded contract appears |
| `compile_time_projection_verification` | Projection chain must be verified at compile/test time (replay gate) |

## Evidence Types

| Evidence | What it proves |
|----------|---------------|
| `canonical_source_ref` | Points to the file:line that is the authoritative definition |
| `projection_derivation_ref` | Shows the code path from source to rendered surface |
| `injection_path_ref` | Shows the runtime injection chain (compile -> load -> inject) |
| `cross_surface_audit_ref` | Compares N surfaces and lists any deltas |
| `shadow_contract_ref` | Points to a surface that defines a contract independently |

## Failure Taxonomy

| Failure | Trigger | Repair Target |
|---------|---------|---------------|
| `shadow_contract_detected` | Surface defines fields not in canonical source | Delete shadow, wire to source |
| `projection_drift` | Surface has stale copy of source | Re-derive from source |
| `injection_gap` | Source exists but isn't injected to a surface | Fix injection path |
| `cross_surface_inconsistency` | Two surfaces disagree on same contract | Find which is stale, re-derive |
| `missing_canonical_source` | Contract has no declared source | Declare source, migrate copies |

## Relationship to Existing Systems

- **SST principle** (`.claude/rules/single-source-of-truth.md`): This pack operationalizes SST
- **Replay gate** (`test_replay_gate.py`): First instance of compile-time projection verification
- **Contract consistency** (`test_contract_consistency.py`): First instance of cross-surface audit
- **Bundle compiler** (`bundle_compiler.py`): The canonical compile path for schema -> surfaces

## Implementation Priority

1. **P0**: TypeScript types + validators in jingu-policy-core (foundation)
2. **P1**: Python wiring in jingu-swebench (runtime enforcement)
3. **P2**: CI integration (shadow contract detection script)
4. **P3**: Prompt injection for implementation governance phase
