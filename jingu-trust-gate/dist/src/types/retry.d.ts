import type { Proposal } from "./proposal.js";
export type RetryFeedback = {
    summary: string;
    errors: Array<{
        unitId?: string;
        reasonCode: string;
        details?: Record<string, unknown>;
    }>;
};
export type RetryConfig = {
    maxRetries: number;
    retryOnDecisions: Array<"reject" | "downgrade">;
};
export type RetryContext = {
    attempt: number;
    maxRetries: number;
    proposalId: string;
};
export type LLMInvoker<TUnit> = (prompt: string, feedback?: RetryFeedback) => Promise<Proposal<TUnit>>;
//# sourceMappingURL=retry.d.ts.map