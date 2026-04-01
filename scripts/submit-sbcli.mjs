/**
 * Submit predictions to SWE-bench cloud evaluation via sb-cli.
 *
 * Usage:
 *   node scripts/submit-sbcli.mjs [options]
 *
 * Options:
 *   --predictions_path <path>   default: results/multi/predictions.jsonl
 *   --subset <name>             default: swe-bench_verified  (or swe-bench_lite, swe-bench-m)
 *   --split <name>              default: test               (or dev)
 *   --run_id <id>               default: jingu-<timestamp>
 *
 * Requires:
 *   - sb-cli installed: pip install sb-cli
 *   - SWEBENCH_API_KEY env var set (sb-cli gen-api-key <email>)
 *
 * Quota reminder:
 *   test split: ~1 run per subset — use only when predictions are final
 *   dev  split: ~976+ runs — use for format/pipeline validation
 */

import { execSync } from "node:child_process"
import { existsSync, readFileSync } from "node:fs"

function getArg(flag, defaultVal) {
  const i = process.argv.indexOf(flag)
  if (i >= 0) return process.argv[i + 1]
  return defaultVal
}

const predictionsPath = getArg("--predictions_path", "results/multi/predictions.jsonl")
const subset = getArg("--subset", "swe-bench_verified")
const split = getArg("--split", "test")
const runId = getArg("--run_id", `jingu-${Date.now()}`)

// Validate
if (!process.env.SWEBENCH_API_KEY) {
  console.error("ERROR: SWEBENCH_API_KEY not set.")
  console.error("  Generate: sb-cli gen-api-key your@email.com")
  console.error("  Export:   export SWEBENCH_API_KEY=<key>")
  process.exit(1)
}

if (!existsSync(predictionsPath)) {
  console.error(`ERROR: predictions file not found: ${predictionsPath}`)
  console.error("  Run first: npm run run:multi -- --dataset verified --n <N>")
  process.exit(1)
}

// Count predictions
const lines = readFileSync(predictionsPath, "utf8").trim().split("\n").filter(Boolean)
console.log(`\nsb-cli submit`)
console.log(`  subset:     ${subset}`)
console.log(`  split:      ${split}`)
console.log(`  run_id:     ${runId}`)
console.log(`  predictions: ${predictionsPath} (${lines.length} instances)`)

if (split === "test") {
  console.log(`\n  WARNING: test split quota is limited (~1 run/subset).`)
  console.log(`  Use --split dev to validate first (quota: 976+).\n`)
}

const cmd = [
  "sb-cli submit",
  subset,
  split,
  `--predictions_path "${predictionsPath}"`,
  `--run_id "${runId}"`,
].join(" ")

console.log(`\n  Running: ${cmd}\n`)

execSync(cmd, { stdio: "inherit" })
