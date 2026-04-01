import type { AttemptResult } from "../types/contracts.js"
import type { Workspace } from "../workspace/workspace.js"

// Extract the target line number from a patch hunk header: "@@ -N,n +M,m @@"
// Returns the original file line number (from -N) for finding the relevant region.
function extractHunkLineNumber(patchText: string): number | null {
  const m = patchText.match(/@@ -(\d+)(?:,\d+)? \+(\d+)/)
  if (!m) return null
  return parseInt(m[1], 10)
}

export function buildRetryFeedback(
  attempt: AttemptResult,
  workspace?: Workspace,
  opts: { verificationPolicy?: string } = {}
): string {
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
    const patchHead = (failedGate.details?.patch_head as string | undefined) ?? ""
    if (patchHead) {
      lines.push(`\nYour previous patch (first 500 chars):\n${patchHead}`)
    }
    const touched = attempt.candidate?.filesTouched ?? []
    if (touched.length > 0) {
      lines.push(`\nYour previous attempt touched: ${touched.join(", ")}`)
    }
    if (workspace) {
      for (const file of touched) {
        // Extract the hunk's target line number from the patch header (@@ -N,n +N,n @@)
        // to show the actual file content around that region, not just head -40
        const hunkLineNum = extractHunkLineNumber(patchHead)
        if (hunkLineNum !== null) {
          const start = Math.max(1, hunkLineNum - 5)
          const count = 50
          const preview = workspace.exec(`sed -n '${start},${start + count}p' "${file}"`).stdout
          if (preview) {
            lines.push(`\nActual file content of ${file} (lines ${start}–${start + count}):\n${preview}`)
            lines.push("Use ONLY these exact lines as context lines in your patch.")
          }
        } else {
          const preview = workspace.exec(`head -60 "${file}"`).stdout
          if (preview) {
            lines.push(`\nCurrent state of ${file} (first 60 lines):\n${preview}`)
          }
        }
      }
    }
    lines.push("\nPlease produce a corrected patch. Use ONLY the exact lines shown above as context lines.")

    // feedback-grounded: inject grounding constraint here (not in first-shot prompt)
    if (opts.verificationPolicy === "feedback-grounded") {
      lines.push("\nGrounding constraint: use ONLY lines that appear verbatim in the provided file contents as context lines.")
      lines.push("Do NOT invent context lines. If the patch failed to apply, your context lines likely don't match the file exactly.")
    }
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
  } else if (failedGate.code === "UNGROUNDED_PATCH") {
    const injected = (failedGate.details?.injectedFiles as string[] | undefined) ?? []
    lines.push("\nYour patch targeted a file that was NOT provided to you.")
    if (injected.length > 0) {
      lines.push(`You MUST select one of these provided files as your target: ${injected.join(", ")}`)
    }
    lines.push("Only modify code that appears verbatim in the provided file contents above.")

    // feedback-grounded: reinforce grounding on ungrounded patch (same message, explicit reminder)
    if (opts.verificationPolicy === "feedback-grounded") {
      lines.push("This grounding constraint applies to all remaining attempts: never patch a file not shown to you.")
    }
  } else if (failedGate.code === "EMPTY_PATCH" || failedGate.code === "PARSE_FAILED") {
    lines.push("\nYour previous response did not contain a valid unified diff patch.")
    lines.push("Output ONLY the patch in unified diff format (--- / +++ / @@ lines). No explanation, no markdown.")
  }

  return lines.join("\n")
}
