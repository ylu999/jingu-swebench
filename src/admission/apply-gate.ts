import type { GateResult } from "../types/contracts.js"
import type { Workspace } from "../workspace/workspace.js"

export function applyGate(workspace: Workspace, patchText: string): GateResult {
  // --check: dry-run, does not modify workspace
  const result = workspace.applyPatch(patchText)
  if (result.exitCode !== 0) {
    return {
      status: "fail",
      code: "PATCH_APPLY_FAILED",
      message: "git apply --check failed",
      details: { stderr: result.stderr.trim() },
    }
  }
  return { status: "pass", code: "ACCEPTED", message: "Patch applies cleanly" }
}
