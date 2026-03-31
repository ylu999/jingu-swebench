import { loadInstances } from "../dataset/swebench-loader.js"
import { runRaw } from "../runner/raw-runner.js"
import { runJingu } from "../runner/jingu-runner.js"
import { runCompare, printCompareSummary } from "../runner/compare-runner.js"
import { writePrediction } from "../output/predictions-writer.js"
import { buildCompareReport, buildSummaryTable, buildFailureTable, writeReport } from "../output/report-writer.js"
import { writeRunEvents } from "../output/eventlog-writer.js"
import { join } from "node:path"
import { mkdirSync, writeFileSync } from "node:fs"
import { randomUUID } from "node:crypto"

// Parse CLI args
const args = process.argv.slice(2)

function getArg(flag: string): string | undefined {
  const i = args.indexOf(flag)
  return i >= 0 ? args[i + 1] : undefined
}

const mode = getArg("--mode") ?? "raw"
const dataset = (getArg("--dataset") ?? "lite") as "lite" | "verified"
const n = getArg("--n") ? parseInt(getArg("--n")!, 10) : 1
const noCache = args.includes("--no-cache")
const skipTestGate = args.includes("--skip-test-gate")

if (!["raw", "jingu", "compare"].includes(mode)) {
  console.error(`Unknown mode: ${mode}. Valid: raw, jingu, compare`)
  process.exit(1)
}

async function main() {
  console.log(`\njingu-swebench — mode=${mode} dataset=${dataset} n=${n}\n`)

  const instances = await loadInstances({ dataset, n, noCache })
  const outDir = join("results", mode)
  mkdirSync(outDir, { recursive: true })
  const predictionsPath = join(outDir, "predictions.jsonl")
  const eventlogPath = join(outDir, "events.jsonl")
  const wsBase = join("workspaces")
  const runId = randomUUID()

  if (mode === "compare") {
    const results = await runCompare(instances, wsBase, { skipTestGate })
    printCompareSummary(results)

    for (const r of results) {
      if (r.raw.finalPatchText) {
        writePrediction(join("results", "raw", "predictions.jsonl"), r.instanceId, r.raw.finalPatchText, "raw")
      }
      if (r.jingu.finalPatchText) {
        writePrediction(join("results", "jingu", "predictions.jsonl"), r.instanceId, r.jingu.finalPatchText, "jingu")
      }
      writeRunEvents(join("results", "raw", "events.jsonl"), runId, r.raw)
      writeRunEvents(join("results", "jingu", "events.jsonl"), runId, r.jingu)
    }

    writeFileSync(join(outDir, "compare.json"), JSON.stringify(results, null, 2))
    writeReport(join(outDir, "report.md"), buildCompareReport(results))
    return
  }

  const results = []

  for (const instance of instances) {
    const result = mode === "raw"
      ? await runRaw(instance)
      : await runJingu(instance, wsBase, { skipTestGate })

    results.push(result)

    if (result.finalPatchText) {
      writePrediction(predictionsPath, instance.instanceId, result.finalPatchText, mode)
    }
    writeRunEvents(eventlogPath, runId, result)
  }

  const accepted = results.filter((r) => r.accepted).length
  console.log(`\n--- Summary ---`)
  console.log(`Mode: ${mode} | Instances: ${results.length} | Accepted: ${accepted}/${results.length}`)

  writeReport(join(outDir, "report.md"),
    buildSummaryTable(results) + "\n\n" + buildFailureTable(results))
  writeFileSync(join(outDir, "summary.json"),
    JSON.stringify({ mode, dataset, n: results.length, accepted }, null, 2))
  console.log(`Predictions: ${predictionsPath} | Events: ${eventlogPath}`)
}

main().catch((err) => {
  console.error("Fatal:", err)
  process.exit(1)
})
