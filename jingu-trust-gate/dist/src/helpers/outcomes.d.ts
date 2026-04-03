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
import type { UnitEvaluationResult } from "../types/gate.js";
export declare function approve(unitId: string, reasonCode?: string): UnitEvaluationResult;
export declare function reject(unitId: string, reasonCode: string, annotations?: Record<string, unknown>): UnitEvaluationResult;
export declare function downgrade(unitId: string, reasonCode: string, newGrade: string, annotations?: Record<string, unknown>): UnitEvaluationResult;
//# sourceMappingURL=outcomes.d.ts.map