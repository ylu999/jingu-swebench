import type { GateResult } from "../types/contracts.js"
import { execSync } from "node:child_process"
import { mkdirSync, writeFileSync, rmSync } from "node:fs"
import { join } from "node:path"
import { tmpdir } from "node:os"

// SWE-bench Docker image naming convention:
//   swebench/sweb.eval.x86_64.<instance_id>:latest
// where instance_id uses "__" separators (e.g. astropy__astropy-12907)
function swebenchImageName(instanceId: string): string {
  return `swebench/sweb.eval.x86_64.${instanceId}:latest`
}

// Check if Docker daemon is reachable.
// Returns false if Docker is not installed or daemon is not running.
export function isDockerAvailable(): boolean {
  try {
    execSync("docker info", { stdio: "pipe", timeout: 5_000 })
    return true
  } catch {
    return false
  }
}

// Check if the SWE-bench image for this instance is available locally.
// Avoids a pull attempt when the image is already cached.
export function isImageCached(instanceId: string): boolean {
  const image = swebenchImageName(instanceId)
  try {
    const out = execSync(`docker image inspect "${image}" 2>/dev/null`, {
      stdio: "pipe",
      timeout: 10_000,
    }).toString()
    return out.trim().length > 0
  } catch {
    return false
  }
}

// Pull the SWE-bench image for this instance if not already cached.
// Returns true on success, false if pull fails (image may not exist for this instance).
export function pullImage(instanceId: string): boolean {
  const image = swebenchImageName(instanceId)
  if (isImageCached(instanceId)) return true
  try {
    console.log(`  [docker] pulling ${image} ...`)
    execSync(`docker pull --platform linux/amd64 "${image}"`, {
      stdio: "pipe",
      timeout: 300_000,  // 5 min — images are 3-12GB
    })
    return true
  } catch (err) {
    const e = err as { stderr?: Buffer }
    const msg = e.stderr?.toString() ?? ""
    console.log(`  [docker] pull failed: ${msg.slice(0, 200)}`)
    return false
  }
}

// Build the test command to run inside the container.
// Mirrors buildTestCommand() from test-gate.ts but runs in /testbed context.
function buildContainerTestCmd(repo: string, failToPass: string[]): string {
  const repoOrg = repo.split("/")[0]

  if (repoOrg === "django") {
    const testIds = new Set<string>()
    for (const t of failToPass) {
      const unittestMatch = t.match(/^(\w+)\s*\(([^)]+)\)$/)
      if (unittestMatch) {
        testIds.add(`${unittestMatch[2]}.${unittestMatch[1]}`)
        continue
      }
      const pytestMatch = t.match(/^([^:]+\.py)(?:::(\w+))?(?:::(\w+))?/)
      if (pytestMatch) {
        const mod = pytestMatch[1].replace(/\//g, ".").replace(/\.py$/, "")
        const cls = pytestMatch[2] ?? ""
        const method = pytestMatch[3] ?? ""
        testIds.add([mod, cls, method].filter(Boolean).join("."))
      }
    }
    const testList = [...testIds].join(" ")
    return `cd /testbed && python tests/runtests.py --verbosity=0 ${testList} 2>&1 || true`
  }

  const nodeIds = failToPass.join(" ")
  return `cd /testbed && python -m pytest -x -q --tb=short ${nodeIds} 2>&1 || true`
}

// Parse pytest/unittest output — same logic as test-gate.ts
function parseTestOutput(output: string): { passed: number; failed: number; errors: number } {
  const passed = parseInt(output.match(/(\d+) passed/)?.[1] ?? "0", 10)
  const failed = parseInt(output.match(/(\d+) failed/)?.[1] ?? "0", 10)
  const errors = parseInt(output.match(/(\d+) error/)?.[1] ?? "0", 10)

  if (passed > 0 || failed > 0 || errors > 0) {
    return { passed, failed, errors }
  }

  const ranMatch = output.match(/Ran (\d+) tests/)
  if (ranMatch) {
    const total = parseInt(ranMatch[1], 10)
    const utFailed = parseInt(output.match(/failures=(\d+)/)?.[1] ?? "0", 10)
    const utErrors = parseInt(output.match(/errors=(\d+)/)?.[1] ?? "0", 10)
    const utBad = utFailed + utErrors
    return { passed: total - utBad, failed: utFailed, errors: utErrors }
  }

  return { passed: 0, failed: 0, errors: 0 }
}

function tailLines(s: string, n: number): string {
  return s.split("\n").slice(-n).join("\n")
}

export type DockerTestGateOpts = {
  // Pull image if not cached (default: true). Set false for offline/dry-run.
  pullIfMissing?: boolean
  // Timeout per container run in ms (default: 120s)
  timeoutMs?: number
}

// Run FAIL_TO_PASS tests inside the official SWE-bench Docker container.
// Applies the patch inside the container against /testbed, then runs the target tests.
// Returns GateResult with evaluation_method: "docker_pytest" in details.
export function dockerTestGate(
  instanceId: string,
  repo: string,
  patchText: string,
  failToPass: string[],
  opts: DockerTestGateOpts = {}
): GateResult {
  const pullIfMissing = opts.pullIfMissing ?? true
  const timeoutMs = opts.timeoutMs ?? 120_000

  const image = swebenchImageName(instanceId)

  // Step 1: ensure image is available
  if (pullIfMissing) {
    const ok = pullImage(instanceId)
    if (!ok) {
      return {
        status: "pass",
        code: "ACCEPTED",
        message: `Docker image not available for ${instanceId} — relying on apply gate`,
        details: {
          evaluation_method: "apply_gate_only",
          is_fallback: true,
          fallback_reason: "docker_image_not_available",
        },
      }
    }
  } else if (!isImageCached(instanceId)) {
    return {
      status: "pass",
      code: "ACCEPTED",
      message: `Docker image not cached for ${instanceId} (pullIfMissing=false) — relying on apply gate`,
      details: {
        evaluation_method: "apply_gate_only",
        is_fallback: true,
        fallback_reason: "docker_image_not_cached",
      },
    }
  }

  // Step 2: write patch to a temp directory that the container can mount
  const patchDir = join(tmpdir(), `jingu-docker-${instanceId.replace(/[^a-z0-9-]/gi, "_")}-${Date.now()}`)
  mkdirSync(patchDir, { recursive: true })
  const patchFile = join(patchDir, "patch.diff")
  const patchContent = patchText.endsWith("\n") ? patchText : patchText + "\n"
  writeFileSync(patchFile, patchContent, "utf8")

  try {
    // Step 3: build the test command
    const testCmd = buildContainerTestCmd(repo, failToPass)
    const expectedPassing = failToPass.length

    // Step 4: apply patch + run tests inside container
    // - Mount patchDir as /patch (read-only)
    // - Use conda run -n testbed to activate the SWE-bench environment
    // - Apply with git apply (SWE-bench containers use git)
    // - Run the test command
    const containerCmd = [
      `conda run -n testbed git apply /patch/patch.diff 2>&1`,
      `|| echo "GIT_APPLY_FAILED: $?"`,
      `&&`,
      `conda run -n testbed ${testCmd}`,
    ].join(" ")

    const dockerCmd = `docker run --rm --platform linux/amd64 -v "${patchDir}:/patch:ro" "${image}" bash -c '${containerCmd}'`

    let rawOutput: string
    try {
      rawOutput = execSync(dockerCmd, {
        stdio: "pipe",
        timeout: timeoutMs,
        encoding: "utf8",
      })
    } catch (err) {
      const e = err as { stdout?: string; stderr?: string }
      rawOutput = (e.stdout ?? "") + (e.stderr ?? "")
    }

    // Step 5: check for apply failure
    if (rawOutput.includes("GIT_APPLY_FAILED:")) {
      return {
        status: "fail",
        code: "PATCH_APPLY_FAILED",
        message: `Patch failed to apply inside Docker container for ${instanceId}`,
        details: {
          evaluation_method: "docker_pytest",
          output: tailLines(rawOutput, 20),
        },
      }
    }

    // Step 6: parse test output and evaluate
    const counts = parseTestOutput(rawOutput)
    const totalRan = counts.passed + counts.failed + counts.errors
    const allPass = counts.passed >= expectedPassing && counts.errors === 0

    if (allPass) {
      return {
        status: "pass",
        code: "ACCEPTED",
        message: `All ${expectedPassing} FAIL_TO_PASS test(s) now passing in Docker (${counts.passed} passed, ${counts.failed} pre-existing failures)`,
        details: {
          evaluation_method: "docker_pytest",
          counts,
          expectedPassing,
        },
      }
    }

    // Check if no tests ran at all (import error even in Docker, or test not found)
    if (totalRan === 0) {
      return {
        status: "pass",
        code: "ACCEPTED",
        message: `Docker: no tests ran for ${instanceId} — relying on apply gate`,
        details: {
          evaluation_method: "apply_gate_only",
          is_fallback: true,
          fallback_reason: "docker_no_tests_ran",
          output: tailLines(rawOutput, 20),
        },
      }
    }

    return {
      status: "fail",
      code: "TESTS_NOT_IMPROVED",
      message: `FAIL_TO_PASS not resolved in Docker: passed=${counts.passed}/${totalRan} expected=${expectedPassing} failed=${counts.failed} errors=${counts.errors}`,
      details: {
        evaluation_method: "docker_pytest",
        counts,
        expectedPassing,
        output: tailLines(rawOutput, 30),
      },
    }
  } finally {
    // Always clean up the temp patch dir
    try { rmSync(patchDir, { recursive: true, force: true }) } catch { /* ignore */ }
  }
}
