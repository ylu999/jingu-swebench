import type { PatchCandidate } from "../types/contracts.js"

// Extract patch from LLM output.
// The LLM should output only a patch, but may wrap it in markdown code blocks.
export function parseResponse(raw: string, attempt: number): PatchCandidate {
  let patchText = raw.trim()

  // Strip markdown code fences (```diff ... ``` or ``` ... ```)
  const fenceMatch = patchText.match(/^```(?:diff)?\n([\s\S]*?)```\s*$/m)
  if (fenceMatch) {
    patchText = fenceMatch[1].trim()
  }

  const filesTouched = extractFilesTouched(patchText)
  const summary = filesTouched.length > 0
    ? `Patch touching: ${filesTouched.join(", ")}`
    : "Patch (files unknown)"

  return {
    attempt,
    summary,
    patchText,
    filesTouched,
  }
}

function extractFilesTouched(patchText: string): string[] {
  const files = new Set<string>()
  for (const line of patchText.split("\n")) {
    // +++ b/path/to/file
    if (line.startsWith("+++ b/")) {
      files.add(line.slice(6).trim())
    }
  }
  return Array.from(files)
}
