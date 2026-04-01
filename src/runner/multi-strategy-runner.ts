import type { BenchmarkInstance } from "../types/contracts.js"
import type { SearchStrategy, StrategyRunResult } from "../types/strategy.js"
import { runJingu } from "./jingu-runner.js"
import { scoreRun, pickBest } from "./scorer.js"
import { join } from "node:path"

export type MultiStrategyResult = {
  best: StrategyRunResult | null
  all: StrategyRunResult[]
}

export async function runMultiStrategy(
  instance: BenchmarkInstance,
  workspaceBase: string,
  strategies: SearchStrategy[],
  opts: { sequential?: boolean; maxAttempts?: number } = {}
): Promise<MultiStrategyResult> {
  const instanceSlug = instance.instanceId.replace(/\//g, "__")

  async function runOne(strategy: SearchStrategy): Promise<StrategyRunResult> {
    const wsDir = join(workspaceBase, instanceSlug, strategy.id)
    const runResult = await runJingu(instance, workspaceBase, { wsDir, strategy, maxAttempts: opts.maxAttempts })
    const verdict = runResult.accepted ? "accepted" : "rejected"
    const result: StrategyRunResult = { strategyId: strategy.id, verdict, runResult, score: 0 }
    result.score = scoreRun(result)
    return result
  }

  let all: StrategyRunResult[]

  if (opts.sequential) {
    all = []
    for (const strategy of strategies) {
      all.push(await runOne(strategy))
    }
  } else {
    all = await Promise.all(strategies.map(runOne))
  }

  const best = pickBest(all)
  return { best, all }
}
