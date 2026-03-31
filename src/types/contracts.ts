// dataset
export type BenchmarkInstance = {
  instanceId: string
  repo: string
  baseCommit: string
  problemStatement: string
  hintsText?: string
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
