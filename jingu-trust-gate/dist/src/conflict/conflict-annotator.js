/**
 * surfaceConflicts — attach conflict annotations to admitted units.
 * Pure function. Used by renderer to decide how to present conflicts.
 *
 * Returns only units that have a conflict (status === "approved_with_conflict").
 */
export function surfaceConflicts(admittedUnits, annotations) {
    const surfaces = [];
    for (const unit of admittedUnits) {
        if (unit.status !== "approved_with_conflict")
            continue;
        const annotation = annotations.find((a) => a.unitIds.includes(unit.unitId));
        if (!annotation)
            continue;
        surfaces.push({
            unitId: unit.unitId,
            status: "approved_with_conflict",
            conflictCode: annotation.conflictCode,
            conflictingSupportIds: annotation.sources,
            description: annotation.description,
        });
    }
    return surfaces;
}
/**
 * groupConflictsByCode — group ConflictAnnotations by conflictCode.
 * Useful for summarizing conflicts in audit/explain output.
 */
export function groupConflictsByCode(annotations) {
    const groups = {};
    for (const annotation of annotations) {
        if (!groups[annotation.conflictCode]) {
            groups[annotation.conflictCode] = [];
        }
        groups[annotation.conflictCode].push(annotation);
    }
    return groups;
}
/**
 * hasConflicts — fast check if any unit has a conflict annotation.
 */
export function hasConflicts(annotations) {
    return annotations.length > 0;
}
//# sourceMappingURL=conflict-annotator.js.map