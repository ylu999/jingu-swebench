import type { Proposal } from "../types/proposal.js";
import type { SupportRef } from "../types/support.js";
import type { GatePolicy } from "../types/policy.js";
import type { AdmissionResult } from "../types/admission.js";
import type { AuditWriter } from "../types/audit.js";
import type { RPPRecord } from "jingu-protocol";
/**
 * Extract an RPPRecord from an unknown input object.
 * Checks top-level `rpp_record` field first, then `metadata.rpp_record`.
 * Returns null if neither is present.
 */
export declare function extractRPPRecord(input: unknown): RPPRecord | null;
export declare class GateRunner<TUnit> {
    private readonly policy;
    private readonly auditWriter?;
    constructor(policy: GatePolicy<TUnit>, auditWriter?: AuditWriter | undefined);
    run(proposal: Proposal<TUnit>, supportPool: SupportRef[]): Promise<AdmissionResult<TUnit>>;
}
//# sourceMappingURL=gate-runner.d.ts.map