/**
 * Extract all rejection/downgrade results that need retry feedback.
 */
export function collectRetryableResults(results, retryOnDecisions) {
    return results.filter((r) => retryOnDecisions.includes(r.decision));
}
/**
 * Check if an AdmissionResult needs retry based on config.
 */
export function needsRetry(unitResults, retryOnDecisions) {
    return collectRetryableResults(unitResults, retryOnDecisions).length > 0;
}
/**
 * Build a default RetryFeedback from gate results.
 * Policy can override this via buildRetryFeedback().
 */
export function buildDefaultRetryFeedback(results) {
    return {
        summary: `${results.length} unit(s) failed governance gates.`,
        errors: results.map((r) => ({
            unitId: r.unitId,
            reasonCode: r.reasonCode,
            details: r.annotations,
        })),
    };
}
//# sourceMappingURL=retry-feedback.js.map