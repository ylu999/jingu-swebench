import type { BenchmarkInstance } from "../types/contracts.js"

export function buildSystemPrompt(): string {
  return `You are an expert software engineer solving GitHub issues.
You will be given a repository, a problem statement, file contents, and optionally hints.
Your task is to produce a minimal, correct git patch (unified diff format) that fixes the issue.

Rules:
- Output ONLY the patch in unified diff format (--- / +++ / @@ lines)
- Do NOT include any explanation outside the patch
- The patch must apply cleanly with "git apply" against the EXACT file content shown
- Use the exact lines from the provided file content as context lines in the patch
- Touch only files necessary to fix the issue
- Keep changes minimal`
}

export function buildUserPrompt(
  instance: BenchmarkInstance,
  opts: { fileContents?: Record<string, string>; previousFeedback?: string } = {}
): string {
  const parts: string[] = []

  parts.push(`## Repository\n${instance.repo}`)
  parts.push(`## Problem Statement\n${instance.problemStatement}`)

  if (instance.hintsText) {
    parts.push(`## Hints\n${instance.hintsText}`)
  }

  if (opts.fileContents && Object.keys(opts.fileContents).length > 0) {
    const fileSection = Object.entries(opts.fileContents)
      .map(([path, content]) => `### ${path}\n\`\`\`\n${content}\n\`\`\``)
      .join("\n\n")
    parts.push(`## Relevant File Contents\n${fileSection}`)
  }

  if (opts.previousFeedback) {
    parts.push(`## Previous Attempt Feedback\n${opts.previousFeedback}`)
  }

  parts.push(`## Task\nProduce a unified diff patch that fixes the issue above. Use the EXACT lines from the provided file contents as context lines.`)

  return parts.join("\n\n")
}
