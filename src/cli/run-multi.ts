import { loadInstances } from "../dataset/swebench-loader.js"
import { runMultiStrategy } from "../runner/multi-strategy-runner.js"
import { STRATEGIES } from "../types/strategy.js"
import { writePrediction } from "../output/predictions-writer.js"
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
const instanceIdsIdx = args.indexOf("--instance-ids")
const instanceIds: string[] =
  instanceIdsIdx >= 0 ? args.slice(instanceIdsIdx + 1).filter((a) => !a.startsWith("--")) : []

async function main() {
  console.log(`\njingu-swebench multi-strategy — dataset=${dataset} n=${n} strategies=${STRATEGIES.map((s) => s.id).join(",")}\n`)

  let instances = await loadInstances({ dataset, n, noCache: false })
  if (instanceIds.length > 0) {
    instances = instances.filter((i) => instanceIds.includes(i.instanceId))
    console.log(`Filtered to ${instances.length} instance(s): ${instances.map((i) => i.instanceId).join(", ")}\n`)
  }

  const outDir = join("results", "multi")
  mkdirSync(outDir, { recursive: true })
  const predictionsPath = join(outDir, "predictions.jsonl")
  const wsBase = join("workspaces")

  type Row = {
    instanceId: string
    best: string | null
    bestScore: number
    accepted: number
    strategies: Record<string, { verdict: string; score: number; patchLines: number }>
  }
  const rows: Row[] = []

  for (const instance of instances) {
    console.log(`\n=== ${instance.instanceId} ===`)
    const { best, all } = await runMultiStrategy(instance, wsBase, STRATEGIES, { sequential })

    const strategyStats: Record<string, { verdict: string; score: number; patchLines: number }> = {}
    for (const r of all) {
      const patchLines = r.runResult.finalPatchText
        ? r.runResult.finalPatchText.split("\n").filter((l) => (l.startsWith("+") || l.startsWith("-")) && !l.startsWith("+++") && !l.startsWith("---")).length
        : 0
      strategyStats[r.strategyId] = { verdict: r.verdict, score: r.score, patchLines }
    }

    const acceptedCount = all.filter((r) => r.verdict === "accepted").length
    const row: Row = {
      instanceId: instance.instanceId,
      best: best?.strategyId ?? null,
      bestScore: best?.score ?? -Infinity,
      accepted: acceptedCount,
      strategies: strategyStats,
    }
    rows.push(row)

    if (best?.runResult.finalPatchText) {
      writePrediction(predictionsPath, instance.instanceId, best.runResult.finalPatchText, "multi")
    }

    // Per-instance summary
    console.log(`  accepted=${acceptedCount}/${STRATEGIES.length} best=${best?.strategyId ?? "none"}(score=${best?.score ?? "n/a"})`)
    for (const [id, s] of Object.entries(strategyStats)) {
      console.log(`    ${id}: ${s.verdict} patchLines=${s.patchLines} score=${s.score}`)
    }
  }

  // Overall summary
  const totalAccepted = rows.filter((r) => r.accepted > 0).length
  console.log(`\n=== Multi-Strategy Summary ===`)
  console.log(`Instances: ${rows.length} | Instances with ≥1 accepted: ${totalAccepted}/${rows.length}`)
  console.log(``)
  console.log(`instance_id                           best_strategy   score   accepted/total`)
  for (const r of rows) {
    const pad = (s: string, n: number) => s.padEnd(n)
    console.log(
      `${pad(r.instanceId, 38)} ${pad(r.best ?? "none", 15)} ${String(r.bestScore === -Infinity ? "n/a" : r.bestScore).padEnd(7)} ${r.accepted}/${STRATEGIES.length}`
    )
  }

  writeFileSync(join(outDir, "summary.json"), JSON.stringify({ rows, totalInstances: rows.length, instancesWithAccepted: totalAccepted }, null, 2))
  console.log(`\nPredictions: ${predictionsPath}`)
  console.log(`Summary JSON: ${join(outDir, "summary.json")}`)
}

main().catch((err) => {
  console.error("Fatal:", err)
  process.exit(1)
})
