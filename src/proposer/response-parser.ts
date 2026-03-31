import type { PatchCandidate } from "../types/contracts.js"

// Extract patch from LLM output.
// Handles: bare diff, ```diff fences, <patch> XML blocks, text+diff mixed.
export function parseResponse(raw: string, attempt: number): PatchCandidate {
  let patchText = extractPatch(raw.trim())

  const filesTouched = extractFilesTouched(patchText)
  const summary = filesTouched.length > 0
    ? `Patch touching: ${filesTouched.join(", ")}`
    : "Patch (files unknown)"

  return { attempt, summary, patchText, filesTouched }
}

function extractPatch(text: string): string {
  // 1. Markdown code fence: ```diff ... ``` or ``` ... ```
  const fenceMatch = text.match(/```(?:diff)?\n([\s\S]*?)```/)
  if (fenceMatch) return fenceMatch[1].trim()

  // 2. XML <patch> block
  const xmlMatch = text.match(/<patch>([\s\S]*?)<\/patch>/)
  if (xmlMatch) return xmlMatch[1].trim()

  // 3. Extract the diff section from mixed text+diff output.
  //    Find the first line starting with "--- " and take everything from there.
  const diffStart = text.search(/^--- /m)
  if (diffStart !== -1) return text.slice(diffStart).trim()

  // 4. Return as-is — structural gate will reject if not a valid diff
  return text
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
