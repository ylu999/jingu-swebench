/**
 * BaseRenderer — default implementation of the render step.
 * Converts admitted units into VerifiedContext (input for LLM API).
 * Does NOT generate user-facing text — that is the LLM's responsibility.
 */
export class BaseRenderer {
    render(admittedUnits, supportPool, context, extractContent) {
        const admittedBlocks = [];
        for (const admittedUnit of admittedUnits) {
            const unitSupport = supportPool.filter((s) => admittedUnit.supportIds.includes(s.id));
            const content = extractContent(admittedUnit.unit, unitSupport);
            const block = {
                sourceId: admittedUnit.unitId,
                content,
                grade: admittedUnit.appliedGrades.length > 0
                    ? admittedUnit.appliedGrades[admittedUnit.appliedGrades.length - 1]
                    : undefined,
                conflictNote: admittedUnit.status === "approved_with_conflict"
                    ? buildConflictNote(admittedUnit.conflictAnnotations)
                    : undefined,
            };
            admittedBlocks.push(block);
        }
        const conflicts = admittedUnits.filter((u) => u.status === "approved_with_conflict").length;
        return {
            admittedBlocks,
            summary: {
                admitted: admittedBlocks.length,
                rejected: 0, // rejected units are not passed to render
                conflicts,
            },
        };
    }
}
function buildConflictNote(annotations) {
    if (!annotations || annotations.length === 0)
        return "conflicting information detected";
    // Join all conflict notes if there are multiple; otherwise use the single one
    return annotations
        .map((a) => a.description ??
        `conflict detected (${a.conflictCode}): sources ${a.sources.join(", ")}`)
        .join("; ");
}
//# sourceMappingURL=base-renderer.js.map