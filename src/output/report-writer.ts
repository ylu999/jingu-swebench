import { writeFileSync, mkdirSync } from "node:fs"
import { dirname } from "node:path"
import type { InstanceRunResult } from "../types/contracts.js"
import type { CompareResult } from "../runner/compare-runner.js"

// Table 1 — Summary
export function buildSummaryTable(results: InstanceRunResult[]): string {
  const total = results.length
  if (total === 0) return "No results."

  const mode = results[0].mode
  const accepted = results.filter((r) => r.accepted).length
  const invalidOutput = results.filter((r) => {
    const a = r.attempts[0]
    return a && (a.structuralGate.code === "EMPTY_PATCH" || a.structuralGate.code === "PARSE_FAILED")
  }).length
  const avgAttempts = results.reduce((s, r) => s + r.attempts.length, 0) / total

  const lines: string[] = []
  lines.push("## Table 1 — Summary")
  lines.push("")
  lines.push("| Mode | Dataset | Accepted | Valid Patch % | Invalid Output % | Avg Attempts |")
  lines.push("|------|---------|----------|--------------|-----------------|-------------|")
  lines.push(
    `| ${mode} | n=${total} | ${accepted}/${total} | ${pct(accepted, total)}% | ${pct(invalidOutput, total)}% | ${avgAttempts.toFixed(2)} |`
  )
  return lines.join("\n")
}

// Table 2 — Failure breakdown
export function buildFailureTable(results: InstanceRunResult[]): string {
  const total = results.length
  if (total === 0) return ""

  let emptyPatch = 0, applyFailed = 0, testExecFailed = 0, noImprovement = 0

  for (const r of results) {
    for (const a of r.attempts) {
      if (a.structuralGate.code === "EMPTY_PATCH") emptyPatch++
      if (a.applyGate?.code === "PATCH_APPLY_FAILED") applyFailed++
      if (a.testGate?.code === "TEST_EXEC_FAILED") testExecFailed++
      if (a.testGate?.code === "TESTS_NOT_IMPROVED") noImprovement++
    }
  }

  const mode = results[0].mode
  const lines: string[] = []
  lines.push("## Table 2 — Failure Breakdown (gate failure counts across all attempts)")
  lines.push("")
  lines.push("| Mode | EMPTY_PATCH | APPLY_FAILED | TEST_EXEC_FAILED | NO_IMPROVEMENT |")
  lines.push("|------|------------|-------------|-----------------|---------------|")
  lines.push(`| ${mode} | ${emptyPatch} | ${applyFailed} | ${testExecFailed} | ${noImprovement} |`)
  return lines.join("\n")
}

// Combined compare report
export function buildCompareReport(results: CompareResult[]): string {
  const rawResults = results.map((r) => r.raw)
  const jinguResults = results.map((r) => r.jingu)
  const total = results.length

  const rawAccepted = rawResults.filter((r) => r.accepted).length
  const jinguAccepted = jinguResults.filter((r) => r.accepted).length
  const recovered = results.filter((r) => !r.raw.accepted && r.jingu.accepted).length
  const regressed = results.filter((r) => r.raw.accepted && !r.jingu.accepted).length
  const avgAttempts = jinguResults.reduce((s, r) => s + r.attempts.length, 0) / total

  const rawInvalid = rawResults.filter((r) => {
    const a = r.attempts[0]
    return a && (a.structuralGate.code === "EMPTY_PATCH" || a.structuralGate.code === "PARSE_FAILED")
  }).length

  const lines: string[] = []
  lines.push("# Jingu × SWE-bench Compare Report")
  lines.push("")
  lines.push("## Table 1 — Summary")
  lines.push("")
  lines.push("| Mode | Instances | Accepted | Valid Patch % | Invalid Output % | Avg Attempts |")
  lines.push("|------|-----------|----------|--------------|-----------------|-------------|")
  lines.push(`| raw   | ${total} | ${rawAccepted}/${total} | ${pct(rawAccepted, total)}% | ${pct(rawInvalid, total)}% | 1.00 |`)
  lines.push(`| jingu | ${total} | ${jinguAccepted}/${total} | ${pct(jinguAccepted, total)}% | — | ${avgAttempts.toFixed(2)} |`)
  lines.push("")
  lines.push("## Table 2 — Jingu Recovery")
  lines.push("")
  lines.push(`- Recovered (raw fail → jingu pass): **${recovered}**`)
  lines.push(`- Regressed (raw pass → jingu fail): **${regressed}**`)
  lines.push(`- Avg retry attempts (jingu): **${avgAttempts.toFixed(2)}**`)
  lines.push("")

  // Per-instance detail
  lines.push("## Per-Instance Detail")
  lines.push("")
  lines.push("| Instance | raw | jingu | jingu attempts |")
  lines.push("|----------|-----|-------|---------------|")
  for (const r of results) {
    const rawStatus = r.raw.accepted ? "✓" : "✗"
    const jinguStatus = r.jingu.accepted ? "✓" : `✗ (${r.jingu.attempts.at(-1)?.testGate?.code ?? r.jingu.attempts.at(-1)?.applyGate?.code ?? r.jingu.attempts.at(-1)?.structuralGate.code})`
    lines.push(`| ${r.instanceId} | ${rawStatus} | ${jinguStatus} | ${r.jingu.attempts.length} |`)
  }

  return lines.join("\n")
}

export function writeReport(outPath: string, content: string): void {
  mkdirSync(dirname(outPath), { recursive: true })
  writeFileSync(outPath, content, "utf8")
  console.log(`[report] written: ${outPath}`)
}

function pct(n: number, total: number): string {
  return total === 0 ? "0.0" : ((n / total) * 100).toFixed(1)
}
