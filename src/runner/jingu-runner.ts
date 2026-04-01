import type { BenchmarkInstance, InstanceRunResult, AttemptResult } from "../types/contracts.js"
import type { SearchStrategy } from "../types/strategy.js"
import { resolveStrategy } from "./strategy-resolver.js"
import { propose } from "../proposer/proposer-adapter.js"
import { structuralGate } from "../admission/structural-gate.js"
import { applyGate } from "../admission/apply-gate.js"
import { testGate, runTestsBaseline, type TestCounts } from "../admission/test-gate.js"
import { buildRetryFeedback } from "../admission/retry-feedback.js"
import { Workspace } from "../workspace/workspace.js"
import { join } from "node:path"
import { existsSync, readFileSync } from "node:fs"
import { join as joinPath } from "node:path"

const MAX_ATTEMPTS = 3
const TEST_CMD = "python -m pytest -x -q --tb=short 2>&1 || true"

// Total lines injected across all files — keeps prompt budget bounded
const MAX_TOTAL_INJECT_LINES = 400
// Per-file hard cap (never inject more than this even if budget allows)
const MAX_FILE_LINES = 350

// Extract the most relevant window from a file around a set of anchor symbols.
// Finds the first def/class line matching any anchor, returns WINDOW_SIZE lines
// centered on it. Falls back to head-of-file if no anchor found.
const WINDOW_SIZE = 120
function extractRelevantWindow(lines: string[], anchors: string[]): { content: string; note: string } {
  if (anchors.length > 0) {
    for (const anchor of anchors) {
      const idx = lines.findIndex((l) =>
        l.match(new RegExp(`\\b(def|class)\\s+${anchor.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`))
      )
      if (idx >= 0) {
        const start = Math.max(0, idx - 10)
        const end = Math.min(lines.length, idx + WINDOW_SIZE)
        return {
          content: lines.slice(start, end).join("\n"),
          note: `(lines ${start + 1}–${end} of ${lines.length}, anchored on "${anchor}")`,
        }
      }
    }
  }
  // No anchor found — take head of file
  const end = Math.min(lines.length, MAX_FILE_LINES)
  return {
    content: lines.slice(0, end).join("\n"),
    note: lines.length > end ? `(lines 1–${end} of ${lines.length}, truncated)` : "",
  }
}

function readWorkspaceFiles(
  workspace: Workspace,
  filePaths: string[],
  anchors: string[] = []
): Record<string, string> {
  const result: Record<string, string> = {}
  let totalLines = 0

  for (const p of filePaths) {
    if (totalLines >= MAX_TOTAL_INJECT_LINES) break
    const abs = joinPath(workspace.dir, p)
    if (!existsSync(abs)) continue
    try {
      const lines = readFileSync(abs, "utf8").split("\n")
      const budget = Math.min(MAX_FILE_LINES, MAX_TOTAL_INJECT_LINES - totalLines)

      let content: string
      if (lines.length <= budget) {
        content = lines.join("\n")
      } else {
        const { content: w, note } = extractRelevantWindow(lines, anchors)
        const wLines = w.split("\n").slice(0, budget)
        content = wLines.join("\n")
        if (note) content += `\n... ${note}`
      }

      result[p] = content
      totalLines += content.split("\n").length
    } catch {
      // skip unreadable files
    }
  }
  return result
}

// Find candidate files to inject into the prompt.
// Priority order:
//   1. Explicit .py paths in hints_text
//   2. Dotted module paths in problem statement (e.g. "ascii.rst" → astropy/io/ascii/rst.py)
//   3. Explicit .py paths in problem statement
//   4. git grep for long identifiers (>6 chars) in backticks, restricted to repo module dir
function findCandidateFiles(instance: BenchmarkInstance, workspace: Workspace): string[] {
  const files = new Set<string>()

  // Only collect .py source files — docs/txt/rst excluded to prevent hallucinated line numbers

  // 1. Explicit .py paths in hints_text
  for (const m of (instance.hintsText ?? "").matchAll(/[\w/.+-]+\.py/g)) files.add(m[0])

  // 2. Dotted module paths like "ascii.rst", "io.fits.table", "astropy.modeling.separable"
  //    Convert to file path: replace dots with /, append .py, find in workspace
  const text = instance.problemStatement + " " + (instance.hintsText ?? "")
  const dottedModules = [...text.matchAll(/\b([a-z][a-z0-9_]+(?:\.[a-z][a-z0-9_]+){1,5})\b/g)]
    .map((m) => m[1])
    .filter((mod) => !mod.includes(".."))  // skip version strings
  for (const mod of dottedModules) {
    const candidate = mod.replace(/\./g, "/") + ".py"
    if (existsSync(joinPath(workspace.dir, candidate))) {
      files.add(candidate)
    }
    // Also try last two segments (e.g. "ascii.rst" → find "*/ascii/rst.py")
    const parts = mod.split(".")
    if (parts.length >= 2) {
      const short = parts.slice(-2).join("/") + ".py"
      const found = workspace.exec(`find . -path "*/${short}" -not -path "*/test*" -not -path "*/.git/*" 2>/dev/null | head -1`).stdout.trim()
      if (found) files.add(found.replace(/^\.\//, ""))
    }
  }

  // 3. Explicit .py paths in problem statement
  for (const m of instance.problemStatement.matchAll(/[\w/.+-]+\.py/g)) files.add(m[0])

  // Remove non-.py files (docs, rst, txt) — they cause hallucinated line numbers
  // Remove test files — injecting them causes LLM to add unnecessary test code
  for (const f of files) {
    if (!f.endsWith(".py")) { files.delete(f); continue }
    const parts = f.split("/")
    if (parts.some(p => p.startsWith("test") || p === "tests")) files.delete(f)
  }

  // 4. git grep for long identifiers — backtick first, then plain snake_case/CamelCase words
  if (files.size === 0) {
    // Backtick identifiers are highest confidence
    const backtickIds = [...instance.problemStatement.matchAll(/`([A-Za-z_]\w{5,})`/g)]
      .map((m) => m[1])
    // Also try snake_case attribute/method names (2+ segments) from problem text
    const plainIds = [...instance.problemStatement.matchAll(/\b([a-z][a-z0-9]*(?:_[a-z0-9]+){1,})\b/g)]
      .map((m) => m[1])
      .filter((id) => id.length > 8)  // skip short generic words
    const identifiers = [...new Set([...backtickIds, ...plainIds])].slice(0, 5)
    // Infer module root from repo name (e.g. "astropy/astropy" → search in "astropy/")
    const repoModule = instance.repo.split("/")[1] ?? ""
    for (const id of identifiers) {
      const result = workspace.exec(
        `git grep -l "def ${id}\\|class ${id}\\|${id}" -- "${repoModule}/**/*.py" 2>/dev/null | grep -v test | head -3`
      )
      for (const line of result.stdout.split("\n").filter(Boolean)) {
        files.add(line.trim())
      }
    }
  }

  // Return at most 2 files — prefer smaller files to avoid token explosion
  return rankBySize(workspace, [...files]).slice(0, 2)
}

function rankBySize(workspace: Workspace, filePaths: string[]): string[] {
  return filePaths
    .map((p) => {
      const abs = joinPath(workspace.dir, p)
      try {
        const lines = readFileSync(abs, "utf8").split("\n").length
        return { p, lines }
      } catch {
        return { p, lines: Infinity }
      }
    })
    .filter((x) => x.lines < Infinity)
    .sort((a, b) => a.lines - b.lines)
    .map((x) => x.p)
}

// Extract symbol anchors for window-based file injection.
// Sources: backtick identifiers from problem statement + method names from FAIL_TO_PASS.
function extractAnchors(instance: BenchmarkInstance): string[] {
  const anchors = new Set<string>()
  // Backtick identifiers in problem statement
  for (const m of instance.problemStatement.matchAll(/`([A-Za-z_]\w{3,})`/g)) anchors.add(m[1])
  // Method/class names from FAIL_TO_PASS: "test_foo (module.TestClass)" → "test_foo", "TestClass"
  for (const t of instance.failToPass ?? []) {
    const m = t.match(/^(\w+)\s*\(([^)]+)\)/)
    if (m) {
      anchors.add(m[1])  // test method name
      const parts = m[2].split(".")
      anchors.add(parts[parts.length - 1])  // class name
    }
  }
  return [...anchors].slice(0, 8)
}

export async function runJingu(
  instance: BenchmarkInstance,
  workspaceBase: string,
  opts: { skipTestGate?: boolean; wsDir?: string; strategy?: SearchStrategy; maxAttempts?: number; baseline?: TestCounts } = {}
): Promise<InstanceRunResult> {
  const t0 = Date.now()
  console.log(`[jingu] ${instance.instanceId}`)

  const instanceSlug = instance.instanceId.replace(/\//g, "__")
  const wsDir = opts.wsDir ?? join(workspaceBase, instanceSlug)
  // Shared clone cache: workspaceBase/__cache__/<instanceSlug>
  // All strategy workspaces cp -r from this single clone — no repeated network transfers
  const cacheDir = join(workspaceBase, "__cache__", instanceSlug)
  let workspace: Workspace

  if (existsSync(join(wsDir, ".git"))) {
    // Already exists — reset to base commit
    workspace = new Workspace(wsDir)
    workspace.exec(`git checkout ${instance.baseCommit}`, { throws: true })
    workspace.reset()
    console.log(`  [jingu] workspace reused, reset to ${instance.baseCommit.slice(0, 8)}`)
  } else {
    // Clone once to cache, then cp -r to strategy workspace
    const repoUrl = `https://github.com/${instance.repo}.git`
    if (!existsSync(join(cacheDir, ".git"))) {
      console.log(`  [jingu] cloning ${repoUrl} ...`)
    } else {
      console.log(`  [jingu] cache hit, copying to workspace...`)
    }
    workspace = Workspace.checkoutFromCache(repoUrl, instance.baseCommit, wsDir, cacheDir)
    console.log(`  [jingu] checkout done @ ${instance.baseCommit.slice(0, 8)}`)
  }

  // Baseline test counts before any patch (shared across strategies when pre-computed)
  const baseline = opts.baseline ?? runTestsBaseline(workspace, TEST_CMD)
  if (!opts.baseline) {
    console.log(`  [jingu] baseline: passed=${baseline.passed} failed=${baseline.failed}`)
  }

  // Read relevant files from workspace to ground LLM in exact file content
  const candidateFiles = findCandidateFiles(instance, workspace)
  // Build symbol anchors from FAIL_TO_PASS test names + backtick identifiers in problem statement
  const anchors = extractAnchors(instance)
  const fileContents = readWorkspaceFiles(workspace, candidateFiles, anchors)
  if (Object.keys(fileContents).length > 0) {
    console.log(`  [jingu] injecting files: ${Object.keys(fileContents).join(", ")}`)
  }

  // Resolve strategy against runtime context (after file injection is known).
  // This handles the Layer A → Layer C dependency:
  //   if no files injected + strict-observed-only → downgrade to "standard"
  const strategyCtx = { injectedFiles: Object.keys(fileContents) }
  const rawStrategy = opts.strategy
  const { effectiveStrategy, status: resStatus, reason: resReason } =
    rawStrategy ? resolveStrategy(rawStrategy, strategyCtx) : { effectiveStrategy: undefined, status: "valid" as const, reason: undefined }
  if (resStatus === "degraded") {
    console.log(`  [proposer] strategy degraded: ${resReason} (${rawStrategy?.id})`)
  }

  const attempts: AttemptResult[] = []
  let previousFeedback: string | undefined
  let finalPatchText: string | undefined

  const strategyResolution = resStatus !== "valid" ? { status: resStatus, reason: resReason } : undefined

  const maxAttempts = opts.maxAttempts ?? MAX_ATTEMPTS
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    const candidate = await propose(instance, attempt, { previousFeedback, fileContents, strategy: effectiveStrategy ?? rawStrategy })

    // Gate 1: structural (pass injected files + filesTouched for grounding compliance check)
    const sg = structuralGate(candidate.patchText, strategyCtx.injectedFiles, candidate.filesTouched)
    if (sg.status === "fail") {
      const ar: AttemptResult = { attempt, candidate, structuralGate: sg, accepted: false, strategyResolution }
      attempts.push(ar)
      console.log(`  [jingu] attempt=${attempt} FAIL structural (${sg.code})`)
      previousFeedback = buildRetryFeedback(ar)
      continue
    }

    // Gate 2: apply
    const ag = applyGate(workspace, candidate.patchText)
    if (ag.status === "fail") {
      const ar: AttemptResult = { attempt, candidate, structuralGate: sg, applyGate: ag, accepted: false, strategyResolution }
      attempts.push(ar)
      console.log(`  [jingu] attempt=${attempt} FAIL apply (${ag.code})`)
      previousFeedback = buildRetryFeedback(ar, workspace)
      workspace.reset()
      continue
    }

    // Apply with the same fuzz level that passed the dry-run
    const fuzz = (ag.details?.apply_strictness === "fuzz") ? 5 : 0
    workspace.applyPatchForReal(candidate.patchText, fuzz)

    // Gate 3: test delta (use FAIL_TO_PASS ground truth when available)
    const tg = testGate(workspace, TEST_CMD, baseline, {
      skipIfNoBaseline: opts.skipTestGate,
      failToPass: instance.failToPass,
      repo: instance.repo,
    })
    workspace.reset() // always reset after test run

    if (tg.status === "fail") {
      const ar: AttemptResult = { attempt, candidate, structuralGate: sg, applyGate: ag, testGate: tg, accepted: false, strategyResolution }
      attempts.push(ar)
      console.log(`  [jingu] attempt=${attempt} FAIL test (${tg.code})`)
      previousFeedback = buildRetryFeedback(ar, workspace)
      continue
    }

    // All gates passed
    const ar: AttemptResult = { attempt, candidate, structuralGate: sg, applyGate: ag, testGate: tg, accepted: true, strategyResolution }
    attempts.push(ar)
    finalPatchText = candidate.patchText
    console.log(`  [jingu] attempt=${attempt} ACCEPTED`)
    break
  }

  const accepted = finalPatchText !== undefined

  return {
    instanceId: instance.instanceId,
    mode: "jingu",
    accepted,
    attempts,
    finalPatchText,
    durationMs: Date.now() - t0,
  }
}
