import type { GateResult } from "../types/contracts.js"

export function structuralGate(patchText: string): GateResult {
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
  return { status: "pass", code: "ACCEPTED", message: "Structural check passed" }
}
