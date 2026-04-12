# Bundle Schema v2 Specification

Goal: Put all behavior-affecting hardcoded values into a compilable, auditable, observable onboard contract.

From:
```text
code contains behavior
```

To:
```text
bundle declares behavior
runtime enforces behavior
events expose behavior
```

---

## Top-Level Structure

```json
{
  "schema_version": "2.0",
  "bundle_id": "jingu-swebench-default",
  "bundle_version": "2026-04-11",
  "strict_mode": true,

  "runtime": {},
  "phases": {},
  "principals": {},
  "types": {},
  "gates": {},
  "verify": {},
  "limits": {},
  "logging": {},
  "fallback_policy": {}
}
```

---

## `runtime`

Runtime master switches. Don't scatter these in code.

```json
{
  "runtime": {
    "degraded_mode_allowed": false,
    "abort_on_bundle_failure": true,
    "emit_bundle_load_events": true,
    "include_degraded_banner_in_prompt": true
  }
}
```

This directly solves the audit's core problem:
* bundle compile failure cannot silent fallback
* benchmark mode should hard fail
* degraded_mode must explicitly enter runtime state

---

## `phases`

Phase onboarding — not just prompt text, but phase contracts.

```json
{
  "phases": {
    "OBSERVE": {
      "enabled": true,
      "prompt_sections": ["objective", "evidence_discipline", "forbidden_actions"],
      "required_fields": ["phase", "claims", "evidence_refs"],
      "forbidden_downstream_actions": ["code_patch", "submit_patch"]
    },
    "ANALYZE": {
      "enabled": true,
      "prompt_sections": ["objective", "root_cause", "alternative_hypothesis"],
      "required_fields": ["phase", "subtype", "principals", "claims", "from_steps"],
      "forbidden_downstream_actions": ["code_patch", "submit_patch"]
    },
    "DESIGN": {
      "enabled": true,
      "prompt_sections": ["solution_shape", "invariants", "tradeoffs"],
      "required_fields": ["phase", "subtype", "principals", "claims", "risks"]
    },
    "EXECUTE": {
      "enabled": true,
      "prompt_sections": ["mutation_rules", "allowed_actions", "verification_expectations"],
      "required_fields": ["phase", "subtype", "principals", "action_refs", "from_steps"]
    }
  }
}
```

This is not just a prompt loader — it's a **phase contract loader**.
Otherwise we fall back to "prompt injected, but system doesn't enforce".

---

## `principals`

Principal registry as canonical source. No more scattered hardcode.

```json
{
  "principals": {
    "causal_grounding": {
      "description": "Claims must be causally linked to observed evidence.",
      "applies_to_phases": ["ANALYZE"],
      "validation_mode": "structured"
    },
    "alternative_hypothesis_check": {
      "description": "At least one plausible alternative explanation must be considered.",
      "applies_to_phases": ["ANALYZE"],
      "validation_mode": "structured"
    },
    "minimal_change": {
      "description": "Patch should minimize unrelated mutation.",
      "applies_to_phases": ["EXECUTE"],
      "validation_mode": "hybrid"
    },
    "strictness_preservation": {
      "description": "Do not weaken validation constraints unless explicitly justified.",
      "applies_to_phases": ["DESIGN", "EXECUTE"],
      "validation_mode": "semantic_check"
    },
    "evidence_linkage": {
      "description": "Every claim must link to specific evidence references.",
      "applies_to_phases": ["ANALYZE", "DESIGN"],
      "validation_mode": "structured"
    },
    "ontology_alignment": {
      "description": "Declared phase/subtype must match actual behavior.",
      "applies_to_phases": ["ANALYZE", "DESIGN", "EXECUTE"],
      "validation_mode": "structured"
    },
    "phase_boundary_discipline": {
      "description": "Actions must stay within current phase boundary.",
      "applies_to_phases": ["ANALYZE", "DESIGN", "EXECUTE"],
      "validation_mode": "structured"
    },
    "action_grounding": {
      "description": "Code changes must be grounded in analysis/design evidence.",
      "applies_to_phases": ["EXECUTE"],
      "validation_mode": "structured"
    },
    "option_comparison": {
      "description": "Multiple approaches must be compared with tradeoffs.",
      "applies_to_phases": ["DESIGN"],
      "validation_mode": "structured"
    },
    "constraint_satisfaction": {
      "description": "Solution must satisfy all identified constraints.",
      "applies_to_phases": ["DESIGN", "EXECUTE"],
      "validation_mode": "structured"
    },
    "result_verification": {
      "description": "Results must be verified against expected outcomes.",
      "applies_to_phases": ["EXECUTE"],
      "validation_mode": "structured"
    },
    "uncertainty_honesty": {
      "description": "Uncertain conclusions must be explicitly marked.",
      "applies_to_phases": ["ANALYZE"],
      "validation_mode": "structured"
    }
  }
}
```

---

## `types`

Phase/type/subtype/principal mapping — the single source of truth.

```json
{
  "types": {
    "analysis.root_cause": {
      "phase": "ANALYZE",
      "required_principals": [
        "causal_grounding",
        "alternative_hypothesis_check",
        "evidence_linkage"
      ],
      "forbidden_principals": [
        "minimal_change"
      ],
      "required_upstream_phases": ["OBSERVE"]
    },
    "design.validation_change": {
      "phase": "DESIGN",
      "required_principals": [
        "option_comparison",
        "constraint_satisfaction",
        "strictness_preservation"
      ],
      "required_upstream_phases": ["ANALYZE"]
    },
    "execution.code_patch": {
      "phase": "EXECUTE",
      "required_principals": [
        "minimal_change",
        "action_grounding"
      ],
      "required_upstream_phases": ["DESIGN"]
    }
  }
}
```

This is the core of onboarding: runtime compiles legal contracts from the bundle, not "remember to mention principals in the prompt".

---

## `gates`

Gate strategies and thresholds — eliminate magic numbers in code.

```json
{
  "gates": {
    "analysis_gate": {
      "enabled": true,
      "max_rejects_before_force_pass": 0,
      "threshold": 0.7,
      "required_checks": [
        "code_grounding",
        "causal_chain",
        "alternative_hypothesis"
      ],
      "emit_force_pass_event": true
    },
    "design_gate": {
      "enabled": true,
      "max_rejects_before_force_pass": 0,
      "threshold": 0.7,
      "required_checks": [
        "invariant_preservation",
        "comparison",
        "completeness"
      ],
      "emit_force_pass_event": true
    },
    "principal_gate": {
      "enabled": true,
      "reject_on_missing_required_principal": true,
      "reject_on_declared_but_unsubstantiated_principal": true
    }
  }
}
```

**Strong recommendation:** `max_rejects_before_force_pass` defaults to 0 (disabled). The audit proved force-pass destroys governance credibility. If needed, must be explicitly configured with justification.

---

## `verify`

Controlled verify contract. Replaces all hardcoded verify behavior.

```json
{
  "verify": {
    "enabled": true,
    "strategy": "batched_targeted",
    "require_signal": true,
    "allow_partial_signal": true,

    "scope_selection": {
      "max_class_fraction": 0.3,
      "max_classes_hard_cap": 40,
      "min_classes_floor": 5,
      "fallback_strategy": "shrink_batch"
    },

    "batching": {
      "enabled": true,
      "batch_size": 10,
      "max_batches": 4
    },

    "timeouts": {
      "per_batch_seconds": 20,
      "overall_seconds": 90,
      "docker_subprocess_seconds": 30
    },

    "no_signal_policy": {
      "treat_as_error": true,
      "emit_event": true,
      "retry_with_ultra_small_subset": true
    }
  }
}
```

This directly replaces audit items:
* C1: 20-class hard limit
* C2: fixed 60s timeout
* C5/C16: multiple duplicate timeout values
* No-signal handling when none existed before

---

## `limits`

All limits unified. No more scattered across step_sections, verify, adapter.

```json
{
  "limits": {
    "analysis_gate_max_rejects": 0,
    "design_gate_max_rejects": 0,
    "retryable_loop_limit": 2,
    "fake_loop_limit": 0,
    "execute_redirect_limit": 2,
    "no_signal_threshold": 5,
    "max_pytest_feedback_bytes": 8192,
    "reviewer_max_tokens": 2048,
    "small_patch_max_lines": 50
  }
}
```

Design principle — everything that satisfies this sentence goes into `limits`:

> **This value, once triggered, changes runtime behavior, gate verdict, retry routing, or output content.**

---

## `logging`

Critical — otherwise bundle is just "config file version of hardcode".

```json
{
  "logging": {
    "emit_limit_triggered_events": true,
    "emit_force_pass_events": true,
    "emit_verify_scope_events": true,
    "emit_bundle_events": true,
    "mirror_to_stdout": true,
    "mirror_to_decisions_jsonl": true
  }
}
```

---

## `fallback_policy`

Prevent silent degradation — the safety net.

```json
{
  "fallback_policy": {
    "bundle_failure": {
      "action": "abort"
    },
    "missing_gate_config": {
      "action": "abort"
    },
    "verify_scope_overflow": {
      "action": "shrink_batch"
    },
    "verify_no_signal": {
      "action": "retry_ultra_small_subset"
    }
  }
}
```
