import { randomUUID } from "node:crypto";
import { hasStructureErrors, buildAdmittedUnit, partitionUnits, } from "./gate-utils.js";
import { buildAuditEntry } from "../audit/audit-entry.js";
import { runRPPGate } from "../rpp/rpp-gate.js";
function gateLog(entry) {
    process.stderr.write(JSON.stringify({ ts: new Date().toISOString(), ...entry }) + "\n");
}
/**
 * Extract an RPPRecord from an unknown input object.
 * Checks top-level `rpp_record` field first, then `metadata.rpp_record`.
 * Returns null if neither is present.
 */
export function extractRPPRecord(input) {
    if (input == null || typeof input !== "object")
        return null;
    const obj = input;
    if (obj["rpp_record"] != null)
        return obj["rpp_record"];
    const metadata = obj["metadata"];
    if (metadata != null && typeof metadata === "object") {
        const meta = metadata;
        if (meta["rpp_record"] != null)
            return meta["rpp_record"];
    }
    return null;
}
export class GateRunner {
    policy;
    auditWriter;
    constructor(policy, auditWriter) {
        this.policy = policy;
        this.auditWriter = auditWriter;
    }
    async run(proposal, supportPool) {
        const auditId = randomUUID();
        const proposalContext = {
            proposalId: proposal.id,
            proposalKind: proposal.kind,
        };
        // Step 1: Structure validation (proposal-level)
        const structureResult = this.policy.validateStructure(proposal);
        if (hasStructureErrors(structureResult.errors)) {
            // Structure failure = all units are structure-rejected (not silently lost)
            const structureRejected = proposal.units.map((unit, i) => buildAdmittedUnit(unit, unit["id"] ?? `unit-${i}`, {
                kind: "unit",
                unitId: unit["id"] ?? `unit-${i}`,
                decision: "reject",
                reasonCode: "STRUCTURE_INVALID",
            }, [], []));
            gateLog({ event: "gate:structure_reject", auditId, proposalId: proposal.id, errors: structureResult.errors });
            const auditEntry = buildAuditEntry({
                auditId,
                proposal,
                allUnits: structureRejected,
                gateResults: [structureResult],
                unitSupportMap: {},
            });
            await this.auditWriter?.append(auditEntry);
            return {
                proposalId: proposal.id,
                admittedUnits: [],
                rejectedUnits: structureRejected,
                hasConflicts: false,
                auditId,
                retryAttempts: 1,
            };
        }
        // Step 2: Bind support + evaluate each unit
        const unitSupportMap = {};
        const evaluationResults = proposal.units.map((unit) => {
            const bound = this.policy.bindSupport(unit, supportPool);
            // Runtime safety: populate supportRefs from the pool if the policy omitted it.
            // UnitWithSupport requires supportRefs, but JS callers without type enforcement
            // may return only supportIds. Reconstruct from the pool to satisfy downstream steps.
            if (!bound.supportRefs) {
                bound.supportRefs =
                    supportPool.filter((s) => bound.supportIds.includes(s.id));
            }
            const supportIds = bound.supportIds;
            const evalResult = this.policy.evaluateUnit(bound, proposalContext);
            unitSupportMap[evalResult.unitId] = supportIds;
            return { unit, evalResult, supportIds };
        });
        // Step 3: Conflict detection (cross-unit)
        // Reconstruct UnitWithSupport[] from evaluationResults so policy can inspect bound evidence
        const unitsWithSupport = evaluationResults.map(({ unit, supportIds }) => ({
            unit,
            supportIds,
            supportRefs: supportPool.filter((s) => supportIds.includes(s.id)),
        }));
        const conflictAnnotations = this.policy.detectConflicts(unitsWithSupport, supportPool);
        // Step 4: Build AdmittedUnit[] for all units
        // Units involved in a blocking conflict are force-rejected
        const blockingConflictUnitIds = new Set(conflictAnnotations
            .filter((a) => a.severity === "blocking")
            .flatMap((a) => a.unitIds));
        const allAdmittedUnits = evaluationResults.map(({ unit, evalResult, supportIds }) => {
            const overriddenResult = blockingConflictUnitIds.has(evalResult.unitId) &&
                evalResult.decision !== "reject"
                ? {
                    ...evalResult,
                    decision: "reject",
                    reasonCode: "BLOCKING_CONFLICT",
                }
                : evalResult;
            return buildAdmittedUnit(unit, overriddenResult.unitId, overriddenResult, conflictAnnotations, supportIds);
        });
        const { admitted, rejected } = partitionUnits(allAdmittedUnits);
        for (const u of allAdmittedUnits) {
            gateLog({ event: "gate:unit_decision", auditId, proposalId: proposal.id, unitId: u.unitId, decision: u.status, reasonCode: u.evaluationResults.at(-1)?.reasonCode ?? null });
        }
        // Step 5: Write audit
        const allGateResults = [
            structureResult,
            ...evaluationResults.map((e) => e.evalResult),
            { kind: "conflict", conflictAnnotations },
        ];
        const auditEntry = buildAuditEntry({
            auditId,
            proposal,
            allUnits: allAdmittedUnits,
            gateResults: allGateResults,
            unitSupportMap,
        });
        await this.auditWriter?.append(auditEntry);
        // Step 6: RPP gate — additive AND condition
        // Only applied when an rpp_record is present in the proposal (or its metadata).
        // If present and invalid, all currently admitted units are force-rejected.
        // If absent, this step is skipped (pass-through) — RPP is opt-in per proposal.
        const rppRecord = extractRPPRecord(proposal);
        const rppResult = rppRecord != null ? runRPPGate(rppRecord) : null;
        if (rppResult != null) {
            gateLog({ event: "gate:rpp_check", auditId, proposalId: proposal.id, rpp_status: rppResult.rpp_status, allow: rppResult.allow, failures: rppResult.failures });
        }
        if (rppResult != null && !rppResult.allow) {
            const rppReasonCode = rppResult.failures[0]?.code ?? "RPP_BLOCKED";
            const rppRejected = admitted.map((admittedUnit) => ({
                ...admittedUnit,
                status: "rejected",
                evaluationResults: [
                    ...admittedUnit.evaluationResults,
                    {
                        kind: "unit",
                        unitId: admittedUnit.unitId,
                        decision: "reject",
                        reasonCode: rppReasonCode,
                    },
                ],
            }));
            return {
                proposalId: proposal.id,
                admittedUnits: [],
                rejectedUnits: [...rejected, ...rppRejected],
                hasConflicts: conflictAnnotations.length > 0,
                auditId,
                retryAttempts: 1,
            };
        }
        gateLog({ event: "gate:result", auditId, proposalId: proposal.id, admitted: admitted.length, rejected: rejected.length, hasConflicts: conflictAnnotations.length > 0 });
        return {
            proposalId: proposal.id,
            admittedUnits: admitted,
            rejectedUnits: rejected,
            hasConflicts: conflictAnnotations.length > 0,
            auditId,
            retryAttempts: 1,
        };
    }
}
//# sourceMappingURL=gate-runner.js.map