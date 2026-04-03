import type { ConflictAnnotation } from "../types/gate.js";
import type { AdmittedUnit } from "../types/admission.js";
export type ConflictSurface = {
    unitId: string;
    status: "approved_with_conflict";
    conflictCode: string;
    conflictingSupportIds: string[];
    description?: string;
};
/**
 * surfaceConflicts — attach conflict annotations to admitted units.
 * Pure function. Used by renderer to decide how to present conflicts.
 *
 * Returns only units that have a conflict (status === "approved_with_conflict").
 */
export declare function surfaceConflicts<TUnit>(admittedUnits: AdmittedUnit<TUnit>[], annotations: ConflictAnnotation[]): ConflictSurface[];
/**
 * groupConflictsByCode — group ConflictAnnotations by conflictCode.
 * Useful for summarizing conflicts in audit/explain output.
 */
export declare function groupConflictsByCode(annotations: ConflictAnnotation[]): Record<string, ConflictAnnotation[]>;
/**
 * hasConflicts — fast check if any unit has a conflict annotation.
 */
export declare function hasConflicts(annotations: ConflictAnnotation[]): boolean;
//# sourceMappingURL=conflict-annotator.d.ts.map