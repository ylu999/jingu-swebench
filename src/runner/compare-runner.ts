import type { BenchmarkInstance, InstanceRunResult } from "../types/contracts.js"
import { runRaw } from "./raw-runner.js"
import { runJingu } from "./jingu-runner.js"
import { join } from "node:path"

export interface CompareResult {
  instanceId: string
  raw: InstanceRunResult
  jingu: InstanceRunResult
}

export async function runCompare(
  instances: BenchmarkInstance[],
  workspaceBase: string,
  opts: { skipTestGate?: boolean } = {}
): Promise<CompareResult[]> {
  const results: CompareResult[] = []

  for (const instance of instances) {
    console.log(`\n[compare] ${instance.instanceId}`)
    const raw = await runRaw(instance)
    const jingu = await runJingu(instance, workspaceBase, opts)
    results.push({ instanceId: instance.instanceId, raw, jingu })
  }

  return results
}

export function printCompareSummary(results: CompareResult[]): void {
  const total = results.length
  const rawAccepted = results.filter((r) => r.raw.accepted).length
  const jinguAccepted = results.filter((r) => r.jingu.accepted).length

  const rawPct = pct(rawAccepted, total)
  const jinguPct = pct(jinguAccepted, total)

  const jinguOnlyWins = results.filter((r) => !r.raw.accepted && r.jingu.accepted).length
  const rawOnlyWins = results.filter((r) => r.raw.accepted && !r.jingu.accepted).length

  const avgAttempts =
    results.reduce((sum, r) => sum + r.jingu.attempts.length, 0) / total

  console.log("\n=== Compare Summary ===")
  console.log(`Instances: ${total}`)
  console.log(``)
  console.log(`Mode     | Accepted | Valid Patch %`)
  console.log(`---------|----------|-------------`)
  console.log(`raw      | ${rawAccepted}/${total}      | ${rawPct}%`)
  console.log(`jingu    | ${jinguAccepted}/${total}      | ${jinguPct}%`)
  console.log(``)
  console.log(`Jingu recovered (raw fail → jingu pass): ${jinguOnlyWins}`)
  console.log(`Jingu regressed (raw pass → jingu fail): ${rawOnlyWins}`)
  console.log(`Avg attempts (jingu): ${avgAttempts.toFixed(2)}`)
}

function pct(n: number, total: number): string {
  return total === 0 ? "0.0" : ((n / total) * 100).toFixed(1)
}
