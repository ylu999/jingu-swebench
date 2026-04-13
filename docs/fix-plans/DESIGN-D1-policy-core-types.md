# DESIGN-D1: jingu-policy-core TypeScript Types

Date: 2026-04-12
Source: design-policy-core agent analysis

---

## Module Structure

Path: `jingu-policy-core/src/implementation-governance/`

| # | File | Purpose |
|---|------|---------|
| 1 | `types.ts` | Core types: ImplementationSurface, ProjectionChain, CanonicalSource, ShadowContract |
| 2 | `evidence.ts` | Evidence types: CanonicalSourceRef, ProjectionDerivationRef, etc. |
| 3 | `cognition.ts` | Cognition subtypes + principals as const objects |
| 4 | `policies.ts` | Policy definitions (6 typed policy objects) |
| 5 | `failures.ts` | Failure taxonomy (5 types) + repair routing |
| 6 | `validators.ts` | Check functions returning InvariantCheckResult |
| 7 | `index.ts` | Public API re-exports |
| 8 | `tests/principles/implementation-governance.test.ts` | Tests using node:test |

## InvariantCodes (add to codes.ts)

```
IG_MISSING_CANONICAL_SOURCE       — contract used by multiple surfaces with no declared source
IG_SHADOW_CONTRACT_DETECTED       — surface defines fields beyond canonical source
IG_PROJECTION_CHAIN_BROKEN        — projection chain from source to surface unverified
IG_CROSS_SURFACE_INCONSISTENCY    — two surfaces for same contract disagree on fields
IG_HARDCODED_SURFACE              — surface uses hardcoded derivation instead of compiled/accessor
IG_INJECTION_GAP                  — canonical source exists but has no projection chains
```

## Policy -> Check Function -> InvariantCode Mapping

| Policy | Check Function | Code |
|--------|---------------|------|
| canonical_source_required | checkCanonicalSourceExists | IG_MISSING_CANONICAL_SOURCE |
| projection_files_cannot_define_contracts | checkNoShadowContract | IG_SHADOW_CONTRACT_DETECTED |
| injection_path_must_be_traceable | checkProjectionChainIntegrity | IG_PROJECTION_CHAIN_BROKEN |
| cross_surface_delta_zero | checkCrossSurfaceConsistency | IG_CROSS_SURFACE_INCONSISTENCY |
| shadow_contract_ci_detection | checkNoHardcodedSurfaces | IG_HARDCODED_SURFACE |
| compile_time_projection_verification | checkAllProjectionChainsVerified | IG_INJECTION_GAP |

## Key Type Definitions

### CanonicalSource
```typescript
type CanonicalSource = {
  contract_id: string        // e.g. "declaration_protocol_schema"
  location: CodeLocation     // file + optional line range
  description: string
  defined_fields: string[]
}
```

### ImplementationSurface
```typescript
type ImplementationSurface = {
  surface_id: string
  surface_type: "tool_description" | "phase_prompt" | "gate_rule" | "bundle_field" | "test_fixture" | "documentation"
  location: CodeLocation
  contract_id: string
  actual_fields: string[]
  derivation_method: "compiled" | "accessor" | "hardcoded"
}
```

### ProjectionChain
```typescript
type ProjectionChain = {
  contract_id: string
  source: CanonicalSource
  target: ImplementationSurface
  intermediate_steps: CodeLocation[]
  verified: boolean
}
```

### ShadowContract
```typescript
type ShadowContract = {
  surface: ImplementationSurface
  extra_fields: string[]      // fields in surface NOT in canonical source
  missing_fields: string[]    // fields in source NOT in surface
  divergent_fields: string[]  // present in both but different
}
```

## Cognition Constants
```typescript
const IMPLEMENTATION_GOVERNANCE_SUBTYPES = {
  SOURCE_OF_TRUTH_IDENTIFICATION: "implementation.source_of_truth_identification",
  INJECTION_PATH_AUDIT: "implementation.injection_path_audit",
  PROJECTION_DERIVATION: "implementation.projection_derivation",
  CROSS_SURFACE_CONSISTENCY: "implementation.cross_surface_consistency",
  SHADOW_CONTRACT_DETECTION: "implementation.shadow_contract_detection",
} as const

const IMPLEMENTATION_GOVERNANCE_PRINCIPALS = {
  SINGLE_SOURCE_PRESERVATION: "single_source_of_truth_preservation",
  PROJECTION_NOT_DEFINITION: "projection_not_definition",
  INJECTION_PATH_ACCOUNTABILITY: "injection_path_accountability",
  CROSS_SURFACE_CONSISTENCY: "cross_surface_consistency",
  NO_SHADOW_CONTRACTS: "no_shadow_contracts",
  COMPILE_OVER_HANDWRITE: "compile_over_handwrite",
} as const
```

## Combined Check Function
```typescript
function checkAllImplementationGovernance(input: {
  sources: CanonicalSource[]
  surfaces: ImplementationSurface[]
  chains: ProjectionChain[]
  shadows: ShadowContract[]
}): InvariantCheckResult
```

## Dependencies & Sequencing

1. Add 6 IG_* codes to `src/invariants/codes.ts`
2. Create types.ts (no internal imports)
3. Create evidence.ts (imports types.ts)
4. Create cognition.ts (standalone)
5. Create policies.ts (standalone)
6. Create failures.ts (standalone + one function)
7. Create validators.ts (imports types + invariants)
8. Create index.ts (re-exports)
9. Add export to `src/index.ts`
10. Create tests, run build + test

Steps 2-6 are independent (parallel). Step 7 depends on 1+2. Steps 8-9 depend on all.

## Severity Calibration

- **error**: shadow contracts, missing canonical source, projection chain broken, cross-surface inconsistency
- **warning**: hardcoded surfaces, injection gaps

## Test Plan (7 test groups)

1. checkCanonicalSourceExists — pass/fail for source existence
2. checkNoShadowContract — pass/fail for extra fields
3. checkProjectionChainIntegrity — pass/fail for chain verification
4. checkCrossSurfaceConsistency — pass/fail for field agreement
5. checkNoHardcodedSurfaces — pass/warning for derivation method
6. checkAllProjectionChainsVerified — pass/warning for chain coverage
7. checkAllImplementationGovernance — combined pass/fail
