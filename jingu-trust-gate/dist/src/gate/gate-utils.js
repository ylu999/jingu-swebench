export function hasStructureErrors(errors) {
    return errors.length > 0;
}
export function hasRejections(results) {
    return results.some((r) => r.decision === "reject");
}
export function resolveStatus(evaluation, conflictAnnotations) {
    if (evaluation.decision === "reject")
        return "rejected";
    const hasConflict = conflictAnnotations.some((c) => c.unitIds.includes(evaluation.unitId));
    if (hasConflict)
        return "approved_with_conflict";
    if (evaluation.decision === "downgrade")
        return "downgraded";
    return "approved";
}
export function buildAdmittedUnit(unit, unitId, evaluationResult, conflictAnnotations, supportIds, previousGrade) {
    const status = resolveStatus(evaluationResult, conflictAnnotations);
    const appliedGrades = previousGrade ? [previousGrade] : [];
    if (evaluationResult.newGrade) {
        appliedGrades.push(evaluationResult.newGrade);
    }
    const matchedConflicts = conflictAnnotations.filter((c) => c.unitIds.includes(unitId));
    return {
        unit,
        unitId,
        status,
        appliedGrades,
        evaluationResults: [evaluationResult],
        conflictAnnotations: status === "approved_with_conflict" ? matchedConflicts : undefined,
        supportIds,
    };
}
export function partitionUnits(admittedUnits) {
    return {
        admitted: admittedUnits.filter((u) => u.status !== "rejected"),
        rejected: admittedUnits.filter((u) => u.status === "rejected"),
    };
}
//# sourceMappingURL=gate-utils.js.map