import { loadInstances } from "../dataset/swebench-loader.js"
import { runMultiStrategy } from "../runner/multi-strategy-runner.js"
import { STRATEGIES, STRATEGIES_BASELINE, STRATEGIES_PRINCIPLE, STRATEGIES_ABLATION, STRATEGIES_V2_ABLATION } from "../types/strategy.js"
import { writePrediction } from "../output/predictions-writer.js"
import type { BenchmarkInstance, AttemptResult } from "../types/contracts.js"
import type { StrategyRunResult } from "../types/strategy.js"
import { join } from "node:path"
import { mkdirSync, writeFileSync } from "node:fs"

// CLI args
const args = process.argv.slice(2)
function getArg(flag: string): string | undefined {
  const i = args.indexOf(flag)
  return i >= 0 ? args[i + 1] : undefined
}

const dataset = (getArg("--dataset") ?? "lite") as "lite" | "verified"
const n = getArg("--n") ? parseInt(getArg("--n")!, 10) : 5
const sequential = args.includes("--sequential")
const parallelInstances = args.includes("--parallel-instances")
const maxAttempts = getArg("--max-attempts") ? parseInt(getArg("--max-attempts")!, 10) : undefined
const skipBaselineTest = args.includes("--skip-baseline-test")
const outDirOverride = getArg("--out-dir")
const strategySet = args.includes("--v2-ablation")
  ? STRATEGIES_V2_ABLATION
  : args.includes("--baseline")
  ? STRATEGIES_BASELINE
  : args.includes("--ablation")
    ? STRATEGIES_ABLATION
    : STRATEGIES_PRINCIPLE
const instanceIdsIdx = args.indexOf("--instance-ids")
const instanceIds: string[] =
  instanceIdsIdx >= 0 ? args.slice(instanceIdsIdx + 1).filter((a) => !a.startsWith("--")) : []

// Monotonic timer relative to process start
const T0 = Date.now()
function ts(): string {
  return `+${((Date.now() - T0) / 1000).toFixed(1)}s`
}

type StrategyMetrics = {
  verdict: string
  score: number
  patchLines: number
  durationMs: number
  applyFailCount: number
  parseFailCount: number
  resolution?: { status: "valid" | "degraded"; reason?: string }
}

type Row = {
  instanceId: string
  best: string | null
  bestScore: number
  accepted: number
  durationMs: number
  strategies: Record<string, StrategyMetrics>
}

// Compute per-strategy metrics from AttemptResult[]
function collectAttemptMetrics(attempts: AttemptResult[]): { applyFailCount: number; parseFailCount: number } {
  let applyFailCount = 0
  let parseFailCount = 0
  for (const a of attempts) {
    if (a.applyGate?.code === "PATCH_APPLY_FAILED") applyFailCount++
    if (a.structuralGate?.code === "PARSE_FAILED") parseFailCount++
  }
  return { applyFailCount, parseFailCount }
}

// Aggregate 5 experiment metrics across all strategy results for an instance
function collectMetrics(all: StrategyRunResult[]): {
  applyFailRate: number
  parseFailRate: number
  avgPatchLines: number
} {
  let totalAttempts = 0
  let applyFails = 0
  let parseFails = 0
  let totalPatchLines = 0
  let acceptedCount = 0

  for (const r of all) {
    const attempts = r.runResult.attempts
    totalAttempts += attempts.length
    for (const a of attempts) {
      if (a.applyGate?.code === "PATCH_APPLY_FAILED") applyFails++
      if (a.structuralGate?.code === "PARSE_FAILED") parseFails++
    }
    if (r.verdict === "accepted" && r.runResult.finalPatchText) {
      const lines = r.runResult.finalPatchText
        .split("\n")
        .filter((l) => (l.startsWith("+") || l.startsWith("-")) && !l.startsWith("+++") && !l.startsWith("---"))
        .length
      totalPatchLines += lines
      acceptedCount++
    }
  }

  return {
    applyFailRate: totalAttempts > 0 ? applyFails / totalAttempts : 0,
    parseFailRate: totalAttempts > 0 ? parseFails / totalAttempts : 0,
    avgPatchLines: acceptedCount > 0 ? totalPatchLines / acceptedCount : 0,
  }
}

async function runInstance(instance: BenchmarkInstance, wsBase: string, predictionsPath: string): Promise<Row> {
  const t0 = Date.now()
  console.log(`\n[${ts()}] === ${instance.instanceId} ===`)

  const { best, all } = await runMultiStrategy(instance, wsBase, strategySet, { sequential, maxAttempts, skipBaselineTest })

  const strategyStats: Record<string, StrategyMetrics> = {}
  for (const r of all) {
    const patchLines = r.runResult.finalPatchText
      ? r.runResult.finalPatchText.split("\n").filter((l) => (l.startsWith("+") || l.startsWith("-")) && !l.startsWith("+++") && !l.startsWith("---")).length
      : 0
    const { applyFailCount, parseFailCount } = collectAttemptMetrics(r.runResult.attempts)
    // Pick resolution from first attempt that has it (strategy-level, not per-attempt)
    const resolution = r.runResult.attempts.find((a) => a.strategyResolution)?.strategyResolution
    strategyStats[r.strategyId] = {
      verdict: r.verdict,
      score: r.score,
      patchLines,
      durationMs: r.runResult.durationMs,
      applyFailCount,
      parseFailCount,
      resolution,
    }
  }

  const acceptedCount = all.filter((r) => r.verdict === "accepted").length
  const instanceDuration = Date.now() - t0

  console.log(`[${ts()}]   done in ${(instanceDuration / 1000).toFixed(1)}s — accepted=${acceptedCount}/${strategySet.length} best=${best?.strategyId ?? "none"}`)
  for (const [id, s] of Object.entries(strategyStats)) {
    const resTag = s.resolution?.status === "degraded" ? ` [degraded:${s.resolution.reason}]` : ""
    console.log(`[${ts()}]     ${id}: ${s.verdict} patchLines=${s.patchLines} score=${s.score} applyFail=${s.applyFailCount} parseFail=${s.parseFailCount}${resTag} (${(s.durationMs / 1000).toFixed(1)}s)`)
  }

  if (best?.runResult.finalPatchText) {
    writePrediction(predictionsPath, instance.instanceId, best.runResult.finalPatchText, "multi")
  }

  return {
    instanceId: instance.instanceId,
    best: best?.strategyId ?? null,
    bestScore: best?.score ?? -Infinity,
    accepted: acceptedCount,
    durationMs: instanceDuration,
    strategies: strategyStats,
  }
}

async function main() {
  console.log(`\n[${ts()}] jingu-swebench multi-strategy — dataset=${dataset} n=${n} strategies=${strategySet.map((s) => s.id).join(",")} parallel-instances=${parallelInstances}\n`)

  let instances = await loadInstances({ dataset, n, noCache: false })
  if (instanceIds.length > 0) {
    instances = instances.filter((i) => instanceIds.includes(i.instanceId))
    console.log(`[${ts()}] Filtered to ${instances.length} instance(s): ${instances.map((i) => i.instanceId).join(", ")}\n`)
  }

  const outDir = outDirOverride ?? join("results", "multi")
  mkdirSync(outDir, { recursive: true })
  const predictionsPath = join(outDir, "predictions.jsonl")
  const wsBase = join("workspaces")

  async function runInstanceSafe(inst: BenchmarkInstance): Promise<Row | null> {
    try {
      return await runInstance(inst, wsBase, predictionsPath)
    } catch (err) {
      console.error(`[${ts()}] ERROR ${inst.instanceId}: ${err instanceof Error ? err.message : String(err)}`)
      return null
    }
  }

  let rows: Row[]
  if (parallelInstances) {
    console.log(`[${ts()}] Running all ${instances.length} instances in parallel`)
    const results = await Promise.all(instances.map(runInstanceSafe))
    rows = results.filter((r): r is Row => r !== null)
  } else {
    rows = []
    for (const instance of instances) {
      const r = await runInstanceSafe(instance)
      if (r) rows.push(r)
    }
  }

  const totalAccepted = rows.filter((r) => r.accepted > 0).length
  const totalDuration = Date.now() - T0

  // Aggregate 5 metrics across all rows
  const allStrategyResults = rows.flatMap((r) =>
    Object.entries(r.strategies).map(([id, s]) => ({
      strategyId: id,
      verdict: s.verdict,
      score: s.score,
      patchLines: s.patchLines,
      applyFailCount: s.applyFailCount,
      parseFailCount: s.parseFailCount,
    }))
  )
  const totalAttemptCounts = rows.flatMap((r) =>
    Object.values(r.strategies).map((s) => s.applyFailCount + s.parseFailCount + (s.verdict === "accepted" ? 1 : s.verdict === "rejected" ? 1 : 0))
  )
  const totals = {
    instances: rows.length,
    passRate: totalAccepted / rows.length,
    applyFailRate: allStrategyResults.reduce((s, r) => s + r.applyFailCount, 0) /
      Math.max(1, allStrategyResults.length),
    parseFailRate: allStrategyResults.reduce((s, r) => s + r.parseFailCount, 0) /
      Math.max(1, allStrategyResults.length),
    avgPatchLines: (() => {
      const accepted = allStrategyResults.filter((r) => r.verdict === "accepted" && r.patchLines > 0)
      return accepted.length > 0 ? accepted.reduce((s, r) => s + r.patchLines, 0) / accepted.length : 0
    })(),
  }

  console.log(`\n[${ts()}] === Multi-Strategy Summary (total=${(totalDuration / 1000).toFixed(1)}s) ===`)
  console.log(`Instances: ${rows.length} | With ≥1 accepted: ${totalAccepted}/${rows.length}`)
  console.log(`pass_rate=${(totals.passRate * 100).toFixed(1)}% apply_fail_rate=${(totals.applyFailRate * 100).toFixed(1)}% parse_fail_rate=${(totals.parseFailRate * 100).toFixed(1)}% avg_patch_lines=${totals.avgPatchLines.toFixed(1)}\n`)
  console.log(`${"instance_id".padEnd(38)} ${"best".padEnd(12)} ${"score".padEnd(7)} ${"acc/tot".padEnd(8)} time`)
  for (const r of rows) {
    console.log(
      `${r.instanceId.padEnd(38)} ${(r.best ?? "none").padEnd(12)} ${String(r.bestScore === -Infinity ? "n/a" : r.bestScore).padEnd(7)} ${String(r.accepted + "/" + strategySet.length).padEnd(8)} ${(r.durationMs / 1000).toFixed(1)}s`
    )
  }

  writeFileSync(
    join(outDir, "summary.json"),
    JSON.stringify(
      {
        strategySet: strategySet.map((s) => s.id),
        totalInstances: rows.length,
        instancesWithAccepted: totalAccepted,
        totalDurationMs: totalDuration,
        metrics: totals,
        rows,
      },
      null,
      2
    )
  )
  console.log(`\n[${ts()}] Predictions: ${predictionsPath} | Summary: ${join(outDir, "summary.json")}`)
}

main().catch((err) => {
  console.error("Fatal:", err)
  process.exit(1)
})
