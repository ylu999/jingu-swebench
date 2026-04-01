import type { SearchStrategy } from "../types/strategy.js"

export type RuntimeContext = {
  injectedFiles: string[]
  // Total lines injected across all files — used to assess localization confidence.
  injectedTotalLines?: number
  // Number of anchor symbols matched in injected files (from FAIL_TO_PASS + backtick identifiers)
  injectedAnchorCount?: number
}

export type LocalizationTier = "off" | "weak" | "strong"

export type LocalizationConfidence = {
  tier: LocalizationTier
  score: number
  reason: string
}

// Compute localization confidence from runtime context.
// Three signals:
//   file_count:   0=none, 1=minimal, 2+=good
//   line_count:   total injected lines; sparse = low confidence
//   anchor_count: matched symbols from FAIL_TO_PASS / backtick ids; more = stronger evidence
//
// Score → tier:
//   < 20  → off  (no useful grounding, A hint would be noise)
//   20-49 → weak (files present but evidence sparse, use soft hint only)
//   >= 50 → strong (adequate grounding, full localization hint)
export function computeLocalizationConfidence(ctx: RuntimeContext): LocalizationConfidence {
  const fileCount = ctx.injectedFiles.length
  const totalLines = ctx.injectedTotalLines ?? 0
  const anchorCount = ctx.injectedAnchorCount ?? 0

  if (fileCount === 0) {
    return { tier: "off", score: 0, reason: "no_files" }
  }

  let score = 0
  const reasons: string[] = []

  // File count contribution (0-30 points)
  if (fileCount >= 2) { score += 30; reasons.push("files≥2") }
  else if (fileCount === 1) { score += 15; reasons.push("files=1") }

  // Line count contribution (0-40 points): adequate = 60+ lines
  if (totalLines >= 80) { score += 40; reasons.push(`lines=${totalLines}`) }
  else if (totalLines >= 40) { score += 25; reasons.push(`lines=${totalLines}`) }
  else if (totalLines >= 15) { score += 10; reasons.push(`lines=${totalLines}(sparse)`) }
  else { reasons.push(`lines=${totalLines}(very_sparse)`) }

  // Anchor count contribution (0-30 points)
  if (anchorCount >= 3) { score += 30; reasons.push(`anchors=${anchorCount}`) }
  else if (anchorCount >= 1) { score += 15; reasons.push(`anchors=${anchorCount}`) }
  else { reasons.push("anchors=0") }

  const tier: LocalizationTier = score >= 50 ? "strong" : score >= 20 ? "weak" : "off"
  return { tier, score, reason: reasons.join(",") }
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
//
// Layer A downgrade ladder (driven by computeLocalizationConfidence):
//   tier=off    → remove localizationPolicy (B-only patching)
//   tier=weak   → downgrade to "file-first" (soft hint, no assertive constraints)
//   tier=strong → keep full localizationPolicy (current behavior)
export function resolveStrategy(strategy: SearchStrategy, ctx: RuntimeContext): ResolutionResult {
  const { promptHints } = strategy
  const hasGrounding = ctx.injectedFiles.length > 0

  // Layer A → Layer C dependency:
  // strict-observed-only requires grounding (injected files to observe).
  if (promptHints.verificationPolicy === "strict-observed-only" && !hasGrounding) {
    return {
      effectiveStrategy: {
        ...strategy,
        promptHints: { ...promptHints, verificationPolicy: "standard" },
      },
      status: "degraded",
      reason: "strict_requires_files",
    }
  }

  // Layer A confidence downgrade:
  // Only applies to assertive policies (test-driven, symbol-first) — file-first is already weak.
  if (promptHints.localizationPolicy && promptHints.localizationPolicy !== "file-first") {
    const conf = computeLocalizationConfidence(ctx)
    console.log(`  [resolver] A-confidence: tier=${conf.tier} score=${conf.score} (${conf.reason}) strategy=${strategy.id}`)

    if (conf.tier === "off") {
      return {
        effectiveStrategy: {
          ...strategy,
          promptHints: { ...promptHints, localizationPolicy: undefined },
        },
        status: "degraded",
        reason: `a_off:${conf.reason}`,
      }
    }
    if (conf.tier === "weak") {
      // A-weak: downgrade to file-first (soft "look through what you have")
      // Removes assertive hints like "CRITICAL: select from provided files" and
      // "start from the failing test name" — these require strong evidence to be useful
      return {
        effectiveStrategy: {
          ...strategy,
          promptHints: { ...promptHints, localizationPolicy: "file-first" },
        },
        status: "degraded",
        reason: `a_weak:${conf.reason}`,
      }
    }
    // tier=strong: keep full policy
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
