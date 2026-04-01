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
 *   A+B+C (strict): pass=55.6%, apply_fail=88.9%, parse_fail=29.6%
 *   A+B (no C):     pass=55.6%, apply_fail=81.5%, parse_fail=29.6%
 *   B+C (no A):     pass=55.6%, apply_fail=25.0%, parse_fail=18.8%
 *   B only:         pass=44.4%, apply_fail=92.6%, parse_fail=25.9%
 *   A+B + conf:     pass=55.6%, apply_fail=74.1%, parse_fail=29.6%  ← current default
 *   (per-strategy averages differ; instance-level numbers above)
 *
 * Layer attribution:
 *   B (retry quality):      net pass_rate lift vs baseline (+11pp at k=3); retry recovers
 *                           hard instances (e.g. 11133 rescued via p-root-cause retry)
 *   A (localization):       reduces apply_fail (-18.5pp from baseline with confidence gate)
 *                           but requires confidence gate — blind A hint adds parse friction
 *   C (strict-observed-only): no net pass_rate benefit; increases parse_fail on sensitive
 *                           instances (e.g. 11049); demoted to optional guardrail only
 *
 * Layer A confidence gate (computeLocalizationConfidence in strategy-resolver.ts):
 *   tier=off    (score<20):  no files → remove localizationPolicy entirely
 *   tier=weak   (score<50):  sparse evidence → downgrade to "file-first" (soft hint)
 *   tier=strong (score≥50):  adequate grounding → keep full localizationPolicy
 *   Signals: file count (0-30pt) + total injected lines (0-40pt) + anchor count (0-30pt)
 *
 * Design rules:
 *   1. Default strategies include only layers with net positive contribution.
 *   2. Guardrails (Layer C) must not enter the main path unless ablation confirms net gain.
 *   3. A layer that reduces parse success rate is a friction layer, not a safety layer.
 *   4. Layer A must be gated by evidence quality — "files exist" is not sufficient confidence.
 *      Assertive localization hints with weak evidence become noise, not signal.
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
