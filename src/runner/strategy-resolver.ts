import type { SearchStrategy } from "../types/strategy.js"

export type RuntimeContext = {
  injectedFiles: string[]
}

export type ResolutionStatus = "valid" | "degraded"

export type ResolutionResult = {
  effectiveStrategy: SearchStrategy
  status: ResolutionStatus
  reason?: string
}

export type SelectedStrategy = {
  strategy: SearchStrategy
  score: number
  status: ResolutionStatus
  reason?: string
}

// Resolve a single strategy against runtime context.
// Returns an effective (possibly downgraded) strategy + resolution status.
// Design: degrade instead of discard — system prefers fixing to abandoning.
export function resolveStrategy(strategy: SearchStrategy, ctx: RuntimeContext): ResolutionResult {
  const { promptHints } = strategy
  const hasGrounding = ctx.injectedFiles.length > 0

  // Layer A → Layer C dependency:
  // strict-observed-only requires grounding (injected files to observe).
  // Without files, the constraint makes it structurally impossible to generate a valid patch.
  if (promptHints.verificationPolicy === "strict-observed-only" && !hasGrounding) {
    return {
      effectiveStrategy: {
        ...strategy,
        promptHints: {
          ...promptHints,
          verificationPolicy: "standard",
        },
      },
      status: "degraded",
      reason: "strict_requires_files",
    }
  }

  return { effectiveStrategy: strategy, status: "valid" }
}

// Score a strategy given runtime context and its resolution result.
// Higher = more likely to succeed. Used for scheduling in selectStrategies.
function scoreStrategy(strategy: SearchStrategy, ctx: RuntimeContext, resolution: ResolutionResult): number {
  const { promptHints } = strategy
  const hasGrounding = ctx.injectedFiles.length > 0
  let score = 0

  // Degraded strategies are penalized but still runnable
  if (resolution.status === "degraded") score -= 50

  // Grounding-aware localization bonus/penalty
  if (hasGrounding) {
    if (promptHints.localizationPolicy === "test-driven") score += 20
    if (promptHints.localizationPolicy === "symbol-first") score += 15
  } else {
    // file-first is only useful when files are available
    if (promptHints.localizationPolicy === "file-first") score -= 30
  }

  // Focus-style vs context size heuristics
  const fileCount = ctx.injectedFiles.length
  if (fileCount <= 1) {
    if (promptHints.focusStyle === "minimal-fix") score += 10
  } else {
    if (promptHints.focusStyle === "root-cause") score += 10
  }

  return score
}

// Select and order strategies for a given runtime context.
// Resolves, scores, and sorts all candidates. Returns up to maxK strategies.
export function selectStrategies(
  strategies: SearchStrategy[],
  ctx: RuntimeContext,
  maxK = 3
): SelectedStrategy[] {
  const candidates: SelectedStrategy[] = strategies.map((s) => {
    const resolution = resolveStrategy(s, ctx)
    const score = scoreStrategy(resolution.effectiveStrategy, ctx, resolution)
    return {
      strategy: resolution.effectiveStrategy,
      score,
      status: resolution.status,
      reason: resolution.reason,
    }
  })

  return candidates.sort((a, b) => b.score - a.score).slice(0, maxK)
}
