import { GateRunner } from "./gate/gate-runner.js";
import { runWithRetry } from "./retry/retry-loop.js";
import { BaseRenderer } from "./renderer/base-renderer.js";
import { createDefaultAuditWriter } from "./audit/audit-log.js";
export function createTrustGate(config) {
    const auditWriter = config.auditWriter ?? createDefaultAuditWriter();
    const runner = new GateRunner(config.policy, auditWriter);
    const renderer = new BaseRenderer();
    const extractContent = config.extractContent ?? (() => "");
    return {
        async admit(proposal, support) {
            return runner.run(proposal, support);
        },
        async admitWithRetry(invoker, support, prompt) {
            const { result } = await runWithRetry(invoker, support, config.policy, prompt, config.retry, auditWriter);
            return result;
        },
        render(result, support = [], context = {}) {
            const ctx = config.policy.render
                ? config.policy.render(result.admittedUnits, support, context)
                : renderer.render(result.admittedUnits, support, context, extractContent);
            // policy.render() doesn't receive rejectedUnits — patch the count here
            ctx.summary.rejected = result.rejectedUnits.length;
            return ctx;
        },
        explain(result) {
            return explainResult(result);
        },
    };
}
export function explainResult(result) {
    const allUnits = [...result.admittedUnits, ...result.rejectedUnits];
    const reasonCodes = new Set();
    for (const unit of allUnits) {
        for (const ev of unit.evaluationResults) {
            reasonCodes.add(ev.reasonCode);
        }
    }
    return {
        totalUnits: allUnits.length,
        approved: result.admittedUnits.filter((u) => u.status === "approved").length,
        downgraded: result.admittedUnits.filter((u) => u.status === "downgraded").length,
        conflicts: result.admittedUnits.filter((u) => u.status === "approved_with_conflict").length,
        rejected: result.rejectedUnits.length,
        retryAttempts: result.retryAttempts,
        gateReasonCodes: [...reasonCodes],
    };
}
//# sourceMappingURL=trust-gate.js.map