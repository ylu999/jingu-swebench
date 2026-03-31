import type { GateResult } from "../types/contracts.js"
import type { Workspace } from "../workspace/workspace.js"

export function applyGate(workspace: Workspace, patchText: string): GateResult {
  const result = workspace.applyPatch(patchText)
  if (result.exitCode === 0) {
    return { status: "pass", code: "ACCEPTED", message: "Patch applies cleanly" }
  }
  return {
    status: "fail",
    code: "PATCH_APPLY_FAILED",
    message: "Patch does not apply",
    details: { stderr: result.stderr.trim() },
  }
}
