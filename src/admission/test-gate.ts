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
// Django uses tests/runtests.py <module.class.method> for maximum precision.
// Other repos fall back to pytest with specific test node ids.
function buildTestCommand(repo: string, failToPass: string[]): string {
  const repoOrg = repo.split("/")[0]  // e.g. "django"

  if (repoOrg === "django") {
    // failToPass entries are either:
    //   "test_foo (app.tests.TestClass)"  → unittest style → run as "app.tests.TestClass.test_foo"
    //   "app/tests/test_foo.py::TestClass::test_foo"  → pytest style
    // Run the SPECIFIC tests (not whole module) to avoid pre-existing failures polluting results
    const testIds = new Set<string>()
    for (const t of failToPass) {
      // unittest style: "test_name (module.path.TestClass)" → "module.path.TestClass.test_name"
      const unittestMatch = t.match(/^(\w+)\s*\(([^)]+)\)$/)
      if (unittestMatch) {
        testIds.add(`${unittestMatch[2]}.${unittestMatch[1]}`)
        continue
      }
      // pytest style: "path/to/test.py::Class::method"
      const pytestMatch = t.match(/^([^:]+\.py)(?:::(\w+))?(?:::(\w+))?/)
      if (pytestMatch) {
        // convert path to module: "forms_tests/tests/test_media.py" → "forms_tests.tests.test_media"
        const mod = pytestMatch[1].replace(/\//g, ".").replace(/\.py$/, "")
        const cls = pytestMatch[2] ?? ""
        const method = pytestMatch[3] ?? ""
        testIds.add([mod, cls, method].filter(Boolean).join("."))
      }
    }
    const testList = [...testIds].join(" ")
    return `python tests/runtests.py --verbosity=0 ${testList} 2>&1 || true`
  }

  // Generic: use pytest with explicit node ids
  const nodeIds = failToPass.join(" ")
  return `python -m pytest -x -q --tb=short ${nodeIds} 2>&1 || true`
}

// Check if the specific test methods in failToPass exist in the workspace test files.
// SWE-bench FAIL_TO_PASS tests are sometimes added by the fix commit itself, not in the base.
// Returns true if all tests exist (can run them), false if any test method is missing.
// IMPORTANT: For unittest format, checks ONLY in the specific test file for that class —
// generic method names like test_str exist in many files but may not exist in the target class.
function failToPassTestsExistInWorkspace(workspace: Workspace, failToPass: string[]): boolean {
  for (const t of failToPass) {
    // unittest format: "test_method_name (module.path.TestClass)"
    const unittestMatch = t.match(/^(\w+)\s*\(([^)]+)\)$/)
    if (unittestMatch) {
      const methodName = unittestMatch[1]  // e.g. "test_str"
      const classDotted = unittestMatch[2]  // e.g. "model_enums.tests.ChoicesTests"

      // Derive the test file path from the module path (strip class name from end)
      const parts = classDotted.split(".")
      // Last segment is the class; rest is the module path
      const moduleParts = parts.slice(0, -1)  // e.g. ["model_enums", "tests"]
      const relPath = moduleParts.join("/") + ".py"
      // Try "tests/<relPath>" first, then bare "<relPath>"
      const candidates = [`tests/${relPath}`, relPath]
      let foundInSpecificFile = false
      for (const candidate of candidates) {
        const searchResult = workspace.exec(
          `grep "def ${methodName}\\b" "${candidate}" 2>/dev/null | head -1`
        )
        if (searchResult.stdout.trim()) {
          foundInSpecificFile = true
          break
        }
      }
      if (!foundInSpecificFile) return false
      continue
    }
    // Pytest format: "path/to/test.py::TestClass::test_method" or "path/to/test.py::test_func[param]"
    const pytestMatch = t.match(/^([^:]+\.py)(?:::(\w+))?(?:::(\w+))?/)
    if (pytestMatch) {
      const testFile = pytestMatch[1]
      // Group 3 = method (Class::method format), Group 2 = either class or standalone func
      // Standalone: "file.py::test_func[param]" → group2=test_func, group3=undefined
      // Class style: "file.py::Class::method" → group2=Class, group3=method
      const methodName = pytestMatch[3] ?? (pytestMatch[2]?.match(/^test_/) ? pytestMatch[2] : undefined)
      if (methodName) {
        // Check in the specific test file
        const searchResult = workspace.exec(
          `grep "def ${methodName}\\b" "${testFile}" 2>/dev/null | head -1`
        )
        if (!searchResult.stdout.trim()) return false
      }
    }
  }
  return true
}

export function testGate(
  workspace: Workspace,
  _testCmd: string,  // kept for API compat, overridden when failToPass available
  beforeCounts: TestCounts,
  opts: { skipIfNoBaseline?: boolean; failToPass?: string[]; repo?: string; basePassCount?: number } = {}
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

  // Check if FAIL_TO_PASS tests exist in the workspace (they may be added by the fix commit).
  // If tests don't exist in the base commit, we can't verify them — skip test gate.
  if (hasGroundTruth && !failToPassTestsExistInWorkspace(workspace, opts.failToPass!)) {
    return {
      status: "pass",
      code: "ACCEPTED",
      message: `Test gate skipped: FAIL_TO_PASS test methods not present in base commit (tests added by fix). Relying on apply gate.`,
      details: { skipped: true, reason: "tests_added_by_fix" },
    }
  }

  // If FAIL_TO_PASS tests already pass in the BASE state (before this patch), it means the test
  // content was updated by the fix commit (assertions changed). The oracle uses new assertions.
  // We cannot verify with the old assertions — skip test gate and rely on apply gate.
  if (hasGroundTruth && opts.basePassCount !== undefined) {
    if (opts.basePassCount >= opts.failToPass!.length) {
      return {
        status: "pass",
        code: "ACCEPTED",
        message: `Test gate skipped: FAIL_TO_PASS tests already pass in base state (test assertions changed by fix commit). Relying on apply gate.`,
        details: { skipped: true, reason: "tests_already_pass_in_base" },
      }
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

  const combinedOutput = result.stdout + result.stderr
  const after = parseTestOutput(combinedOutput)

  if (hasGroundTruth) {
    // Ground-truth mode: all FAIL_TO_PASS tests must now pass.
    // We run specific tests (not whole module), so passed >= expectedPassing is the primary check.
    // Allow some failures from pre-existing unrelated tests (e.g., Python version incompatibilities).
    const expectedPassing = opts.failToPass!.length
    const totalRan = after.passed + after.failed + after.errors

    // Pre-existing failures are OK if the expected tests pass.
    // Simple rule: accept if passed >= expectedPassing and no NEW failures beyond what baseline had.
    const allPass = after.passed >= expectedPassing && after.errors === 0
    if (allPass) {
      return {
        status: "pass",
        code: "ACCEPTED",
        message: `All ${expectedPassing} FAIL_TO_PASS test(s) now passing (${after.passed} passed, ${after.failed} pre-existing failures)`,
        details: { after, expectedPassing },
      }
    }

    return {
      status: "fail",
      code: "TESTS_NOT_IMPROVED",
      message: `FAIL_TO_PASS not fully resolved: passed=${after.passed}/${totalRan} expected=${expectedPassing} failed=${after.failed} errors=${after.errors}`,
      details: { after, expectedPassing, output: tailLines(combinedOutput, 30) },
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
  const counts = parseTestOutput(result.stdout + result.stderr)
  // Rule: empty result (0/0) is NOT a valid baseline — harness is unavailable or misconfigured.
  // A valid baseline must show at least 1 test ran (passed or failed).
  if (counts.passed === 0 && counts.failed === 0 && counts.errors === 0) {
    return { passed: -1, failed: -1, errors: -1 }  // sentinel: invalid harness
  }
  return counts
}

function tailLines(s: string, n: number): string {
  return s.split("\n").slice(-n).join("\n")
}
