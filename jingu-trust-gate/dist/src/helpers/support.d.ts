/**
 * Support ref query helpers.
 *
 * Thin wrappers around common `sourceType` and `attributes` filter patterns.
 * Every function here is equivalent to a one- or two-line expression —
 * the value is consistency and readability, not hidden logic.
 *
 * What these helpers do NOT do:
 * - No semantic rules (e.g. "proven requires two supports")
 * - No grade or risk checks
 * - No approve/reject decisions
 */
import type { SupportRef } from "../types/support.js";
export declare function hasSupportType(refs: SupportRef[], sourceType: string): boolean;
export declare function findSupportByType(refs: SupportRef[], sourceType: string): SupportRef | undefined;
export declare function filterSupportByType(refs: SupportRef[], sourceType: string): SupportRef[];
export declare function hasSupportAttr(refs: SupportRef[], key: string, value: unknown): boolean;
export declare function findSupportByAttr(refs: SupportRef[], key: string, value: unknown): SupportRef | undefined;
/**
 * Return all refs matching an arbitrary predicate.
 *
 * Use this when the built-in helpers don't cover your filter logic:
 *
 *   const matched = filterSupport(pool, s => s.sourceType === "finding" && s.attributes.verified);
 */
export declare function filterSupport(refs: SupportRef[], predicate: (s: SupportRef) => boolean): SupportRef[];
//# sourceMappingURL=support.d.ts.map