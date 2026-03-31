import type { BenchmarkInstance } from "../types/contracts.js"

export function buildSystemPrompt(): string {
  return `You are an expert software engineer solving GitHub issues.
You will be given a repository, a problem statement, file contents, and optionally hints.
Your task is to produce a minimal, correct git patch (unified diff format) that fixes the issue.

## Reasoning Protocol (mandatory before writing the patch)

Before writing any patch, answer these three questions in a <analysis> block:

1. ROOT CAUSE: What is the exact line or expression in the code that causes the bug?
   - Point to the specific function, variable, or logic that is wrong
   - Do NOT describe symptoms (e.g. "the output is wrong") — identify the cause

2. WHY WRONG: Why is the current code incorrect?
   - What assumption does it make that is violated?
   - What edge case does it miss?

3. MINIMAL FIX: What is the smallest change that corrects the root cause?
   - If you need more than 15 lines, re-examine — you are likely fixing a symptom, not the cause
   - Prefer modifying existing logic over adding new logic
   - Do NOT add helper methods, new abstractions, or new test cases
   - If the bug involves a regex: check whether the regex flags (re.DOTALL, re.MULTILINE, re.IGNORECASE) are correct
     for the input — fix the regex definition, not the caller
   - If the bug involves a conditional: check whether the condition covers all required cases

## Patch Rules

- After the <analysis> block, output the patch in unified diff format (--- / +++ / @@ lines)
- The patch must apply cleanly with "git apply" against the EXACT file content shown
- Use the exact lines from the provided file content as context lines in the patch
- Touch ONLY production source files (no test files, no docs, no migrations)
- Modify AT MOST ONE file — the single most targeted fix
- Do NOT add new test cases or modify existing tests`
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

  parts.push(`## Task
First, write a <analysis> block answering the three reasoning questions (ROOT CAUSE / WHY WRONG / MINIMAL FIX).
Then output the unified diff patch using EXACT lines from the provided file contents as context lines.`)

  return parts.join("\n\n")
}
