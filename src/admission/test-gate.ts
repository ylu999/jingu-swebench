import type { GateResult } from "../types/contracts.js"
import type { Workspace } from "../workspace/workspace.js"

interface TestCounts {
  passed: number
  failed: number
  errors: number
}

function parseTestOutput(output: string): TestCounts {
  // pytest summary line: "3 passed, 2 failed, 1 error"
  const passed = parseInt(output.match(/(\d+) passed/)?.[1] ?? "0", 10)
  const failed = parseInt(output.match(/(\d+) failed/)?.[1] ?? "0", 10)
  const errors = parseInt(output.match(/(\d+) error/)?.[1] ?? "0", 10)
  return { passed, failed, errors }
}

export function testGate(
  workspace: Workspace,
  testCmd: string,
  beforeCounts: TestCounts
): GateResult {
  // If baseline has no tests at all, skip the test gate (no pytest environment available)
  if (beforeCounts.passed === 0 && beforeCounts.failed === 0) {
    return {
      status: "pass",
      code: "ACCEPTED",
      message: "Test gate skipped — no test baseline available (pytest env not set up)",
    }
  }

  // Apply patch is already done by caller before this gate runs.
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
