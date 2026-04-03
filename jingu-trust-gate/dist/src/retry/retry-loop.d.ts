import type { SupportRef } from "../types/support.js";
import type { GatePolicy } from "../types/policy.js";
import type { AdmissionResult } from "../types/admission.js";
import type { LLMInvoker, RetryConfig } from "../types/retry.js";
import type { AuditWriter } from "../types/audit.js";
export type RetryLoopResult<TUnit> = {
    result: AdmissionResult<TUnit>;
    attempts: number;
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
export declare function runWithRetry<TUnit>(invoker: LLMInvoker<TUnit>, support: SupportRef[], policy: GatePolicy<TUnit>, prompt: string, config?: RetryConfig, auditWriter?: AuditWriter): Promise<RetryLoopResult<TUnit>>;
//# sourceMappingURL=retry-loop.d.ts.map