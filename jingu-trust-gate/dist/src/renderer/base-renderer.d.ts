import type { AdmittedUnit } from "../types/admission.js";
import type { SupportRef } from "../types/support.js";
import type { VerifiedContext, RenderContext } from "../types/renderer.js";
/**
 * BaseRenderer — default implementation of the render step.
 * Converts admitted units into VerifiedContext (input for LLM API).
 * Does NOT generate user-facing text — that is the LLM's responsibility.
 */
export declare class BaseRenderer {
    render<TUnit>(admittedUnits: AdmittedUnit<TUnit>[], supportPool: SupportRef[], context: RenderContext, extractContent: (unit: TUnit, support: SupportRef[]) => string): VerifiedContext;
}
//# sourceMappingURL=base-renderer.d.ts.map