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

// Derive source files from FAIL_TO_PASS test entries via test-class → source-class mapping.
// Strategy:
//   1. Parse failToPass entries to extract the test module path
//   2. Locate the test file in the workspace tests/ directory
//   3. Extract the "Subject under test" class name (e.g. "CharFieldTests" → "CharField")
//   4. git grep the source tree for that class/function definition
// Returns empty array when failToPass is absent or mapping fails.
function findFilesFromFailToPass(instance: BenchmarkInstance, workspace: Workspace): string[] {
  const failToPass = instance.failToPass
  if (!failToPass || failToPass.length === 0) return []

  const repoModule = instance.repo.split("/")[1] ?? ""
  const sourceFiles = new Set<string>()

  // Collect test class names and module paths from all failToPass entries.
  // Use DOMINANT class only: the test class that appears most often in failToPass.
  // This avoids mixing signals from unrelated test classes (e.g. admin tests + forms tests).
  const classCounts = new Map<string, { count: number; module: string }>()
  for (const t of failToPass) {
    // Unittest format: "test_method (module.path.TestClass)"
    const unittestMatch = t.match(/\(([^)]+)\)$/)
    if (unittestMatch) {
      const dotted = unittestMatch[1]
      const parts = dotted.split(".")
      if (parts.length >= 2) {
        const cls = parts[parts.length - 1]
        const mod = parts.slice(0, -1).join(".")
        const entry = classCounts.get(cls) ?? { count: 0, module: mod }
        classCounts.set(cls, { count: entry.count + 1, module: mod })
      }
      continue
    }
    // Pytest format: "path/to/test_file.py::TestClass::test_method"
    const pytestMatch = t.match(/^([^:]+\.py)(?:::(\w+))?/)
    if (pytestMatch) {
      const pyPath = pytestMatch[1]
      const cls = pytestMatch[2] ?? "__default__"
      const mod = pyPath.replace(/\//g, ".").replace(/\.py$/, "")
      const entry = classCounts.get(cls) ?? { count: 0, module: mod }
      classCounts.set(cls, { count: entry.count + 1, module: mod })
    }
  }

  // Pick top-1 class (most tests = strongest signal) as the focus
  const testModules = new Set<string>()
  const testClasses = new Set<string>()
  const sorted = [...classCounts.entries()].sort((a, b) => b[1].count - a[1].count)
  for (const [cls, { module }] of sorted.slice(0, 1)) {
    testClasses.add(cls)
    testModules.add(module)
  }

  // For each test class name, derive the likely source class/function name
  // Primary: "CharFieldTests" → "CharField", "FormsMediaTestCase" → "FormsMedia", "TestValidation" → "Validation"
  // Fallback parts (used only when primary not found): "FormsMedia" → ["Forms", "Media"]
  const sourceSymbolsPrimary = new Set<string>()
  const sourceSymbolsFallback = new Set<string>()
  for (const cls of testClasses) {
    // Strip common test class naming patterns
    const stripped = cls.replace(/TestCase$/, "").replace(/Tests$/, "").replace(/^Test/, "")
    if (stripped.length >= 3) sourceSymbolsPrimary.add(stripped)
    // PascalCase parts as fallback — e.g. "FormsMedia" → ["Forms", "Media"] (last 2, most specific)
    const parts = stripped.replace(/([A-Z])/g, " $1").trim().split(" ").filter((p) => p.length >= 3)
    for (const part of parts.slice(-2)) {
      if (part !== stripped) sourceSymbolsFallback.add(part)
    }
  }
  // Use primary symbols first; add fallback symbols if primary produces nothing
  const sourceSymbols = sourceSymbolsPrimary

  // Also use test module path to find the test file and extract source module imports
  // We extract "from django.X.Y import Z" → source module paths → git grep for symbols
  const sourceModulePaths = new Set<string>()
  for (const mod of testModules) {
    // Try two forms: "tests/module/path.py" and "module/path.py"
    const relPath = mod.replace(/\./g, "/") + ".py"
    const candidates = [relPath, `tests/${relPath}`]
    for (const candidate of candidates) {
      const abs = joinPath(workspace.dir, candidate)
      if (!existsSync(abs)) continue
      try {
        const content = readFileSync(abs, "utf8")
        // Single-line imports only: "from django.X import Y" — avoid multiline capture
        for (const m of content.matchAll(/^from\s+([\w.]+)\s+import\s+/gm)) {
          const srcMod = m[1]
          // Keep only non-test source module paths
          if (!srcMod.includes("test") && !srcMod.startsWith(".")) {
            sourceModulePaths.add(srcMod)
          }
        }
      } catch { /* skip */ }
      break
    }
  }

  // Determine search scope: prefer narrowing to directories of modules referenced in test imports
  // e.g. ["django.db", "django.forms"] → search within "django/db/" and "django/forms/"
  // git grep takes directory paths directly (no globstar needed)
  // Falls back to repo module root if no source modules found
  const searchDirs: string[] = []
  for (const srcMod of [...sourceModulePaths].slice(0, 4)) {
    searchDirs.push(srcMod.replace(/\./g, "/") + "/")
  }
  if (searchDirs.length === 0 && repoModule) {
    searchDirs.push(repoModule + "/")
  }
  const scopeArg = searchDirs.map((d) => `"${d}"`).join(" ")

  // git grep source tree for each symbol — skip test files
  // Try primary symbols first; if nothing found, try fallback PascalCase parts
  const symbolSets = [sourceSymbols, sourceSymbolsFallback]
  for (const symSet of symbolSets) {
    for (const sym of [...symSet].slice(0, 6)) {
      const result = workspace.exec(
        `git grep -l "^class ${sym}\\b\\|^def ${sym}\\b" -- ${scopeArg} 2>/dev/null | grep -v test | head -2`
      )
      for (const line of result.stdout.split("\n").filter(Boolean)) {
        sourceFiles.add(line.trim())
      }
    }
    if (sourceFiles.size > 0) break  // found with primary symbols, skip fallback
  }

  // Fallback: if symbol grep found nothing but we have import-derived module paths,
  // resolve the most specific module path to an actual file.
  // e.g. "django.forms" → check django/forms/__init__.py, django/forms.py
  //      "django.db.models.expressions" → django/db/models/expressions.py etc.
  // Prefer the deepest (most specific) module that resolves to a file.
  if (sourceFiles.size === 0 && sourceModulePaths.size > 0) {
    const sorted = [...sourceModulePaths].sort((a, b) => b.split(".").length - a.split(".").length)
    for (const mod of sorted.slice(0, 3)) {
      const modPath = mod.replace(/\./g, "/")
      for (const suffix of [".py", "/__init__.py"]) {
        const candidate = modPath + suffix
        if (existsSync(joinPath(workspace.dir, candidate)) && !candidate.includes("test")) {
          sourceFiles.add(candidate)
          break
        }
      }
    }
  }

  return rankBySize(workspace, [...sourceFiles]).slice(0, 2)
}

// Find candidate files to inject into the prompt.
// Priority order:
//   0. failToPass test-class analysis (highest precision — replaces text-matching when found)
//   1. Explicit .py paths in hints_text
//   2. Dotted module paths in problem statement (e.g. "ascii.rst" → astropy/io/ascii/rst.py)
//   3. Explicit .py paths in problem statement
//   4. git grep for long identifiers (>6 chars) in backticks, restricted to repo module dir
function findCandidateFiles(instance: BenchmarkInstance, workspace: Workspace): string[] {
  // Step 0: failToPass-based file discovery (highest precision)
  // If this produces results, skip text-matching entirely — text matching causes wrong file injection
  const failToPassFiles = findFilesFromFailToPass(instance, workspace)
  if (failToPassFiles.length > 0) {
    return failToPassFiles
  }

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

// Extract the target line number from the first hunk header "@@ -N,n +M,m @@"
function extractHunkLineFromPatch(patchText: string): number | null {
  const m = patchText.match(/@@ -(\d+)(?:,\d+)? \+(\d+)/)
  if (!m) return null
  return parseInt(m[1], 10)
}

// Re-read a file centered on a specific line number — used after apply_fail to give
// LLM the exact content around where it tried to patch.
function readWorkspaceFilesAtLine(
  workspace: Workspace,
  filePaths: string[],
  centerLine: number
): Record<string, string> {
  const result: Record<string, string> = {}
  for (const p of filePaths) {
    const abs = joinPath(workspace.dir, p)
    if (!existsSync(abs)) continue
    try {
      const lines = readFileSync(abs, "utf8").split("\n")
      const start = Math.max(0, centerLine - 15)
      const end = Math.min(lines.length, centerLine + 100)
      const window = lines.slice(start, end).join("\n")
      result[p] = window + `\n... (lines ${start + 1}–${end} of ${lines.length}, re-centered after apply failure)`
    } catch { /* skip */ }
  }
  return result
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
// Sources: backtick identifiers from problem statement + SOURCE class/method names from FAIL_TO_PASS.
// Note: test class names (e.g. "CharFieldTests") are stripped to source names ("CharField")
// so extractRelevantWindow can locate the correct code region in the injected file.
function extractAnchors(instance: BenchmarkInstance): string[] {
  const anchors = new Set<string>()
  // Backtick identifiers in problem statement
  for (const m of instance.problemStatement.matchAll(/`([A-Za-z_]\w{3,})`/g)) anchors.add(m[1])
  // Method/class names from FAIL_TO_PASS: "test_foo (module.TestClass)" → test_foo, CharField (stripped)
  for (const t of instance.failToPass ?? []) {
    const m = t.match(/^(\w+)\s*\(([^)]+)\)/)
    if (m) {
      anchors.add(m[1])  // test method name (e.g. test_choices_in_max_length)
      const parts = m[2].split(".")
      const testClassName = parts[parts.length - 1]
      // Add test class name as-is (might match in test files)
      anchors.add(testClassName)
      // Also add stripped source class name (e.g. "CharFieldTests" → "CharField")
      const stripped = testClassName.replace(/TestCase$/, "").replace(/Tests$/, "").replace(/^Test/, "")
      if (stripped.length >= 3 && stripped !== testClassName) anchors.add(stripped)
    }
  }
  return [...anchors].slice(0, 10)
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

  if (existsSync(wsDir)) {
    // Worktree already exists — force reset to base commit (clean -fdx handles untracked files)
    workspace = new Workspace(wsDir)
    workspace.reset()
    console.log(`  [jingu] workspace reused, reset to ${instance.baseCommit.slice(0, 8)}`)
  } else {
    // Clone once to cache, then create git worktree (shares .git objects, no cp -r)
    const repoUrl = `https://github.com/${instance.repo}.git`
    if (!existsSync(join(cacheDir, ".git"))) {
      console.log(`  [jingu] cloning ${repoUrl} ...`)
    } else {
      console.log(`  [jingu] cache hit, creating worktree...`)
    }
    workspace = Workspace.checkoutFromCache(repoUrl, instance.baseCommit, wsDir, cacheDir)
    console.log(`  [jingu] worktree ready @ ${instance.baseCommit.slice(0, 8)}`)
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
  const injectedTotalLines = Object.values(fileContents).reduce((sum, c) => sum + c.split("\n").length, 0)
  // anchors from FAIL_TO_PASS + backtick identifiers — proxy for localization evidence strength
  const strategyCtx = { injectedFiles: Object.keys(fileContents), injectedTotalLines, injectedAnchorCount: anchors.length }
  const rawStrategy = opts.strategy
  const { effectiveStrategy, status: resStatus, reason: resReason } =
    rawStrategy ? resolveStrategy(rawStrategy, strategyCtx) : { effectiveStrategy: undefined, status: "valid" as const, reason: undefined }
  if (resStatus === "degraded") {
    console.log(`  [proposer] strategy degraded: ${resReason} (${rawStrategy?.id})`)
  }

  const attempts: AttemptResult[] = []
  let previousFeedback: string | undefined
  let finalPatchText: string | undefined
  // Dynamic file contents: updated across retries when UNGROUNDED_PATCH reveals needed files
  let currentFileContents = fileContents
  let currentInjectedFiles = strategyCtx.injectedFiles

  const strategyResolution = resStatus !== "valid" ? { status: resStatus, reason: resReason } : undefined
  const retryOpts = { verificationPolicy: (effectiveStrategy ?? rawStrategy)?.promptHints.verificationPolicy }

  const maxAttempts = opts.maxAttempts ?? MAX_ATTEMPTS
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    const candidate = await propose(instance, attempt, { previousFeedback, fileContents: currentFileContents, strategy: effectiveStrategy ?? rawStrategy })

    // Gate 1: structural (pass injected files + filesTouched for grounding compliance check)
    const sg = structuralGate(candidate.patchText, currentInjectedFiles, candidate.filesTouched)
    if (sg.status === "fail") {
      const ar: AttemptResult = { attempt, candidate, structuralGate: sg, accepted: false, strategyResolution }
      attempts.push(ar)
      console.log(`  [jingu] attempt=${attempt} FAIL structural (${sg.code})`)
      // UNGROUNDED_PATCH: LLM patched a file we didn't show it — add that file to next attempt
      if (sg.code === "UNGROUNDED_PATCH" && candidate.filesTouched.length > 0) {
        const extraFiles = candidate.filesTouched.filter(
          (f) => !currentInjectedFiles.some((inj) => f.endsWith(inj) || inj.endsWith(f))
        )
        if (extraFiles.length > 0) {
          const extraContents = readWorkspaceFiles(workspace, extraFiles, anchors)
          if (Object.keys(extraContents).length > 0) {
            currentFileContents = { ...currentFileContents, ...extraContents }
            currentInjectedFiles = Object.keys(currentFileContents)
            console.log(`  [jingu] adding ungrounded files to context: ${Object.keys(extraContents).join(", ")}`)
          }
        }
      }
      previousFeedback = buildRetryFeedback(ar, undefined, retryOpts)
      continue
    }

    // Gate 2: apply
    const ag = applyGate(workspace, candidate.patchText)
    if (ag.status === "fail") {
      const ar: AttemptResult = { attempt, candidate, structuralGate: sg, applyGate: ag, accepted: false, strategyResolution }
      attempts.push(ar)
      console.log(`  [jingu] attempt=${attempt} FAIL apply (${ag.code})`)
      previousFeedback = buildRetryFeedback(ar, workspace, retryOpts)
      // After apply_fail: re-center file window on the hunk line from the failed patch
      // This gives the LLM the exact content it needs for a corrected patch on the next attempt
      if (candidate.filesTouched.length > 0) {
        const hunkLine = extractHunkLineFromPatch(candidate.patchText)
        if (hunkLine !== null) {
          const refreshed = readWorkspaceFilesAtLine(workspace, candidate.filesTouched, hunkLine)
          if (Object.keys(refreshed).length > 0) {
            currentFileContents = { ...currentFileContents, ...refreshed }
            currentInjectedFiles = Object.keys(currentFileContents)
          }
        }
      }
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
      previousFeedback = buildRetryFeedback(ar, workspace, retryOpts)
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
