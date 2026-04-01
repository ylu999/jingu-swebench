import type { GateResult } from "../types/contracts.js"
import type { Workspace } from "../workspace/workspace.js"

export interface TestCounts {
  passed: number
  failed: number
  errors: number
}

// Parse pytest format: "3 passed, 2 failed, 1 error"
// Parse unittest format: "Ran 22 tests in 0.025s\nOK" or "FAILED (failures=2, errors=1)"
function parseTestOutput(output: string): TestCounts {
  // pytest
  const passed = parseInt(output.match(/(\d+) passed/)?.[1] ?? "0", 10)
  const failed = parseInt(output.match(/(\d+) failed/)?.[1] ?? "0", 10)
  const errors = parseInt(output.match(/(\d+) error/)?.[1] ?? "0", 10)

  if (passed > 0 || failed > 0 || errors > 0) {
    return { passed, failed, errors }
  }

  // unittest: "Ran N tests" + "OK" | "FAILED (failures=N)"
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

// Derive a precise test command from FAIL_TO_PASS test IDs.
// Django uses tests/runtests.py <module> ...
// Other repos fall back to pytest with specific test node ids.
function buildTestCommand(repo: string, failToPass: string[]): string {
  const repoOrg = repo.split("/")[0]  // e.g. "django"

  if (repoOrg === "django") {
    // failToPass entries are either:
    //   "test_foo (app.tests.TestClass)"  → unittest style → module = "app.tests"
    //   "app/tests/test_foo.py::TestClass::test_foo"  → pytest style
    const modules = new Set<string>()
    for (const t of failToPass) {
      // unittest style: "test_name (module.path.TestClass)"
      const unittestMatch = t.match(/\(([^)]+)\)$/)
      if (unittestMatch) {
        // strip last segment (class name) → module
        const parts = unittestMatch[1].split(".")
        modules.add(parts.slice(0, -1).join("."))
        continue
      }
      // pytest style: "path/to/test.py::Class::method"
      const pytestMatch = t.match(/^([^:]+\.py)/)
      if (pytestMatch) {
        // convert path to module: "forms_tests/tests/test_media.py" → "forms_tests.tests.test_media"
        modules.add(pytestMatch[1].replace(/\//g, ".").replace(/\.py$/, ""))
      }
    }
    const moduleList = [...modules].join(" ")
    return `python tests/runtests.py --verbosity=0 ${moduleList} 2>&1 || true`
  }

  // Generic: use pytest with explicit node ids
  const nodeIds = failToPass.join(" ")
  return `python -m pytest -x -q --tb=short ${nodeIds} 2>&1 || true`
}

export function testGate(
  workspace: Workspace,
  _testCmd: string,  // kept for API compat, overridden when failToPass available
  beforeCounts: TestCounts,
  opts: { skipIfNoBaseline?: boolean; failToPass?: string[]; repo?: string } = {}
): GateResult {
  // If failToPass is provided, we have a reliable test command — baseline=0/0 is OK
  // (means the failing tests haven't passed yet, which is expected before the patch)
  const hasGroundTruth = opts.failToPass && opts.failToPass.length > 0

  if (!hasGroundTruth && beforeCounts.passed === 0 && beforeCounts.failed === 0) {
    if (opts.skipIfNoBaseline) {
      return {
        status: "pass",
        code: "ACCEPTED",
        message: "Test gate skipped (--skip-test-gate): no pytest baseline available",
        details: { skipped: true },
      }
    }
    return {
      status: "fail",
      code: "TEST_HARNESS_UNAVAILABLE",
      message: "No test baseline — pytest environment not available or FAIL_TO_PASS not loaded. Use --skip-test-gate for smoke runs.",
      details: { skipped: false },
    }
  }

  // Build the actual test command
  const testCmd = hasGroundTruth
    ? buildTestCommand(opts.repo ?? "", opts.failToPass!)
    : _testCmd

  const result = workspace.exec(testCmd)

  if (result.exitCode !== 0 && result.stdout.trim() === "" && result.stderr.trim() === "") {
    return {
      status: "fail",
      code: "TEST_EXEC_FAILED",
      message: `Test command failed to execute: ${testCmd}`,
      details: { stderr: result.stderr.trim() },
    }
  }

  const after = parseTestOutput(result.stdout + result.stderr)

  if (hasGroundTruth) {
    // Ground-truth mode: all FAIL_TO_PASS tests must now pass
    // "passed" count must equal total FAIL_TO_PASS tests, no failures/errors
    const expectedPassing = opts.failToPass!.length
    const allPass = after.passed >= expectedPassing && after.failed === 0 && after.errors === 0
    if (allPass) {
      return {
        status: "pass",
        code: "ACCEPTED",
        message: `All ${expectedPassing} FAIL_TO_PASS test(s) now passing`,
        details: { after, expectedPassing },
      }
    }
    return {
      status: "fail",
      code: "TESTS_NOT_IMPROVED",
      message: `FAIL_TO_PASS not fully resolved: passed=${after.passed} failed=${after.failed} errors=${after.errors} (expected ${expectedPassing} passing)`,
      details: { after, expectedPassing, output: tailLines(result.stdout + result.stderr, 30) },
    }
  }

  // Fallback: delta mode (no ground truth)
  const newlyPassing = Math.max(0, beforeCounts.failed - after.failed)
  const newlyFailing = Math.max(0, after.failed - beforeCounts.failed)

  if (newlyFailing > 0) {
    return {
      status: "fail",
      code: "TESTS_NOT_IMPROVED",
      message: `Patch introduced ${newlyFailing} regression(s)`,
      details: { before: beforeCounts, after, newlyPassing, newlyFailing, output: tailLines(result.stdout + result.stderr, 30) },
    }
  }

  if (newlyPassing === 0) {
    return {
      status: "fail",
      code: "TESTS_NOT_IMPROVED",
      message: "No previously-failing tests moved to passing",
      details: { before: beforeCounts, after, output: tailLines(result.stdout + result.stderr, 30) },
    }
  }

  return {
    status: "pass",
    code: "ACCEPTED",
    message: `${newlyPassing} test(s) now passing, no regressions`,
    details: { before: beforeCounts, after },
  }
}

export function runTestsBaseline(workspace: Workspace, testCmd: string): TestCounts {
  const result = workspace.exec(testCmd)
  return parseTestOutput(result.stdout + result.stderr)
}

function tailLines(s: string, n: number): string {
  return s.split("\n").slice(-n).join("\n")
}
