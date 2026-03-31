import type { AttemptResult } from "../types/contracts.js"
import type { Workspace } from "../workspace/workspace.js"

export function buildRetryFeedback(attempt: AttemptResult, workspace?: Workspace): string {
  const lines: string[] = []

  // Determine which gate failed
  const failedGate =
    attempt.applyGate?.status === "fail"
      ? attempt.applyGate
      : attempt.testGate?.status === "fail"
      ? attempt.testGate
      : attempt.structuralGate

  lines.push(`Gate failed: ${failedGate.code}`)
  lines.push(failedGate.message)

  if (failedGate.code === "PATCH_APPLY_FAILED") {
    const stderr = (failedGate.details?.stderr as string | undefined) ?? ""
    if (stderr) {
      lines.push(`\nError detail:\n${stderr}`)
    }
    const touched = attempt.candidate?.filesTouched ?? []
    if (touched.length > 0) {
      lines.push(`\nYour previous attempt touched: ${touched.join(", ")}`)
    }
    if (workspace) {
      for (const file of touched) {
        const preview = workspace.exec(`head -40 "${file}"`).stdout
        if (preview) {
          lines.push(`\nCurrent state of ${file} (first 40 lines):\n${preview}`)
        }
      }
    }
    lines.push("\nPlease produce a corrected patch. Focus only on the failing hunk.")
  } else if (failedGate.code === "TESTS_NOT_IMPROVED") {
    const details = failedGate.details as Record<string, unknown> | undefined
    const output = (details?.output as string | undefined) ?? ""
    if (output) {
      lines.push(`\nTest output (last 30 lines):\n${output}`)
    }
    const touched = attempt.candidate?.filesTouched ?? []
    if (touched.length > 0) {
      lines.push(`\nYour patch touched: ${touched.join(", ")}`)
    }
    lines.push("\nPlease revise the patch to fix the failing tests above.")
  } else if (failedGate.code === "EMPTY_PATCH" || failedGate.code === "PARSE_FAILED") {
    lines.push("\nYour previous response did not contain a valid unified diff patch.")
    lines.push("Output ONLY the patch in unified diff format (--- / +++ / @@ lines). No explanation, no markdown.")
  }

  return lines.join("\n")
}
