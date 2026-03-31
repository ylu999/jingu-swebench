import { loadInstances } from "../dataset/swebench-loader.js"
import { runRaw } from "../runner/raw-runner.js"
import { runJingu } from "../runner/jingu-runner.js"
import { runCompare, printCompareSummary } from "../runner/compare-runner.js"
import { writePrediction } from "../output/predictions-writer.js"
import { join } from "node:path"
import { mkdirSync, writeFileSync } from "node:fs"

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
  const wsBase = join("workspaces")

  if (mode === "compare") {
    const results = await runCompare(instances, wsBase)
    printCompareSummary(results)

    // Write both raw and jingu predictions
    for (const r of results) {
      if (r.raw.finalPatchText) {
        writePrediction(join("results", "raw", "predictions.jsonl"), r.instanceId, r.raw.finalPatchText, "raw")
      }
      if (r.jingu.finalPatchText) {
        writePrediction(join("results", "jingu", "predictions.jsonl"), r.instanceId, r.jingu.finalPatchText, "jingu")
      }
    }

    writeFileSync(
      join(outDir, "compare.json"),
      JSON.stringify(results, null, 2)
    )
    return
  }

  const results = []

  for (const instance of instances) {
    if (mode === "raw") {
      const result = await runRaw(instance)
      results.push(result)
      if (result.finalPatchText) {
        writePrediction(predictionsPath, instance.instanceId, result.finalPatchText, "raw")
      }
    } else {
      const result = await runJingu(instance, wsBase)
      results.push(result)
      if (result.finalPatchText) {
        writePrediction(predictionsPath, instance.instanceId, result.finalPatchText, "jingu")
      }
    }
  }

  const accepted = results.filter((r) => r.accepted).length
  console.log(`\n--- Summary ---`)
  console.log(`Mode: ${mode} | Instances: ${results.length} | Accepted: ${accepted}/${results.length}`)
  console.log(`Predictions written to: ${predictionsPath}`)

  writeFileSync(
    join(outDir, "summary.json"),
    JSON.stringify({ mode, dataset, n: results.length, accepted }, null, 2)
  )
}

main().catch((err) => {
  console.error("Fatal:", err)
  process.exit(1)
})
