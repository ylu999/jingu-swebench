import type { GateResult } from "../types/contracts.js"

export function structuralGate(patchText: string, injectedFiles: string[] = []): GateResult {
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
  // If files were injected, the patch must target one of them.
  // files=(none) when grounding exists means the LLM ignored context — catch it early.
  if (injectedFiles.length > 0) {
    const targetedFile = injectedFiles.some((f) => patchText.includes(f))
    if (!targetedFile) {
      return {
        status: "fail",
        code: "UNGROUNDED_PATCH",
        message: `Patch targets none of the injected files (${injectedFiles.join(", ")}). LLM must use provided file contents.`,
        details: { injectedFiles },
      }
    }
  }

  return { status: "pass", code: "ACCEPTED", message: "Structural check passed" }
}
