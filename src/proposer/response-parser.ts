import type { PatchCandidate } from "../types/contracts.js"

// Extract patch from LLM output.
// Handles: bare diff, ```diff fences, <patch> XML blocks, text+diff mixed.
export function parseResponse(raw: string, attempt: number): PatchCandidate {
  let patchText = extractPatch(raw.trim())

  // Strip non-.py and test file hunks — they cause apply failures and corrupt the patch
  patchText = filterPyOnly(patchText)

  // Remove duplicate file hunks — LLMs sometimes emit two conflicting diffs for the same file
  patchText = deduplicateFileHunks(patchText)

  const filesTouched = extractFilesTouched(patchText)
  const summary = filesTouched.length > 0
    ? `Patch touching: ${filesTouched.join(", ")}`
    : "Patch (files unknown)"

  return { attempt, summary, patchText, filesTouched }
}

function extractPatch(text: string): string {
  // 0. Strip <analysis> reasoning blocks (p162 protocol) before searching for diff
  text = text.replace(/<analysis>[\s\S]*?<\/analysis>/g, "").trim()

  // 1. Markdown code fence: ```diff ... ``` or ``` ... ```
  const fenceMatch = text.match(/```(?:diff)?\n([\s\S]*?)```/)
  if (fenceMatch) return fenceMatch[1].trim()

  // 2. XML <patch> block
  const xmlMatch = text.match(/<patch>([\s\S]*?)<\/patch>/)
  if (xmlMatch) return xmlMatch[1].trim()

  // 3. Extract the diff section from mixed text+diff output.
  //    Find the first line starting with "--- a/" (git diff format) and take everything from there.
  const diffStart = text.search(/^--- a\//m)
  if (diffStart !== -1) return text.slice(diffStart).trim()

  // 4. Fallback: first "--- " line
  const fallbackStart = text.search(/^--- /m)
  if (fallbackStart !== -1) return text.slice(fallbackStart).trim()

  // 5. Return as-is — structural gate will reject if not a valid diff
  return text
}

// Keep only hunks that target production .py files (not tests, not docs).
// A hunk starts with "--- a/<path>" / "+++ b/<path>" and ends at the next "--- a/" or EOF.
function filterPyOnly(patchText: string): string {
  const lines = patchText.split("\n")
  const result: string[] = []
  let inPyFile = true  // assume py until we see a header

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    if (line.startsWith("--- ")) {
      // peek at next line for "+++ b/<path>"
      const nextLine = lines[i + 1] ?? ""
      const fileMatch = nextLine.match(/^\+\+\+ b\/(.+)/)
      const filePath = fileMatch ? fileMatch[1] : (line.match(/\.py(\s|$)/) ? line.split(" ")[1] ?? "" : "")
      const parts = filePath.split("/")
      const isTestFile = parts.some(p => p.startsWith("test") || p === "tests")
      if (fileMatch) {
        inPyFile = fileMatch[1].endsWith(".py") && !isTestFile
      } else {
        inPyFile = line.match(/\.py(\s|$)/) !== null && !isTestFile
      }
    }
    if (inPyFile) result.push(line)
  }

  return result.join("\n").trim()
}

// If a file appears more than once in the patch (duplicate hunks), keep only the last occurrence.
// LLMs sometimes generate two overlapping diffs for the same file (self-correction pattern).
function deduplicateFileHunks(patchText: string): string {
  const lines = patchText.split("\n")
  // Find all file header positions
  const headers: { file: string; start: number }[] = []
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].startsWith("--- ")) {
      const nextLine = lines[i + 1] ?? ""
      const m = nextLine.match(/^\+\+\+ b\/(.+)/)
      if (m) headers.push({ file: m[1], start: i })
    }
  }
  if (headers.length <= 1) return patchText

  // For each file, keep only the last hunk block
  const seen = new Set<string>()
  const keepFrom = new Set<number>()
  for (let i = headers.length - 1; i >= 0; i--) {
    const { file, start } = headers[i]
    if (!seen.has(file)) {
      seen.add(file)
      keepFrom.add(start)
    }
  }

  // Rebuild: include lines from header blocks we kept
  const result: string[] = []
  let inKeptBlock = true
  let headerIdx = 0
  for (let i = 0; i < lines.length; i++) {
    // Check if this line is a header start
    if (headerIdx < headers.length && headers[headerIdx].start === i) {
      inKeptBlock = keepFrom.has(i)
      headerIdx++
    }
    if (inKeptBlock) result.push(lines[i])
  }
  return result.join("\n").trim()
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
