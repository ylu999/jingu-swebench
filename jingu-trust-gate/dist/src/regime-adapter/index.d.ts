/**
 * regime-adapter: thin type bridge between external regime evaluation results
 * and the trust-gate GateResultLog system.
 *
 * This adapter is optional. It converts a RegimeEvaluation (produced by
 * jingu-policy-core agents) into trust-gate-compatible audit log entries
 * so regime decisions can appear in the unified GateResultLog audit trail.
 *
 * No P1-P16 logic lives here — this is purely a type bridge.
 */
import type { UnitEvaluationResult, GateResultLog } from "../types/gate.js";
/**
 * Minimal regime evaluation result shape.
 * Kept intentionally narrow — only what trust-gate needs to bridge.
 */
export type RegimeDecision = "accept" | "reject" | "block" | "downgrade_claim";
export type RegimeViolation = {
    policyId: string;
    severity: "warning" | "reject" | "block";
    message: string;
};
export type RegimeEvaluation = {
    decision: RegimeDecision;
    score: number;
    violations: RegimeViolation[];
    summary: string;
};
/**
 * Converts a regime evaluation result to a trust-gate UnitEvaluationResult
 * so it can be included in the GateResultLog audit trail.
 *
 * Maps:
 *   "accept"           → decision: "approve",   reasonCode: "REGIME_OK"
 *   "downgrade_claim"  → decision: "downgrade",  reasonCode: "REGIME_CLAIM_INFLATED"
 *   "reject" | "block" → decision: "reject",     reasonCode: "REGIME_<policyId>"
 */
export declare function regimeToUnitResult(unitId: string, evaluation: RegimeEvaluation): UnitEvaluationResult;
/**
 * Wraps the regime unit result in a GateResultLog for inclusion in audit.
 */
export declare function regimeToGateLog(unitId: string, evaluation: RegimeEvaluation): GateResultLog;
//# sourceMappingURL=index.d.ts.map