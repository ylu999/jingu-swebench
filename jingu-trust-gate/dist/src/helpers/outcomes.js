/**
 * Outcome builders for UnitEvaluationResult.
 *
 * These are the canonical way to construct gate decisions inside a policy.
 * Using these instead of hand-building objects ensures consistent shape and
 * avoids typos in field names.
 *
 * Contract:
 *   approve()   — unit passes all checks
 *   reject()    — unit must not be admitted
 *   downgrade() — unit is admitted with reduced grade and flagged attributes
 *
 * These are value constructors only. They contain no logic.
 */
export function approve(unitId, reasonCode = "OK") {
    return { kind: "unit", unitId, decision: "approve", reasonCode };
}
export function reject(unitId, reasonCode, annotations) {
    return { kind: "unit", unitId, decision: "reject", reasonCode, annotations };
}
export function downgrade(unitId, reasonCode, newGrade, annotations) {
    return { kind: "unit", unitId, decision: "downgrade", reasonCode, newGrade, annotations };
}
//# sourceMappingURL=outcomes.js.map