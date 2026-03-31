import { appendFileSync, mkdirSync } from "node:fs"
import { dirname } from "node:path"
import type { InstanceRunResult, AttemptResult } from "../types/contracts.js"

// Jingu JSONL event log — one event per line, append-only
export type LoopEventType =
  | "run_started"
  | "attempt_started"
  | "structural_gate"
  | "apply_gate"
  | "test_gate"
  | "retry_requested"
  | "run_finished"

export interface LoopEvent {
  event_type: LoopEventType
  timestamp: string
  run_id: string
  instance_id: string
  attempt?: number
  status?: "pass" | "fail" | "skip"
  code?: string
  message?: string
  details?: Record<string, unknown>
}

function emit(outPath: string, event: LoopEvent): void {
  mkdirSync(dirname(outPath), { recursive: true })
  appendFileSync(outPath, JSON.stringify(event) + "\n", "utf8")
}

function ts(): string {
  return new Date().toISOString()
}

export function writeRunEvents(outPath: string, runId: string, result: InstanceRunResult): void {
  emit(outPath, {
    event_type: "run_started",
    timestamp: ts(),
    run_id: runId,
    instance_id: result.instanceId,
    details: { mode: result.mode },
  })

  for (const attempt of result.attempts) {
    emit(outPath, {
      event_type: "attempt_started",
      timestamp: ts(),
      run_id: runId,
      instance_id: result.instanceId,
      attempt: attempt.attempt,
    })

    emit(outPath, {
      event_type: "structural_gate",
      timestamp: ts(),
      run_id: runId,
      instance_id: result.instanceId,
      attempt: attempt.attempt,
      status: attempt.structuralGate.status,
      code: attempt.structuralGate.code,
      message: attempt.structuralGate.message,
    })

    if (attempt.applyGate) {
      emit(outPath, {
        event_type: "apply_gate",
        timestamp: ts(),
        run_id: runId,
        instance_id: result.instanceId,
        attempt: attempt.attempt,
        status: attempt.applyGate.status,
        code: attempt.applyGate.code,
        message: attempt.applyGate.message,
        details: attempt.applyGate.details,
      })
    }

    if (attempt.testGate) {
      emit(outPath, {
        event_type: "test_gate",
        timestamp: ts(),
        run_id: runId,
        instance_id: result.instanceId,
        attempt: attempt.attempt,
        status: attempt.testGate.status,
        code: attempt.testGate.code,
        message: attempt.testGate.message,
      })
    }

    if (!attempt.accepted && attempt.attempt < result.attempts.length) {
      const failedGate = attempt.testGate ?? attempt.applyGate ?? attempt.structuralGate
      emit(outPath, {
        event_type: "retry_requested",
        timestamp: ts(),
        run_id: runId,
        instance_id: result.instanceId,
        attempt: attempt.attempt,
        code: failedGate.code,
        message: `Retrying after ${failedGate.code}`,
      })
    }
  }

  emit(outPath, {
    event_type: "run_finished",
    timestamp: ts(),
    run_id: runId,
    instance_id: result.instanceId,
    status: result.accepted ? "pass" : "fail",
    details: {
      accepted: result.accepted,
      total_attempts: result.attempts.length,
      duration_ms: result.durationMs,
    },
  })
}
