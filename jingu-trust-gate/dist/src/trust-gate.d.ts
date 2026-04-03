import type { Proposal } from "./types/proposal.js";
import type { SupportRef } from "./types/support.js";
import type { GatePolicy } from "./types/policy.js";
import type { AdmissionResult } from "./types/admission.js";
import type { AuditWriter } from "./types/audit.js";
import type { VerifiedContext, RenderContext, GateExplanation } from "./types/renderer.js";
import type { LLMInvoker, RetryConfig } from "./types/retry.js";
export type TrustGateConfig<TUnit> = {
    policy: GatePolicy<TUnit>;
    auditWriter?: AuditWriter;
    retry?: RetryConfig;
    extractContent?: (unit: TUnit, support: SupportRef[]) => string;
};
export type TrustGate<TUnit> = {
    /**
     * Synchronous admission — runs Gate only, no LLM.
     * Proposal must already be schema-valid (obtained via output_config.format or strict:true).
     */
    admit(proposal: Proposal<TUnit>, support: SupportRef[]): Promise<AdmissionResult<TUnit>>;
    /**
     * Async admission with semantic retry.
     * LLMInvoker encapsulates one complete LLM interaction (may contain tool_use loop).
     * RetryFeedback is passed to invoker as structured type — invoker serializes it
     * as tool_result + is_error:true for Claude's built-in retry understanding.
     */
    admitWithRetry(invoker: LLMInvoker<TUnit>, support: SupportRef[], prompt: string): Promise<AdmissionResult<TUnit>>;
    /**
     * Render admitted units → VerifiedContext (input for LLM API).
     * NOT the final user-facing text — pass VerifiedContext to the LLM for language generation.
     *
     * Pass the same support pool used in admit() so the renderer can access
     * SupportRef attributes (source URLs, confidence, etc.).
     */
    render(result: AdmissionResult<TUnit>, support?: SupportRef[], context?: RenderContext): VerifiedContext;
    /**
     * Read-only summary of admission result — for orchestrators that don't need render.
     */
    explain(result: AdmissionResult<TUnit>): GateExplanation;
};
export declare function createTrustGate<TUnit>(config: TrustGateConfig<TUnit>): TrustGate<TUnit>;
export declare function explainResult<TUnit>(result: AdmissionResult<TUnit>): GateExplanation;
//# sourceMappingURL=trust-gate.d.ts.map