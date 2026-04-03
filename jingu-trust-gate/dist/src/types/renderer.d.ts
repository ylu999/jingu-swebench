export type VerifiedBlock = {
    sourceId: string;
    content: string;
    grade?: string;
    conflictNote?: string;
    unsupportedAttributes?: string[];
};
export type VerifiedContext = {
    admittedBlocks: VerifiedBlock[];
    summary: {
        admitted: number;
        rejected: number;
        conflicts: number;
    };
    instructions?: string;
};
export type RenderContext = {
    userLocale?: string;
    channelType?: "chat" | "api" | "notification";
    metadata?: Record<string, unknown>;
};
export type GateExplanation = {
    totalUnits: number;
    approved: number;
    downgraded: number;
    conflicts: number;
    rejected: number;
    retryAttempts: number;
    gateReasonCodes: string[];
};
//# sourceMappingURL=renderer.d.ts.map