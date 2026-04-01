import type { GateResult } from "../types/contracts.js"

export function structuralGate(
  patchText: string,
  injectedFiles: string[] = [],
  filesTouched: string[] = []
): GateResult {
  if (!patchText || patchText.trim().length < 10) {
    return { status: "fail", code: "EMPTY_PATCH", message: "Patch is empty or too short" }
  }
  const hasDiffMarker = /^(---|\+\+\+|@@)/m.test(patchText)
  if (!hasDiffMarker) {
    return {
      status: "fail",
      code: "PARSE_FAILED",
      message: "Patch contains no diff markers (---/+++/@@)",
    }
  }

  // Grounding compliance check:
  // If files were injected, the patch must target at least one of them.
  // filesTouched=[] when injectedFiles exist means LLM targeted an unshown file — catch early.
  if (injectedFiles.length > 0) {
    const isGrounded =
      filesTouched.length > 0 &&
      filesTouched.some((t) => injectedFiles.some((i) => t.endsWith(i) || i.endsWith(t) || t === i))
    if (!isGrounded) {
      return {
        status: "fail",
        code: "UNGROUNDED_PATCH",
        message: `Patch targets none of the injected files (${injectedFiles.join(", ")}). LLM must use provided file contents.`,
        details: { injectedFiles, filesTouched },
      }
    }
  }

  return { status: "pass", code: "ACCEPTED", message: "Structural check passed" }
}
