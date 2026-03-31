import type { BenchmarkInstance, InstanceRunResult, AttemptResult } from "../types/contracts.js"
import { propose } from "../proposer/proposer-adapter.js"
import { structuralGate } from "../admission/structural-gate.js"
import { applyGate } from "../admission/apply-gate.js"
import { testGate, runTestsBaseline } from "../admission/test-gate.js"
import { buildRetryFeedback } from "../admission/retry-feedback.js"
import { Workspace } from "../workspace/workspace.js"
import { join } from "node:path"
import { existsSync, readFileSync } from "node:fs"
import { join as joinPath } from "node:path"

const MAX_ATTEMPTS = 3
const TEST_CMD = "python -m pytest -x -q --tb=short 2>&1 || true"

const MAX_FILE_LINES = 600

function readWorkspaceFiles(workspace: Workspace, filePaths: string[]): Record<string, string> {
  const result: Record<string, string> = {}
  for (const p of filePaths) {
    const abs = joinPath(workspace.dir, p)
    if (existsSync(abs)) {
      try {
        const lines = readFileSync(abs, "utf8").split("\n")
        // Truncate large files — keep first MAX_FILE_LINES lines
        const content = lines.length > MAX_FILE_LINES
          ? lines.slice(0, MAX_FILE_LINES).join("\n") + `\n... (truncated at ${MAX_FILE_LINES} lines)`
          : lines.join("\n")
        result[p] = content
      } catch {
        // skip unreadable files
      }
    }
  }
  return result
}

// Find candidate files to inject into the prompt.
// 1. Explicit paths in hints_text
// 2. git grep for key identifiers mentioned in problem statement
function findCandidateFiles(instance: BenchmarkInstance, workspace: Workspace): string[] {
  const files = new Set<string>()

  // 1. Explicit .py paths in hints
  for (const m of (instance.hintsText ?? "").matchAll(/[\w/.+-]+\.py/g)) files.add(m[0])
  // Explicit paths in problem statement
  for (const m of instance.problemStatement.matchAll(/[\w/.+-]+\.py/g)) files.add(m[0])

  // 2. git grep for class/function names mentioned in backticks
  const identifiers = [...instance.problemStatement.matchAll(/`([A-Za-z_]\w*)`/g)]
    .map((m) => m[1])
    .filter((id) => id.length > 3)  // skip short tokens
    .slice(0, 5)  // limit searches

  for (const id of identifiers) {
    const result = workspace.exec(`git grep -l "def ${id}\\|class ${id}" -- "*.py" 2>/dev/null`)
    for (const line of result.stdout.split("\n").filter(Boolean)) {
      files.add(line.trim())
    }
  }

  return [...files].slice(0, 3)  // max 3 files to keep prompt size manageable
}

export async function runJingu(
  instance: BenchmarkInstance,
  workspaceBase: string
): Promise<InstanceRunResult> {
  const t0 = Date.now()
  console.log(`[jingu] ${instance.instanceId}`)

  const wsDir = join(workspaceBase, instance.instanceId.replace(/\//g, "__"))
  let workspace: Workspace

  if (existsSync(join(wsDir, ".git"))) {
    // Already cloned — reset to base commit
    workspace = new Workspace(wsDir)
    workspace.exec(`git checkout ${instance.baseCommit}`, { throws: true })
    workspace.reset()
    console.log(`  [jingu] workspace reused, reset to ${instance.baseCommit.slice(0, 8)}`)
  } else {
    // Clone from GitHub
    const repoUrl = `https://github.com/${instance.repo}.git`
    console.log(`  [jingu] cloning ${repoUrl} ...`)
    workspace = Workspace.checkout(repoUrl, instance.baseCommit, wsDir)
    console.log(`  [jingu] checkout done @ ${instance.baseCommit.slice(0, 8)}`)
  }

  // Baseline test counts before any patch
  const baseline = runTestsBaseline(workspace, TEST_CMD)
  console.log(`  [jingu] baseline: passed=${baseline.passed} failed=${baseline.failed}`)

  // Read relevant files from workspace to ground LLM in exact file content
  const candidateFiles = findCandidateFiles(instance, workspace)
  const fileContents = readWorkspaceFiles(workspace, candidateFiles)
  if (Object.keys(fileContents).length > 0) {
    console.log(`  [jingu] injecting files: ${Object.keys(fileContents).join(", ")}`)
  }

  const attempts: AttemptResult[] = []
  let previousFeedback: string | undefined
  let finalPatchText: string | undefined

  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    const candidate = await propose(instance, attempt, { previousFeedback, fileContents })

    // Gate 1: structural
    const sg = structuralGate(candidate.patchText)
    if (sg.status === "fail") {
      const ar: AttemptResult = { attempt, candidate, structuralGate: sg, accepted: false }
      attempts.push(ar)
      console.log(`  [jingu] attempt=${attempt} FAIL structural (${sg.code})`)
      previousFeedback = buildRetryFeedback(ar)
      continue
    }

    // Gate 2: apply
    const ag = applyGate(workspace, candidate.patchText)
    if (ag.status === "fail") {
      const ar: AttemptResult = { attempt, candidate, structuralGate: sg, applyGate: ag, accepted: false }
      attempts.push(ar)
      console.log(`  [jingu] attempt=${attempt} FAIL apply (${ag.code})`)
      previousFeedback = buildRetryFeedback(ar, workspace)
      workspace.reset()
      continue
    }

    // Actually apply the patch for the test gate
    workspace.applyPatchForReal(candidate.patchText)

    // Gate 3: test delta
    const tg = testGate(workspace, TEST_CMD, baseline)
    workspace.reset() // always reset after test run

    if (tg.status === "fail") {
      const ar: AttemptResult = { attempt, candidate, structuralGate: sg, applyGate: ag, testGate: tg, accepted: false }
      attempts.push(ar)
      console.log(`  [jingu] attempt=${attempt} FAIL test (${tg.code})`)
      previousFeedback = buildRetryFeedback(ar, workspace)
      continue
    }

    // All gates passed
    const ar: AttemptResult = { attempt, candidate, structuralGate: sg, applyGate: ag, testGate: tg, accepted: true }
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
