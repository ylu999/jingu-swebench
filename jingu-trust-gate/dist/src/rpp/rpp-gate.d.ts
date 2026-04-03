import type { RPPRecord, RPPFailure, RPPPolicy } from "jingu-protocol";
export type ExtraCheck = (record: RPPRecord, context?: Record<string, unknown>) => RPPFailure[];
export type RPPGateOptions = {
    policy?: RPPPolicy;
    extraChecks?: ExtraCheck[];
    context?: Record<string, unknown>;
};
export type RPPGateResult = {
    allow: boolean;
    rpp_status: "valid" | "weakly_supported" | "invalid" | "missing";
    failures: RPPFailure[];
    warnings: RPPFailure[];
};
export declare function runRPPGate(record: RPPRecord | null | undefined, options?: RPPGateOptions): RPPGateResult;
//# sourceMappingURL=rpp-gate.d.ts.map