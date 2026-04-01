// dataset
export type BenchmarkInstance = {
  instanceId: string
  repo: string
  baseCommit: string
  problemStatement: string
  hintsText?: string
  // SWE-bench ground truth: test IDs that must go from FAIL → PASS
  failToPass?: string[]
  // version string used to pick correct test runner
  version?: string
}

// proposer output
export type PatchCandidate = {
  attempt: number
  summary: string
  patchText: string
  filesTouched: string[]
  reasoning?: string
}

// gate result
export type GateResult = {
  status: "pass" | "fail"
  code:
    | "EMPTY_PATCH"
    | "PARSE_FAILED"
    | "PATCH_APPLY_FAILED"
    | "TEST_EXEC_FAILED"
    | "TEST_HARNESS_UNAVAILABLE"
    | "TESTS_NOT_IMPROVED"
    | "ACCEPTED"
  message: string
  details?: Record<string, unknown>
}

// per-attempt
export type AttemptResult = {
  attempt: number
  candidate?: PatchCandidate
  structuralGate: GateResult
  applyGate?: GateResult
  testGate?: GateResult
  accepted: boolean
  // Strategy resolution result for this attempt (set when strategy was resolved/degraded)
  strategyResolution?: { status: "valid" | "degraded"; reason?: string }
}

// per-instance final result
export type InstanceRunResult = {
  instanceId: string
  mode: "raw" | "jingu"
  accepted: boolean
  attempts: AttemptResult[]
  finalPatchText?: string
  durationMs: number
}
