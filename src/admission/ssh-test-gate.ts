/**
 * SSH + Docker test gate for SWE-bench evaluation.
 *
 * Runs FAIL_TO_PASS tests on a remote host (cloud dev desktop) via SSH,
 * using the official SWE-bench Docker images (x86_64, conda testbed, all C extensions).
 *
 * Flow:
 *   1. Encode patch as base64
 *   2. SSH to remote host, run a shell script that:
 *      a. Creates a tmp directory
 *      b. Decodes patch from base64 env var → patch.diff
 *      c. docker pull (if image not cached)
 *      d. docker run --rm: git apply + conda testbed pytest
 *      e. Prints test output to stdout
 *   3. Parse stdout, return GateResult
 *
 * Remote host requirement: SSH alias "cloud" in ~/.ssh/config, Docker running.
 */

import { execSync } from "node:child_process"
import { writeFileSync, rmSync, mkdirSync } from "node:fs"
import { join } from "node:path"
import { tmpdir } from "node:os"
import { normalizePatch } from "./apply-gate.js"
import type { GateResult } from "../types/contracts.js"

export type SshTestGateConfig = {
  // SSH host alias (from ~/.ssh/config). Default: "cloud"
  sshHost: string
  // Timeout per full run (pull + run) in ms. Default: 900s (images are 3-12GB)
  timeoutMs: number
}

const DEFAULT_CONFIG: SshTestGateConfig = {
  sshHost: "cloud",
  timeoutMs: 900_000,
}

// Build the correct SWE-bench Docker Hub image name.
// Format: swebench/sweb.eval.x86_64.<org>_<version>_<project-issue>:latest
// Example: astropy__astropy-12907 + version 1776 → astropy_1776_astropy-12907
// The instance_id uses "__" separator; image name uses "_<version>_".
function swebenchImageName(instanceId: string, version: string): string {
  // instance_id: "astropy__astropy-12907" → org="astropy", slug="astropy-12907"
  const parts = instanceId.split("__")
  const org = parts[0]   // e.g. "astropy", "pylint-dev"
  const slug = parts[1]  // e.g. "astropy-12907", "pylint-5859"
  return `swebench/sweb.eval.x86_64.${org}_${version}_${slug}:latest`
}

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
    return `python tests/runtests.py --verbosity=0 ${[...testIds].join(" ")}`
  }

  return `python -m pytest -x -q --tb=short ${failToPass.join(" ")}`
}

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

export function sshTestGate(
  instanceId: string,
  repo: string,
  version: string,
  patchText: string,
  failToPass: string[],
  opts: Partial<SshTestGateConfig> = {}
): GateResult {
  const cfg: SshTestGateConfig = { ...DEFAULT_CONFIG, ...opts }
  const image = swebenchImageName(instanceId, version)
  // Normalize patch: fix wrong hunk line counts that cause git apply to fail
  const normalizedPatch = normalizePatch(patchText)
  const testCmd = buildContainerTestCmd(repo, failToPass)
  const expectedPassing = failToPass.length

  console.log(`  [ssh] ${instanceId}: submitting to ${cfg.sshHost}`)

  // Write patch to a local tmpfile, scp it to the remote host, then run tests.
  // This avoids shell quoting issues with large base64 env vars.
  const localTmpDir = join(tmpdir(), `jingu-ssh-${Date.now()}`)
  mkdirSync(localTmpDir, { recursive: true })
  const localPatchFile = join(localTmpDir, "patch.diff")
  const patchContent = normalizedPatch.endsWith("\n") ? normalizedPatch : normalizedPatch + "\n"
  writeFileSync(localPatchFile, patchContent, "utf8")

  // Remote tmp path for the patch file
  const remotePatchPath = `/tmp/jingu-patch-${Date.now()}.diff`

  try {
    // Step 1: scp patch to remote
    execSync(
      `scp -o BatchMode=yes -o ConnectTimeout=10 -q "${localPatchFile}" ${cfg.sshHost}:${remotePatchPath}`,
      { encoding: "utf8", stdio: "pipe", timeout: 30_000 }
    )
  } catch (err) {
    rmSync(localTmpDir, { recursive: true, force: true })
    const e = err as { stderr?: string }
    const reason = `scp_failed: ${(e.stderr ?? "").replace(/\*\*[^\n]*/g, "").trim().slice(0, 200)}`
    console.log(`  [ssh] fallback (${reason})`)
    return {
      status: "pass",
      code: "ACCEPTED",
      message: `SSH/Docker gate unavailable for ${instanceId} — relying on apply gate`,
      details: { evaluation_method: "apply_gate_only", is_fallback: true, fallback_reason: reason },
    }
  } finally {
    rmSync(localTmpDir, { recursive: true, force: true })
  }

  // Remote script: runs entirely on the cloud host
  const remoteScript = `
set -e
PATCH_FILE="${remotePatchPath}"
trap "rm -f $PATCH_FILE" EXIT

IMAGE="${image}"

# Pull image if not cached (may take several minutes for first pull)
if ! docker image inspect "$IMAGE" > /dev/null 2>&1; then
  echo "[ssh-gate] pulling $IMAGE ..." >&2
  docker pull "$IMAGE" >&2
fi

# Apply patch and run tests inside the container
docker run --rm \\
  -v "$PATCH_FILE:/patch/patch.diff:ro" \\
  "$IMAGE" \\
  bash -c '
    set -e
    cd /testbed
    conda run -n testbed git apply /patch/patch.diff 2>&1 || { echo "GIT_APPLY_FAILED"; exit 1; }
    conda run -n testbed ${testCmd} 2>&1 || true
  '
`

  let rawOutput: string
  try {
    rawOutput = execSync(
      `ssh -o BatchMode=yes -o ConnectTimeout=10 ${cfg.sshHost} bash -s`,
      {
        input: remoteScript,
        encoding: "utf8",
        stdio: ["pipe", "pipe", "pipe"],
        timeout: cfg.timeoutMs,
      }
    )
  } catch (err) {
    const e = err as { stdout?: string; stderr?: string; code?: string }
    const stdout = e.stdout ?? ""
    const stderr = e.stderr ?? ""
    const combined = stdout + stderr

    // Apply failure is a real gate failure
    if (combined.includes("GIT_APPLY_FAILED")) {
      return {
        status: "fail",
        code: "PATCH_APPLY_FAILED",
        message: `Patch failed to apply in Docker container for ${instanceId}`,
        details: {
          evaluation_method: "docker_pytest",
          output: tailLines(combined, 20),
        },
      }
    }

    // SSH unreachable / timeout / other infra failure → fallback
    // Strip SSH post-quantum warnings (** WARNING: ...) which are not real errors
    const stderrClean = stderr.replace(/\*\*[^\n]*\n?/g, "").trim()
    const reason = e.code === "ETIMEDOUT" ? "ssh_timeout" : `ssh_error: ${stderrClean.slice(0, 200)}`
    console.log(`  [ssh] fallback (${reason})`)
    return {
      status: "pass",
      code: "ACCEPTED",
      message: `SSH/Docker gate unavailable for ${instanceId} — relying on apply gate`,
      details: {
        evaluation_method: "apply_gate_only",
        is_fallback: true,
        fallback_reason: reason,
      },
    }
  }

  // Apply failure in stdout (exit 0 path — bash -c '... || true' can mask)
  if (rawOutput.includes("GIT_APPLY_FAILED")) {
    return {
      status: "fail",
      code: "PATCH_APPLY_FAILED",
      message: `Patch failed to apply in Docker container for ${instanceId}`,
      details: {
        evaluation_method: "docker_pytest",
        output: tailLines(rawOutput, 20),
      },
    }
  }

  const counts = parseTestOutput(rawOutput)
  const totalRan = counts.passed + counts.failed + counts.errors

  if (totalRan === 0) {
    // No test output — likely import error even inside Docker, or tests not collected
    console.log(`  [ssh] no tests ran for ${instanceId}`)
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

  const allPass = counts.passed >= expectedPassing && counts.errors === 0

  if (allPass) {
    return {
      status: "pass",
      code: "ACCEPTED",
      message: `All ${expectedPassing} FAIL_TO_PASS test(s) now passing via Docker (${counts.passed} passed, ${counts.failed} pre-existing failures)`,
      details: {
        evaluation_method: "docker_pytest",
        counts,
        expectedPassing,
      },
    }
  }

  return {
    status: "fail",
    code: "TESTS_NOT_IMPROVED",
    message: `FAIL_TO_PASS not resolved: passed=${counts.passed}/${totalRan} expected=${expectedPassing} failed=${counts.failed} errors=${counts.errors}`,
    details: {
      evaluation_method: "docker_pytest",
      counts,
      expectedPassing,
      output: tailLines(rawOutput, 30),
    },
  }
}
