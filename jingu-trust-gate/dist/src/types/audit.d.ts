import type { GateResultLog } from "./gate.js";
import type { ProposalKind } from "./proposal.js";
export type AuditEntry = {
    auditId: string;
    timestamp: string;
    proposalId: string;
    proposalKind: ProposalKind;
    totalUnits: number;
    approvedCount: number;
    downgradedCount: number;
    rejectedCount: number;
    conflictCount: number;
    unitSupportMap: Record<string, string[]>;
    gateResults: GateResultLog[];
    retryAttempts?: number;
    metadata?: Record<string, unknown>;
};
export interface AuditWriter {
    append(entry: AuditEntry): Promise<void>;
}
//# sourceMappingURL=audit.d.ts.map