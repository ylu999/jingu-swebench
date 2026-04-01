import type { BenchmarkInstance } from "../types/contracts.js"
import type { SearchStrategy } from "../types/strategy.js"

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
  opts: {
    fileContents?: Record<string, string>
    previousFeedback?: string
    strategy?: SearchStrategy
  } = {}
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

  const strategyHint = buildStrategyHint(opts.strategy)
  if (strategyHint) {
    parts.push(`## Search Strategy\n${strategyHint}`)
  }

  parts.push(`## Task
First, write a <analysis> block answering the three reasoning questions (ROOT CAUSE / WHY WRONG / MINIMAL FIX).
Then output the unified diff patch using EXACT lines from the provided file contents as context lines.`)

  return parts.join("\n\n")
}

// Pure function: strategy → instruction segment injected into user prompt.
// Three sections: Layer A (localization), Layer B (patching), Layer C (verification).
// Returns empty string when no strategy or no meaningful hints.
function buildStrategyHint(strategy?: SearchStrategy): string {
  if (!strategy) return ""

  const lines: string[] = []
  const { localizationPolicy, focusStyle, analysisDepth, maxPatchLines, verificationPolicy, extraInstruction } = strategy.promptHints

  // --- Layer A: Localization ---
  if (localizationPolicy === "test-driven") {
    lines.push("Localization: start from the failing test name(s) to identify the source function or class under test.")
    lines.push("Do NOT guess which file to edit — only modify code that is directly exercised by the failing test.")
  } else if (localizationPolicy === "symbol-first") {
    lines.push("Localization: extract the key symbol (function, class, variable) from the problem statement first.")
    lines.push("Find that symbol in the provided file contents before deciding where to apply the fix.")
    lines.push("CRITICAL: You MUST select the target file from the provided file contents. Do NOT patch a file that was not shown to you.")
  } else if (localizationPolicy === "file-first") {
    lines.push("Localization: work through the provided file contents systematically to find the defect.")
    lines.push("Consider all provided files before selecting the one to modify.")
  }

  // --- Layer B: Patching ---
  if (focusStyle === "minimal-fix") {
    lines.push("Focus: find the SINGLE smallest change that fixes the root cause.")
    lines.push("Prefer changing one expression or condition over restructuring logic.")
  } else if (focusStyle === "root-cause") {
    lines.push("Focus: trace the bug to its deepest root cause before proposing a fix.")
    lines.push("Do not fix symptoms — identify why the code is structurally wrong.")
  } else if (focusStyle === "defensive") {
    lines.push("Focus: fix the root cause AND add guards against related edge cases.")
    lines.push("Consider what other inputs could trigger the same class of failure.")
  }

  if (analysisDepth === "light") {
    lines.push("Analysis depth: light — identify the bug quickly, do not over-analyze.")
  } else if (analysisDepth === "deep") {
    lines.push("Analysis depth: deep — examine call sites, callers, and related code paths before deciding on the fix.")
  }

  if (maxPatchLines !== undefined) {
    lines.push(`Patch size constraint: the patch MUST be at most ${maxPatchLines} lines of actual changes (+ and - lines combined).`)
    lines.push("If your patch exceeds this limit, re-examine — you are likely fixing a symptom.")
  }

  // --- Layer C: Verification ---
  if (verificationPolicy === "strict-observed-only") {
    lines.push("Verification constraint: ONLY modify lines that appear verbatim in the provided file contents.")
    lines.push("Do NOT edit any file that was not shown to you. Do NOT invent context lines.")
  }

  if (extraInstruction) {
    lines.push(extraInstruction)
  }

  return lines.join("\n")
}
