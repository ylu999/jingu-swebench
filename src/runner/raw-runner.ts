import type { BenchmarkInstance, InstanceRunResult, AttemptResult } from "../types/contracts.js"
import { propose } from "../proposer/proposer-adapter.js"
import { structuralGate } from "../admission/structural-gate.js"

export async function runRaw(instance: BenchmarkInstance): Promise<InstanceRunResult> {
  const t0 = Date.now()
  console.log(`[raw] ${instance.instanceId}`)

  const candidate = await propose(instance, 1)
  const gate = structuralGate(candidate.patchText)

  const attempt: AttemptResult = {
    attempt: 1,
    candidate,
    structuralGate: gate,
    accepted: gate.status === "pass",
  }

  const accepted = gate.status === "pass"
  console.log(`  [raw] ${accepted ? "ACCEPTED" : "REJECTED"} (${gate.code})`)

  return {
    instanceId: instance.instanceId,
    mode: "raw",
    accepted,
    attempts: [attempt],
    finalPatchText: accepted ? candidate.patchText : undefined,
    durationMs: Date.now() - t0,
  }
}
