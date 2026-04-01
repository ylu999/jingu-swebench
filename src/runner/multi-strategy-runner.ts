import type { BenchmarkInstance } from "../types/contracts.js"
import type { SearchStrategy, StrategyRunResult } from "../types/strategy.js"
import { runJingu } from "./jingu-runner.js"
import { scoreRun, pickBest } from "./scorer.js"
import { runTestsBaseline } from "../admission/test-gate.js"
import { Workspace } from "../workspace/workspace.js"
import { join } from "node:path"
import { existsSync, readFileSync, writeFileSync } from "node:fs"

export type MultiStrategyResult = {
  best: StrategyRunResult | null
  all: StrategyRunResult[]
}

const TEST_CMD = "python -m pytest -x -q --tb=short 2>&1 || true"

export async function runMultiStrategy(
  instance: BenchmarkInstance,
  workspaceBase: string,
  strategies: SearchStrategy[],
  opts: { sequential?: boolean; maxAttempts?: number; skipBaselineTest?: boolean } = {}
): Promise<MultiStrategyResult> {
  const instanceSlug = instance.instanceId.replace(/\//g, "__")

  // Run baseline test once per instance, persist to cache — skip on subsequent runs
  const cacheDir = join(workspaceBase, "__cache__", instanceSlug)
  const baselineCacheFile = join(workspaceBase, "__cache__", `${instanceSlug}.baseline.json`)
  let sharedBaseline: { passed: number; failed: number; errors: number } | undefined
  if (opts.skipBaselineTest) {
    sharedBaseline = { passed: 0, failed: 0, errors: 0 }
    console.log(`  [jingu] baseline (skipped)`)
  } else if (existsSync(baselineCacheFile)) {
    const cached = JSON.parse(readFileSync(baselineCacheFile, "utf8"))
    if (cached.passed === -1 || (cached.passed === 0 && cached.failed === 0 && cached.errors === 0)) {
      console.log(`  [jingu] baseline (cached INVALID — removing stale cache)`)
      // Remove the invalid cache so we retry next time
      try { require("node:fs").unlinkSync(baselineCacheFile) } catch {}
    } else {
      sharedBaseline = cached
      console.log(`  [jingu] baseline (cached): passed=${sharedBaseline!.passed} failed=${sharedBaseline!.failed}`)
    }
  } else if (existsSync(join(cacheDir, ".git"))) {
    const cacheWs = new Workspace(cacheDir)
    const computed = runTestsBaseline(cacheWs, TEST_CMD)
    if (computed.passed === -1) {
      // Invalid harness: 0/0 result means test env is broken — do not cache, do not use
      console.log(`  [jingu] baseline (INVALID HARNESS — no tests ran, skipping)`)
      sharedBaseline = undefined
    } else {
      sharedBaseline = computed
      writeFileSync(baselineCacheFile, JSON.stringify(sharedBaseline), "utf8")
      console.log(`  [jingu] baseline (computed+cached): passed=${sharedBaseline.passed} failed=${sharedBaseline.failed}`)
    }
  }

  async function runOne(strategy: SearchStrategy): Promise<StrategyRunResult> {
    const wsDir = join(workspaceBase, instanceSlug, strategy.id)
    const runResult = await runJingu(instance, workspaceBase, { wsDir, strategy, maxAttempts: opts.maxAttempts, baseline: sharedBaseline })
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
