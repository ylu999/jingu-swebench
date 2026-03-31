import type { GateResult } from "../types/contracts.js"
import type { Workspace } from "../workspace/workspace.js"

export type ApplyStrictness = "strict" | "fuzz"

export function applyGate(workspace: Workspace, patchText: string): GateResult {
  // Try strict first (fuzz=0)
  const strict = workspace.applyPatch(patchText, 0)
  if (strict.exitCode === 0) {
    return {
      status: "pass",
      code: "ACCEPTED",
      message: "Patch applies cleanly (strict)",
      details: { apply_strictness: "strict" as ApplyStrictness },
    }
  }

  // Fallback: fuzz=5 (LLM line-number drift tolerance)
  const fuzzy = workspace.applyPatch(patchText, 5)
  if (fuzzy.exitCode === 0) {
    return {
      status: "pass",
      code: "ACCEPTED",
      message: "Patch applies with fuzz=5 (line number drift)",
      details: { apply_strictness: "fuzz" as ApplyStrictness },
    }
  }

  return {
    status: "fail",
    code: "PATCH_APPLY_FAILED",
    message: "Patch does not apply (strict or fuzz)",
    details: { stderr: strict.stderr.trim() },
  }
}
