import type { BenchmarkInstance, InstanceRunResult, AttemptResult } from "../types/contracts.js"
import { propose } from "../proposer/proposer-adapter.js"
import { structuralGate } from "../admission/structural-gate.js"
import { applyGate } from "../admission/apply-gate.js"
import { testGate, runTestsBaseline } from "../admission/test-gate.js"
import { buildRetryFeedback } from "../admission/retry-feedback.js"
import { Workspace } from "../workspace/workspace.js"
import { join } from "node:path"

const MAX_ATTEMPTS = 3
const TEST_CMD = "python -m pytest -x -q --tb=short 2>&1 || true"

export async function runJingu(
  instance: BenchmarkInstance,
  workspaceBase: string
): Promise<InstanceRunResult> {
  const t0 = Date.now()
  console.log(`[jingu] ${instance.instanceId}`)

  const wsDir = join(workspaceBase, instance.instanceId.replace(/\//g, "__"))
  const workspace = Workspace.fromLocalPath(wsDir)

  // Baseline test counts before any patch
  const baseline = runTestsBaseline(workspace, TEST_CMD)
  console.log(`  [jingu] baseline: passed=${baseline.passed} failed=${baseline.failed}`)

  const attempts: AttemptResult[] = []
  let previousFeedback: string | undefined
  let finalPatchText: string | undefined

  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    const candidate = await propose(instance, attempt, previousFeedback)

    // Gate 1: structural
    const sg = structuralGate(candidate.patchText)
    if (sg.status === "fail") {
      const ar: AttemptResult = { attempt, candidate, structuralGate: sg, accepted: false }
      attempts.push(ar)
      console.log(`  [jingu] attempt=${attempt} FAIL structural (${sg.code})`)
      previousFeedback = buildRetryFeedback(ar)
      continue
    }

    // Gate 2: apply
    const ag = applyGate(workspace, candidate.patchText)
    if (ag.status === "fail") {
      const ar: AttemptResult = { attempt, candidate, structuralGate: sg, applyGate: ag, accepted: false }
      attempts.push(ar)
      console.log(`  [jingu] attempt=${attempt} FAIL apply (${ag.code})`)
      previousFeedback = buildRetryFeedback(ar, workspace)
      workspace.reset()
      continue
    }

    // Actually apply the patch for the test gate
    workspace.applyPatchForReal(candidate.patchText)

    // Gate 3: test delta
    const tg = testGate(workspace, TEST_CMD, baseline)
    workspace.reset() // always reset after test run

    if (tg.status === "fail") {
      const ar: AttemptResult = { attempt, candidate, structuralGate: sg, applyGate: ag, testGate: tg, accepted: false }
      attempts.push(ar)
      console.log(`  [jingu] attempt=${attempt} FAIL test (${tg.code})`)
      previousFeedback = buildRetryFeedback(ar, workspace)
      continue
    }

    // All gates passed
    const ar: AttemptResult = { attempt, candidate, structuralGate: sg, applyGate: ag, testGate: tg, accepted: true }
    attempts.push(ar)
    finalPatchText = candidate.patchText
    console.log(`  [jingu] attempt=${attempt} ACCEPTED`)
    break
  }

  const accepted = finalPatchText !== undefined

  return {
    instanceId: instance.instanceId,
    mode: "jingu",
    accepted,
    attempts,
    finalPatchText,
    durationMs: Date.now() - t0,
  }
}
