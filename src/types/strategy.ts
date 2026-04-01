import type { InstanceRunResult } from "./contracts.js"

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
    verificationPolicy?: "strict-observed-only" | "standard"

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

// Principle-tagged strategies — all 3 layers explicit
export const STRATEGIES_PRINCIPLE: SearchStrategy[] = [
  {
    id: "p-minimal",
    promptHints: {
      localizationPolicy: "test-driven",
      focusStyle: "minimal-fix",
      analysisDepth: "light",
      maxPatchLines: 10,
      verificationPolicy: "strict-observed-only",
    },
  },
  {
    id: "p-root-cause",
    promptHints: {
      localizationPolicy: "symbol-first",
      focusStyle: "root-cause",
      analysisDepth: "standard",
      verificationPolicy: "strict-observed-only",
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

// Default export: principle-tagged set (used in experiments)
export const STRATEGIES: SearchStrategy[] = STRATEGIES_PRINCIPLE
