import type { Proposal } from "./proposal.js";
import type { StructureValidationResult, UnitEvaluationResult, ConflictAnnotation } from "./gate.js";
import type { SupportRef, UnitWithSupport } from "./support.js";
import type { AdmittedUnit } from "./admission.js";
import type { VerifiedContext, RenderContext } from "./renderer.js";
import type { RetryFeedback, RetryContext } from "./retry.js";
export interface GatePolicy<TUnit> {
    validateStructure(proposal: Proposal<TUnit>): StructureValidationResult;
    bindSupport(unit: TUnit, supportPool: SupportRef[]): UnitWithSupport<TUnit>;
    evaluateUnit(unitWithSupport: UnitWithSupport<TUnit>, context: {
        proposalId: string;
        proposalKind: string;
    }): UnitEvaluationResult;
    detectConflicts(units: UnitWithSupport<TUnit>[], supportPool: SupportRef[]): ConflictAnnotation[];
    render(admittedUnits: AdmittedUnit<TUnit>[], supportPool: SupportRef[], context: RenderContext): VerifiedContext;
    buildRetryFeedback(unitResults: UnitEvaluationResult[], context: RetryContext): RetryFeedback;
}
//# sourceMappingURL=policy.d.ts.map