import type { StrategyRunResult } from "../types/strategy.js"

// Score a single strategy run.
// Verdict is the primary discriminator — rejected/failed runs score -Infinity
// and can never beat any accepted run regardless of patch size.
// Among accepted runs: prefer fewer files touched, then fewer lines changed.
export function scoreRun(result: StrategyRunResult): number {
  if (result.verdict !== "accepted" || !result.runResult.finalPatchText) {
    return -Infinity
  }

  const patch = result.runResult.finalPatchText
  const lines = patch.split("\n")
  const filesTouched = lines.filter((l) => l.startsWith("+++ b/")).length
  const linesChanged = lines.filter(
    (l) => (l.startsWith("+") || l.startsWith("-")) && !l.startsWith("+++") && !l.startsWith("---")
  ).length

  return 1000 - filesTouched * 50 - linesChanged * 2
}

// Pick the highest-scoring accepted run. Returns null if none accepted.
export function pickBest(results: StrategyRunResult[]): StrategyRunResult | null {
  const scored = results
    .map((r) => ({ result: r, score: scoreRun(r) }))
    .filter((x) => x.score > -Infinity)
    .sort((a, b) => b.score - a.score)

  if (scored.length === 0) return null
  return scored[0].result
}
