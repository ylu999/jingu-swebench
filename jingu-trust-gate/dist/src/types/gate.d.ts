export type StructureValidationResult = {
    kind: "structure";
    valid: boolean;
    errors: Array<{
        field: string;
        reasonCode: string;
        message?: string;
    }>;
};
export type UnitEvaluationResult = {
    kind: "unit";
    unitId: string;
    decision: "approve" | "downgrade" | "reject";
    reasonCode: string;
    newGrade?: string;
    annotations?: Record<string, unknown>;
};
export type ConflictDetectionResult = {
    kind: "conflict";
    conflictAnnotations: ConflictAnnotation[];
};
export type ConflictAnnotation = {
    unitIds: string[];
    conflictCode: string;
    sources: string[];
    severity: "informational" | "blocking";
    description?: string;
};
export type GateResultLog = StructureValidationResult | UnitEvaluationResult | ConflictDetectionResult;
//# sourceMappingURL=gate.d.ts.map