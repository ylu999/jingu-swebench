export function buildAuditEntry({ auditId, proposal, allUnits, gateResults, unitSupportMap, retryAttempts, }) {
    const approvedCount = allUnits.filter((u) => u.status === "approved").length;
    const downgradedCount = allUnits.filter((u) => u.status === "downgraded").length;
    const rejectedCount = allUnits.filter((u) => u.status === "rejected").length;
    const conflictCount = allUnits.filter((u) => u.status === "approved_with_conflict").length;
    return {
        auditId,
        timestamp: new Date().toISOString(),
        proposalId: proposal.id,
        proposalKind: proposal.kind,
        totalUnits: proposal.units.length,
        approvedCount,
        downgradedCount,
        rejectedCount,
        conflictCount,
        unitSupportMap,
        gateResults,
        retryAttempts,
    };
}
//# sourceMappingURL=audit-entry.js.map