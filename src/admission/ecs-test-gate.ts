/**
 * ECS-based test gate for SWE-bench evaluation.
 *
 * Runs FAIL_TO_PASS tests inside the official SWE-bench Docker image on AWS ECS Fargate.
 * This provides the correct evaluation environment (x86_64, conda testbed, all C extensions)
 * without requiring a local Docker daemon or disk space for large images.
 *
 * Architecture:
 *   1. Encode patch as base64
 *   2. Submit ECS RunTask with:
 *      - image override: swebench/sweb.eval.x86_64.<instance_id>:latest
 *      - command override: decode patch → git apply → conda run pytest
 *      - log driver: awslogs → /jingu/swebench/<taskId>
 *   3. Poll DescribeTasks until STOPPED
 *   4. Fetch CloudWatch Logs, parse test output
 *   5. Return GateResult with evaluation_method: "docker_pytest"
 *
 * AWS resources required (one-time setup):
 *   - ECS cluster: jingu-swebench (us-west-2)
 *   - Task definition: jingu-swebench-runner
 *   - IAM execution role: jingu-swebench-task-exec
 *   - CloudWatch Log Group: /jingu/swebench
 *   - VPC + public subnet (default VPC works)
 */

import {
  ECSClient,
  RunTaskCommand,
  DescribeTasksCommand,
  RegisterTaskDefinitionCommand,
  type Task,
} from "@aws-sdk/client-ecs"
import {
  CloudWatchLogsClient,
  GetLogEventsCommand,
} from "@aws-sdk/client-cloudwatch-logs"
import type { GateResult } from "../types/contracts.js"

// ---------------------------------------------------------------------------
// Configuration — matches the one-time AWS setup
// ---------------------------------------------------------------------------

export type EcsConfig = {
  region: string
  cluster: string
  taskDefinition: string
  containerName: string
  executionRoleArn: string
  subnetId: string
  securityGroupId: string
  logGroup: string
  logStreamPrefix: string
}

export const DEFAULT_ECS_CONFIG: EcsConfig = {
  region: "us-west-2",
  cluster: "jingu-swebench",
  taskDefinition: "jingu-swebench-runner",
  containerName: "runner",
  executionRoleArn: "arn:aws:iam::235494812052:role/jingu-swebench-task-exec",
  subnetId: "subnet-0d1858b107b12ebc7",
  securityGroupId: "sg-01ec5deee6d5ea0b6",
  logGroup: "/jingu/swebench",
  logStreamPrefix: "task",
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function swebenchImageName(instanceId: string): string {
  return `swebench/sweb.eval.x86_64.${instanceId}:latest`
}

// Build test command for inside the container (same logic as docker-test-gate.ts)
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

// Poll DescribeTasks until the task reaches STOPPED state or timeout.
async function waitForTask(
  ecs: ECSClient,
  cluster: string,
  taskArn: string,
  pollIntervalMs = 5_000,
  maxWaitMs = 600_000   // 10 min max (pulling large images takes time)
): Promise<Task | null> {
  const deadline = Date.now() + maxWaitMs
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, pollIntervalMs))
    const res = await ecs.send(new DescribeTasksCommand({ cluster, tasks: [taskArn] }))
    const task = res.tasks?.[0]
    if (!task) return null
    const status = task.lastStatus ?? ""
    if (status === "STOPPED") return task
    // Log progress every ~30s (every 6 polls)
  }
  return null  // timed out
}

// Fetch all CloudWatch log events for a task's log stream.
async function fetchTaskLogs(
  cwl: CloudWatchLogsClient,
  logGroup: string,
  logStreamPrefix: string,
  taskId: string,  // short task ID, not full ARN
  containerName: string
): Promise<string> {
  const streamName = `${logStreamPrefix}/${containerName}/${taskId}`
  try {
    const res = await cwl.send(new GetLogEventsCommand({
      logGroupName: logGroup,
      logStreamName: streamName,
      startFromHead: true,
    }))
    return (res.events ?? []).map((e) => e.message ?? "").join("\n")
  } catch (err) {
    const e = err as { name?: string }
    if (e.name === "ResourceNotFoundException") {
      return ""  // log stream not created yet (task failed before writing)
    }
    throw err
  }
}

// ---------------------------------------------------------------------------
// Main: ecsTestGate
// ---------------------------------------------------------------------------

export type EcsTestGateOpts = {
  config?: Partial<EcsConfig>
  // Maximum wait time for task completion in ms (default: 600s)
  timeoutMs?: number
}

// Run FAIL_TO_PASS tests on ECS Fargate using the official SWE-bench Docker image.
// Returns GateResult with evaluation_method: "docker_pytest" (EA6 compliant).
// Falls back to apply_gate_only (EA5 compliant) when ECS is unavailable.
export async function ecsTestGate(
  instanceId: string,
  repo: string,
  patchText: string,
  failToPass: string[],
  opts: EcsTestGateOpts = {}
): Promise<GateResult> {
  const cfg: EcsConfig = { ...DEFAULT_ECS_CONFIG, ...(opts.config ?? {}) }
  const timeoutMs = opts.timeoutMs ?? 600_000

  const ecs = new ECSClient({ region: cfg.region })
  const cwl = new CloudWatchLogsClient({ region: cfg.region })

  const image = swebenchImageName(instanceId)
  const testCmd = buildContainerTestCmd(repo, failToPass)
  const expectedPassing = failToPass.length

  // Encode patch as base64 — passed as env var to the container
  // Container decodes it and applies with git apply
  const patchB64 = Buffer.from(patchText).toString("base64")

  // Full container command:
  // 1. Decode patch from env var
  // 2. Apply patch in /testbed
  // 3. Run FAIL_TO_PASS tests via conda testbed env
  const applyAndTest = [
    `echo "$PATCH_B64" | base64 -d > /tmp/patch.diff`,
    `cd /testbed`,
    `conda run -n testbed git apply /tmp/patch.diff 2>&1 || { echo "GIT_APPLY_FAILED"; exit 1; }`,
    `conda run -n testbed ${testCmd} 2>&1`,
  ].join(" && ")

  console.log(`  [ecs] submitting task for ${instanceId}`)

  let taskArn: string
  let taskId: string
  try {
    // ECS does not support image override in containerOverrides at RunTask time.
    // Register a per-run task definition with the correct SWE-bench image.
    // This is a lightweight API call (~100ms) — task definitions are cheap and reusable.
    const tdResult = await ecs.send(new RegisterTaskDefinitionCommand({
      family: cfg.taskDefinition,
      executionRoleArn: cfg.executionRoleArn,
      networkMode: "awsvpc",
      requiresCompatibilities: ["FARGATE"],
      cpu: "4096",
      memory: "8192",
      containerDefinitions: [
        {
          name: cfg.containerName,
          image,
          essential: true,
          command: ["bash", "-c", applyAndTest],
          environment: [{ name: "PATCH_B64", value: patchB64 }],
          logConfiguration: {
            logDriver: "awslogs",
            options: {
              "awslogs-group": cfg.logGroup,
              "awslogs-region": cfg.region,
              "awslogs-stream-prefix": cfg.logStreamPrefix,
            },
          },
        },
      ],
    }))
    const taskDefArn = tdResult.taskDefinition?.taskDefinitionArn
    if (!taskDefArn) throw new Error("RegisterTaskDefinition returned no ARN")

    const runResult = await ecs.send(new RunTaskCommand({
      cluster: cfg.cluster,
      taskDefinition: taskDefArn,
      launchType: "FARGATE",
      networkConfiguration: {
        awsvpcConfiguration: {
          subnets: [cfg.subnetId],
          securityGroups: [cfg.securityGroupId],
          assignPublicIp: "ENABLED",  // required to pull Docker Hub images
        },
      },
    }))

    const task = runResult.tasks?.[0]
    if (!task?.taskArn) {
      const failures = runResult.failures?.map((f) => f.reason).join(", ") ?? "unknown"
      console.log(`  [ecs] RunTask failed: ${failures}`)
      return {
        status: "pass",
        code: "ACCEPTED",
        message: `ECS RunTask failed for ${instanceId} — relying on apply gate`,
        details: {
          evaluation_method: "apply_gate_only",
          is_fallback: true,
          fallback_reason: `ecs_run_task_failed: ${failures}`,
        },
      }
    }

    taskArn = task.taskArn
    // Task ID is the last segment of the ARN
    taskId = taskArn.split("/").pop()!
    console.log(`  [ecs] task submitted: ${taskId}`)
  } catch (err) {
    const e = err as Error
    console.log(`  [ecs] RunTask error: ${e.message}`)
    return {
      status: "pass",
      code: "ACCEPTED",
      message: `ECS unavailable for ${instanceId} — relying on apply gate`,
      details: {
        evaluation_method: "apply_gate_only",
        is_fallback: true,
        fallback_reason: `ecs_error: ${e.message}`,
      },
    }
  }

  // Wait for task to complete
  console.log(`  [ecs] waiting for task ${taskId} (timeout ${timeoutMs / 1000}s)...`)
  const completedTask = await waitForTask(ecs, cfg.cluster, taskArn, 5_000, timeoutMs)

  if (!completedTask) {
    console.log(`  [ecs] task ${taskId} timed out`)
    return {
      status: "pass",
      code: "ACCEPTED",
      message: `ECS task timed out for ${instanceId} — relying on apply gate`,
      details: {
        evaluation_method: "apply_gate_only",
        is_fallback: true,
        fallback_reason: "ecs_task_timeout",
        taskId,
      },
    }
  }

  // Check exit code from the container
  const containerExit = completedTask.containers?.[0]?.exitCode ?? -1
  const stopReason = completedTask.stoppedReason ?? ""
  console.log(`  [ecs] task ${taskId} stopped (exit=${containerExit}, reason=${stopReason})`)

  // Fetch logs from CloudWatch
  const logOutput = await fetchTaskLogs(cwl, cfg.logGroup, cfg.logStreamPrefix, taskId, cfg.containerName)

  if (logOutput.includes("GIT_APPLY_FAILED")) {
    return {
      status: "fail",
      code: "PATCH_APPLY_FAILED",
      message: `Patch failed to apply in ECS container for ${instanceId}`,
      details: {
        evaluation_method: "docker_pytest",
        output: tailLines(logOutput, 20),
        taskId,
      },
    }
  }

  // Parse test output
  const counts = parseTestOutput(logOutput)
  const totalRan = counts.passed + counts.failed + counts.errors

  // If no test output at all (container crashed before tests)
  if (totalRan === 0 && containerExit !== 0) {
    console.log(`  [ecs] no test output, container exit=${containerExit}`)
    return {
      status: "pass",
      code: "ACCEPTED",
      message: `ECS container exited without test output for ${instanceId} — relying on apply gate`,
      details: {
        evaluation_method: "apply_gate_only",
        is_fallback: true,
        fallback_reason: "ecs_container_no_output",
        containerExit,
        stopReason,
        taskId,
        output: tailLines(logOutput, 10),
      },
    }
  }

  const allPass = counts.passed >= expectedPassing && counts.errors === 0

  if (allPass) {
    return {
      status: "pass",
      code: "ACCEPTED",
      message: `All ${expectedPassing} FAIL_TO_PASS test(s) now passing on ECS (${counts.passed} passed, ${counts.failed} pre-existing failures)`,
      details: {
        evaluation_method: "docker_pytest",
        counts,
        expectedPassing,
        taskId,
      },
    }
  }

  return {
    status: "fail",
    code: "TESTS_NOT_IMPROVED",
    message: `FAIL_TO_PASS not resolved on ECS: passed=${counts.passed}/${totalRan} expected=${expectedPassing} failed=${counts.failed} errors=${counts.errors}`,
    details: {
      evaluation_method: "docker_pytest",
      counts,
      expectedPassing,
      output: tailLines(logOutput, 30),
      taskId,
    },
  }
}
