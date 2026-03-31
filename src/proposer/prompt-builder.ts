import type { BenchmarkInstance } from "../types/contracts.js"

export function buildSystemPrompt(): string {
  return `You are an expert software engineer solving GitHub issues.
You will be given a repository, a problem statement, and optionally hints.
Your task is to produce a minimal, correct git patch (unified diff format) that fixes the issue.

Rules:
- Output ONLY the patch in unified diff format (--- / +++ / @@ lines)
- Do NOT include any explanation outside the patch
- The patch must apply cleanly with "git apply"
- Touch only files necessary to fix the issue
- Keep changes minimal`
}

export function buildUserPrompt(
  instance: BenchmarkInstance,
  previousFeedback?: string
): string {
  const parts: string[] = []

  parts.push(`## Repository\n${instance.repo}`)
  parts.push(`## Problem Statement\n${instance.problemStatement}`)

  if (instance.hintsText) {
    parts.push(`## Hints\n${instance.hintsText}`)
  }

  if (previousFeedback) {
    parts.push(`## Previous Attempt Feedback\n${previousFeedback}`)
  }

  parts.push(`## Task\nProduce a unified diff patch that fixes the issue above.`)

  return parts.join("\n\n")
}
