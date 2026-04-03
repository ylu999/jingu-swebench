import type { AuditEntry } from "../types/audit.js";
import type { Proposal } from "../types/proposal.js";
import type { AdmittedUnit } from "../types/admission.js";
import type { GateResultLog } from "../types/gate.js";
export declare function buildAuditEntry<TUnit>({ auditId, proposal, allUnits, gateResults, unitSupportMap, retryAttempts, }: {
    auditId: string;
    proposal: Proposal<TUnit>;
    allUnits: AdmittedUnit<TUnit>[];
    gateResults: GateResultLog[];
    unitSupportMap: Record<string, string[]>;
    retryAttempts?: number;
}): AuditEntry;
//# sourceMappingURL=audit-entry.d.ts.map