import type { GateResult } from "../types/contracts.js"
import type { Workspace } from "../workspace/workspace.js"

export type ApplyStrictness = "strict" | "fuzz"

// Fix common LLM patch generation errors:
// 1. Wrong hunk line counts (@@ -N,n +M,m @@ — n,m often miscounted)
// 2. Context lines missing the leading space prefix (LLM omits it sometimes)
// Returns the normalized patch, or the original if parsing fails.
export function normalizePatch(patchText: string): string {
  const lines = patchText.split("\n")
  const result: string[] = []
  let i = 0

  while (i < lines.length) {
    const line = lines[i]

    // File headers: pass through unchanged
    if (line.startsWith("--- ") || line.startsWith("+++ ")) {
      result.push(line)
      i++
      continue
    }

    // Hunk header: recount lines in hunk to fix wrong counts
    const hunkMatch = line.match(/^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@(.*)$/)
    if (hunkMatch) {
      const origStart = parseInt(hunkMatch[1], 10)
      const newStart = parseInt(hunkMatch[2], 10)
      const rest = hunkMatch[3] ?? ""

      // Collect hunk body lines
      i++
      const hunkLines: string[] = []
      while (i < lines.length) {
        const hl = lines[i]
        if (hl.startsWith("--- ") || hl.startsWith("+++ ") || hl.match(/^@@\s+-\d+/)) break
        // Skip "No newline at end of file" markers
        if (hl.startsWith("\\ ")) { i++; continue }
        hunkLines.push(hl)
        i++
      }

      // Remove trailing empty lines from hunk (common LLM artifact)
      while (hunkLines.length > 0 && hunkLines[hunkLines.length - 1] === "") {
        hunkLines.pop()
      }

      // Fix lines that are neither context (+/-/ ) — treat as context
      const normalizedHunk: string[] = []
      for (const hl of hunkLines) {
        if (hl.startsWith("+") || hl.startsWith("-") || hl.startsWith(" ") || hl === "") {
          normalizedHunk.push(hl)
        } else {
          // Line has no diff prefix — treat as context (add space prefix)
          normalizedHunk.push(" " + hl)
        }
      }

      // Recount
      let origCount = 0
      let newCount = 0
      for (const hl of normalizedHunk) {
        if (hl.startsWith("-")) origCount++
        else if (hl.startsWith("+")) newCount++
        else { origCount++; newCount++ }
      }

      // Skip empty hunks
      if (origCount === 0 && newCount === 0) continue

      result.push(`@@ -${origStart},${origCount} +${newStart},${newCount} @@${rest}`)
      result.push(...normalizedHunk)
      continue
    }

    result.push(line)
    i++
  }

  return result.join("\n")
}

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

  // Fallback: fuzz=25 (LLM often hallucinates line numbers ±24 lines away)
  const fuzzy = workspace.applyPatch(patchText, 25)
  if (fuzzy.exitCode === 0) {
    return {
      status: "pass",
      code: "ACCEPTED",
      message: "Patch applies with fuzz=25 (line number drift)",
      details: { apply_strictness: "fuzz" as ApplyStrictness },
    }
  }

  // Fallback: normalize patch (fix wrong hunk counts, missing context prefixes)
  const normalized = normalizePatch(patchText)
  if (normalized !== patchText) {
    const normStrict = workspace.applyPatch(normalized, 0)
    if (normStrict.exitCode === 0) {
      return {
        status: "pass",
        code: "ACCEPTED",
        message: "Patch applies after normalization (fixed hunk counts)",
        details: { apply_strictness: "strict" as ApplyStrictness, normalized: true },
      }
    }
    const normFuzzy = workspace.applyPatch(normalized, 25)
    if (normFuzzy.exitCode === 0) {
      return {
        status: "pass",
        code: "ACCEPTED",
        message: "Patch applies after normalization with fuzz=25",
        details: { apply_strictness: "fuzz" as ApplyStrictness, normalized: true },
      }
    }
  }

  // patch writes errors to stdout, not stderr
  const errOutput = (strict.stdout + strict.stderr).trim()
  return {
    status: "fail",
    code: "PATCH_APPLY_FAILED",
    message: "Patch does not apply (strict, fuzz, or normalized)",
    details: { stderr: errOutput, patch_head: patchText.slice(0, 500) },
  }
}
