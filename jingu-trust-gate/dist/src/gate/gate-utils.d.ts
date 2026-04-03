import type { UnitEvaluationResult, ConflictAnnotation } from "../types/gate.js";
import type { AdmittedUnit, UnitStatus } from "../types/admission.js";
export declare function hasStructureErrors(errors: Array<{
    field: string;
    reasonCode: string;
}>): boolean;
export declare function hasRejections(results: UnitEvaluationResult[]): boolean;
export declare function resolveStatus(evaluation: UnitEvaluationResult, conflictAnnotations: ConflictAnnotation[]): UnitStatus;
export declare function buildAdmittedUnit<TUnit>(unit: TUnit, unitId: string, evaluationResult: UnitEvaluationResult, conflictAnnotations: ConflictAnnotation[], supportIds: string[], previousGrade?: string): AdmittedUnit<TUnit>;
export declare function partitionUnits<TUnit>(admittedUnits: AdmittedUnit<TUnit>[]): {
    admitted: AdmittedUnit<TUnit>[];
    rejected: AdmittedUnit<TUnit>[];
};
//# sourceMappingURL=gate-utils.d.ts.map