import { GateRunner } from "../gate/gate-runner.js";
import { needsRetry, collectRetryableResults } from "./retry-feedback.js";
const DEFAULT_RETRY_CONFIG = {
    maxRetries: 3,
    retryOnDecisions: ["reject"],
};
/**
 * runWithRetry — semantic-level retry loop.
 *
 * Key design:
 * - LLMInvoker encapsulates ONE complete LLM interaction (may contain multiple tool_use turns).
 * - RetryFeedback is structured (not a string). The LLMInvoker implementer is responsible
 *   for serializing it as tool_result + is_error: true to leverage Claude's built-in retry.
 * - the gate decides WHETHER to retry (gate semantic rejection).
 * - LLMInvoker decides HOW to pass feedback to the LLM.
 */
export async function runWithRetry(invoker, support, policy, prompt, config = DEFAULT_RETRY_CONFIG, auditWriter) {
    const runner = new GateRunner(policy, auditWriter);
    let lastResult;
    let attempts = 0;
    for (let attempt = 0; attempt <= config.maxRetries; attempt++) {
        attempts = attempt + 1;
        // Get feedback from previous attempt (undefined on first attempt)
        const feedback = attempt > 0 && lastResult
            ? buildFeedbackFromResult(lastResult, policy, attempt, config)
            : undefined;
        // Invoke LLM (complete interaction, may contain tool loop internally)
        const proposal = await invoker(prompt, feedback);
        // Run gate engine
        lastResult = await runner.run(proposal, support);
        // Collect unit evaluation results for retry check
        const allUnitResults = [
            ...lastResult.admittedUnits.flatMap((u) => u.evaluationResults),
            ...lastResult.rejectedUnits.flatMap((u) => u.evaluationResults),
        ];
        // Check if retry is needed
        if (!needsRetry(allUnitResults, config.retryOnDecisions)) {
            break; // converged
        }
        // Last attempt reached, stop regardless
        if (attempt >= config.maxRetries) {
            break;
        }
    }
    // Attach retry count to result
    const finalResult = {
        ...lastResult,
        retryAttempts: attempts,
    };
    return { result: finalResult, attempts };
}
function buildFeedbackFromResult(result, policy, attempt, config) {
    const allUnitResults = [
        ...result.admittedUnits.flatMap((u) => u.evaluationResults),
        ...result.rejectedUnits.flatMap((u) => u.evaluationResults),
    ];
    const retryableResults = collectRetryableResults(allUnitResults, config.retryOnDecisions);
    const context = {
        attempt,
        maxRetries: config.maxRetries,
        proposalId: result.proposalId,
    };
    return policy.buildRetryFeedback(retryableResults, context);
}
//# sourceMappingURL=retry-loop.js.map