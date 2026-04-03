/**
 * Retry feedback helpers.
 *
 * The hints-dict pattern appears identically in every buildRetryFeedback()
 * implementation: map reasonCode → human hint, build error list, wrap in
 * RetryFeedback.  hintsFeedback() eliminates that boilerplate.
 *
 * What this helper does NOT do:
 * - No predefined hints or reason codes
 * - No default policy for what constitutes a failure
 * - The caller owns the hints map entirely
 */
import type { UnitEvaluationResult } from "../types/gate.js";
import type { RetryFeedback } from "../types/retry.js";
/**
 * Build a RetryFeedback from unit results and a hints map.
 *
 * Only results with decision !== "approve" are included as errors.
 *
 * @param unitResults  All UnitEvaluationResult from the gate run.
 * @param hints        Map from reasonCode to a human-readable correction hint.
 * @param summary      Top-level summary string.
 * @param defaultHint  Fallback for reason codes not in hints.
 *
 * @example
 *   return hintsFeedback(unitResults, {
 *     MISSING_CONTEXT:    "Add the required context ref to the support pool.",
 *     WEAK_JUSTIFICATION: "Expand the justification to explain why this step is necessary.",
 *   }, `${failed} step(s) need correction`);
 */
export declare function hintsFeedback(unitResults: UnitEvaluationResult[], hints: Record<string, string>, summary: string, defaultHint?: string): RetryFeedback;
//# sourceMappingURL=feedback.d.ts.map