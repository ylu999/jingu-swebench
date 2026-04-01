import type { InstanceRunResult } from "./contracts.js"

/**
 * Strategy Design Principles (derived from ablation experiments 2026-03)
 *
 * Three layers:
 *   Layer A — Localization: hints to guide where the model looks (localizationPolicy)
 *   Layer B — Patching:     how to generate the fix (focusStyle, analysisDepth, maxPatchLines)
 *   Layer C — Verification: output-time constraints (verificationPolicy)
 *
 * Experimental findings (9 django instances, k=3 attempts):
 *   A+B+C: pass=55.6%, apply_fail=40%, parse_fail=14%
 *   A+B:   pass=55.6%, apply_fail=35.3%, parse_fail=11.8%  ← current default
 *   B+C:   pass=55.6%, apply_fail=25.0%, parse_fail=18.8%
 *   B:     pass=44.4%, apply_fail=42%,   parse_fail=12%
 *
 * Layer attribution:
 *   B (retry quality): provides net pass_rate lift vs baseline (+11pp at k=3)
 *   A (localization):  reduces apply_fail but introduces mild parse friction
 *   C (strict-observed-only): no net pass_rate benefit; increases parse_fail
 *
 * Design rule:
 *   Default strategies include only layers with net positive contribution.
 *   Guardrails (Layer C) must not enter the main path unless ablation confirms net gain.
 *   A layer that reduces parse success rate is a friction layer, not a safety layer.
 */

export type SearchStrategy = {
  id: string
  promptHints: {
    // Layer A — Localization: how to find the relevant code
    localizationPolicy?: "symbol-first" | "file-first" | "test-driven"

    // Layer B — Patching: how to generate the fix
    focusStyle: "minimal-fix" | "root-cause" | "defensive"
    analysisDepth?: "light" | "standard" | "deep"
    maxPatchLines?: number

    // Layer C — Verification: what constraints to apply at output time
    verificationPolicy?: "strict-observed-only" | "feedback-grounded" | "standard"

    extraInstruction?: string
  }
}

export type StrategyRunResult = {
  strategyId: string
  verdict: "accepted" | "rejected" | "failed"
  runResult: InstanceRunResult
  score: number  // -Infinity if not accepted; else 1000 - files*50 - lines*2
}

// Baseline strategies — Layer B only (original set, no localization/verification hints)
export const STRATEGIES_BASELINE: SearchStrategy[] = [
  {
    id: "minimal",
    promptHints: { focusStyle: "minimal-fix", analysisDepth: "light", maxPatchLines: 15 },
  },
  {
    id: "root-cause",
    promptHints: { focusStyle: "root-cause", analysisDepth: "standard" },
  },
  {
    id: "defensive",
    promptHints: { focusStyle: "defensive", analysisDepth: "deep" },
  },
]

// Principle-tagged strategies — Layer A (localization) + Layer B (patching), no Layer C.
// Layer C (strict-observed-only) removed from main path after ablation (exp 2026-03):
//   - C had no net pass_rate gain
//   - C increased parse_fail_rate (especially on parse-sensitive instances like django-11049)
//   - A+B achieves apply_fail=35.3% vs baseline 42%, parse_fail=11.8% ≈ baseline 12%
export const STRATEGIES_PRINCIPLE: SearchStrategy[] = [
  {
    id: "p-minimal",
    promptHints: {
      localizationPolicy: "test-driven",
      focusStyle: "minimal-fix",
      analysisDepth: "light",
      maxPatchLines: 10,
      // Layer C intentionally absent: no net benefit, increases parse friction
    },
  },
  {
    id: "p-root-cause",
    promptHints: {
      localizationPolicy: "symbol-first",
      focusStyle: "root-cause",
      analysisDepth: "standard",
      // Layer C intentionally absent: no net benefit, increases parse friction
    },
  },
  {
    id: "p-defensive",
    promptHints: {
      localizationPolicy: "file-first",
      focusStyle: "defensive",
      analysisDepth: "deep",
      verificationPolicy: "standard",
    },
  },
]

// Ablation strategies — layer contribution analysis experiments
// ab-no-c: Layer A+B, no Layer C — this pattern is now the default (STRATEGIES_PRINCIPLE)
// ab-no-a: Layer B+C, no Layer A — tests whether localization hints are net positive
// strict-observed-only retained here for targeted experiments and special-case guardrails
export const STRATEGIES_ABLATION: SearchStrategy[] = [
  {
    id: "ab-no-c",
    promptHints: {
      localizationPolicy: "test-driven",
      focusStyle: "minimal-fix",
      analysisDepth: "light",
      maxPatchLines: 10,
      // no verificationPolicy = Layer C absent
    },
  },
  {
    id: "ab-no-a",
    promptHints: {
      // no localizationPolicy = Layer A absent
      focusStyle: "minimal-fix",
      analysisDepth: "light",
      maxPatchLines: 10,
      verificationPolicy: "strict-observed-only",
    },
  },
]

// Default export: principle-tagged set (used in experiments)
export const STRATEGIES: SearchStrategy[] = STRATEGIES_PRINCIPLE

// V2 ablation strategies — test "feedback-grounded" vs "strict-observed-only"
// Key change: verificationPolicy = "feedback-grounded"
//   → no grounding constraint on first-shot prompt
//   → grounding constraint injected ONLY in retry feedback after apply/ungrounded fail
export const STRATEGIES_V2_ABLATION: SearchStrategy[] = [
  {
    id: "v2-minimal",
    promptHints: {
      localizationPolicy: "test-driven",
      focusStyle: "minimal-fix",
      analysisDepth: "light",
      maxPatchLines: 10,
      verificationPolicy: "feedback-grounded",
    },
  },
  {
    id: "v2-root-cause",
    promptHints: {
      localizationPolicy: "symbol-first",
      focusStyle: "root-cause",
      analysisDepth: "standard",
      verificationPolicy: "feedback-grounded",
    },
  },
]
