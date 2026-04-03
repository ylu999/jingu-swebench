import type { ConflictAnnotation, UnitEvaluationResult } from "./gate.js";
export type UnitStatus = "approved" | "downgraded" | "rejected" | "approved_with_conflict";
export type AdmittedUnit<TUnit> = {
    unit: TUnit;
    unitId: string;
    status: UnitStatus;
    appliedGrades: string[];
    evaluationResults: UnitEvaluationResult[];
    conflictAnnotations?: ConflictAnnotation[];
    supportIds: string[];
};
export type AdmissionResult<TUnit> = {
    proposalId: string;
    admittedUnits: AdmittedUnit<TUnit>[];
    rejectedUnits: AdmittedUnit<TUnit>[];
    hasConflicts: boolean;
    auditId: string;
    retryAttempts: number;
};
//# sourceMappingURL=admission.d.ts.map