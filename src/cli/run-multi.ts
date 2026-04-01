import { loadInstances } from "../dataset/swebench-loader.js"
import { runMultiStrategy } from "../runner/multi-strategy-runner.js"
import { STRATEGIES } from "../types/strategy.js"
import { writePrediction } from "../output/predictions-writer.js"
import type { BenchmarkInstance } from "../types/contracts.js"
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
const instanceIdsIdx = args.indexOf("--instance-ids")
const instanceIds: string[] =
  instanceIdsIdx >= 0 ? args.slice(instanceIdsIdx + 1).filter((a) => !a.startsWith("--")) : []

// Monotonic timer relative to process start
const T0 = Date.now()
function ts(): string {
  return `+${((Date.now() - T0) / 1000).toFixed(1)}s`
}

type Row = {
  instanceId: string
  best: string | null
  bestScore: number
  accepted: number
  durationMs: number
  strategies: Record<string, { verdict: string; score: number; patchLines: number; durationMs: number }>
}

async function runInstance(instance: BenchmarkInstance, wsBase: string, predictionsPath: string): Promise<Row> {
  const t0 = Date.now()
  console.log(`\n[${ts()}] === ${instance.instanceId} ===`)

  const { best, all } = await runMultiStrategy(instance, wsBase, STRATEGIES, { sequential })

  const strategyStats: Record<string, { verdict: string; score: number; patchLines: number; durationMs: number }> = {}
  for (const r of all) {
    const patchLines = r.runResult.finalPatchText
      ? r.runResult.finalPatchText.split("\n").filter((l) => (l.startsWith("+") || l.startsWith("-")) && !l.startsWith("+++") && !l.startsWith("---")).length
      : 0
    strategyStats[r.strategyId] = {
      verdict: r.verdict,
      score: r.score,
      patchLines,
      durationMs: r.runResult.durationMs,
    }
  }

  const acceptedCount = all.filter((r) => r.verdict === "accepted").length
  const instanceDuration = Date.now() - t0

  console.log(`[${ts()}]   done in ${(instanceDuration / 1000).toFixed(1)}s — accepted=${acceptedCount}/${STRATEGIES.length} best=${best?.strategyId ?? "none"}`)
  for (const [id, s] of Object.entries(strategyStats)) {
    console.log(`[${ts()}]     ${id}: ${s.verdict} patchLines=${s.patchLines} score=${s.score} (${(s.durationMs / 1000).toFixed(1)}s)`)
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
  console.log(`\n[${ts()}] jingu-swebench multi-strategy — dataset=${dataset} n=${n} strategies=${STRATEGIES.map((s) => s.id).join(",")} parallel-instances=${parallelInstances}\n`)

  let instances = await loadInstances({ dataset, n, noCache: false })
  if (instanceIds.length > 0) {
    instances = instances.filter((i) => instanceIds.includes(i.instanceId))
    console.log(`[${ts()}] Filtered to ${instances.length} instance(s): ${instances.map((i) => i.instanceId).join(", ")}\n`)
  }

  const outDir = join("results", "multi")
  mkdirSync(outDir, { recursive: true })
  const predictionsPath = join(outDir, "predictions.jsonl")
  const wsBase = join("workspaces")

  let rows: Row[]
  if (parallelInstances) {
    console.log(`[${ts()}] Running all ${instances.length} instances in parallel`)
    rows = await Promise.all(instances.map((inst) => runInstance(inst, wsBase, predictionsPath)))
  } else {
    rows = []
    for (const instance of instances) {
      rows.push(await runInstance(instance, wsBase, predictionsPath))
    }
  }

  const totalAccepted = rows.filter((r) => r.accepted > 0).length
  const totalDuration = Date.now() - T0

  console.log(`\n[${ts()}] === Multi-Strategy Summary (total=${(totalDuration / 1000).toFixed(1)}s) ===`)
  console.log(`Instances: ${rows.length} | With ≥1 accepted: ${totalAccepted}/${rows.length}\n`)
  console.log(`${"instance_id".padEnd(38)} ${"best".padEnd(12)} ${"score".padEnd(7)} ${"acc/tot".padEnd(8)} time`)
  for (const r of rows) {
    console.log(
      `${r.instanceId.padEnd(38)} ${(r.best ?? "none").padEnd(12)} ${String(r.bestScore === -Infinity ? "n/a" : r.bestScore).padEnd(7)} ${String(r.accepted + "/" + STRATEGIES.length).padEnd(8)} ${(r.durationMs / 1000).toFixed(1)}s`
    )
  }

  writeFileSync(
    join(outDir, "summary.json"),
    JSON.stringify({ rows, totalInstances: rows.length, instancesWithAccepted: totalAccepted, totalDurationMs: totalDuration }, null, 2)
  )
  console.log(`\n[${ts()}] Predictions: ${predictionsPath} | Summary: ${join(outDir, "summary.json")}`)
}

main().catch((err) => {
  console.error("Fatal:", err)
  process.exit(1)
})
