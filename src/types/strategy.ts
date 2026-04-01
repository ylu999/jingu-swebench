import type { InstanceRunResult } from "./contracts.js"

export type SearchStrategy = {
  id: string
  promptHints: {
    focusStyle: "minimal-fix" | "root-cause" | "defensive"
    analysisDepth?: "light" | "standard" | "deep"
    maxPatchLines?: number
    extraInstruction?: string
  }
}

export type StrategyRunResult = {
  strategyId: string
  verdict: "accepted" | "rejected" | "failed"
  runResult: InstanceRunResult
  score: number  // -Infinity if not accepted; else 1000 - files*50 - lines*2
}

export const STRATEGIES: SearchStrategy[] = [
  {
    id: "minimal",
    promptHints: {
      focusStyle: "minimal-fix",
      analysisDepth: "light",
      maxPatchLines: 15,
    },
  },
  {
    id: "root-cause",
    promptHints: {
      focusStyle: "root-cause",
      analysisDepth: "standard",
    },
  },
  {
    id: "defensive",
    promptHints: {
      focusStyle: "defensive",
      analysisDepth: "deep",
    },
  },
]
