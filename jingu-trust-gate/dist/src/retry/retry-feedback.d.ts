import type { UnitEvaluationResult } from "../types/gate.js";
import type { RetryFeedback } from "../types/retry.js";
/**
 * Extract all rejection/downgrade results that need retry feedback.
 */
export declare function collectRetryableResults(results: UnitEvaluationResult[], retryOnDecisions: Array<"reject" | "downgrade">): UnitEvaluationResult[];
/**
 * Check if an AdmissionResult needs retry based on config.
 */
export declare function needsRetry(unitResults: UnitEvaluationResult[], retryOnDecisions: Array<"reject" | "downgrade">): boolean;
/**
 * Build a default RetryFeedback from gate results.
 * Policy can override this via buildRetryFeedback().
 */
export declare function buildDefaultRetryFeedback(results: UnitEvaluationResult[]): RetryFeedback;
//# sourceMappingURL=retry-feedback.d.ts.map