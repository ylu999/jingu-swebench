import type { BenchmarkInstance, InstanceRunResult, AttemptResult } from "../types/contracts.js"
import { propose } from "../proposer/proposer-adapter.js"

// Gate 1 only: structural check (non-empty, looks like a diff)
function structuralGate(patchText: string) {
  if (!patchText || patchText.length < 10) {
    return { status: "fail" as const, code: "EMPTY_PATCH" as const, message: "Patch is empty or too short" }
  }
  const hasDiffMarker = /^(---|\+\+\+|@@)/m.test(patchText)
  if (!hasDiffMarker) {
    return { status: "fail" as const, code: "PARSE_FAILED" as const, message: "Patch contains no diff markers (---/+++/@@)" }
  }
  return { status: "pass" as const, code: "ACCEPTED" as const, message: "Structural check passed" }
}

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
