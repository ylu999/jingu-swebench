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
export function hasSupportType(refs, sourceType) {
    return refs.some(s => s.sourceType === sourceType);
}
export function findSupportByType(refs, sourceType) {
    return refs.find(s => s.sourceType === sourceType);
}
export function filterSupportByType(refs, sourceType) {
    return refs.filter(s => s.sourceType === sourceType);
}
export function hasSupportAttr(refs, key, value) {
    return refs.some(s => s.attributes[key] === value);
}
export function findSupportByAttr(refs, key, value) {
    return refs.find(s => s.attributes[key] === value);
}
/**
 * Return all refs matching an arbitrary predicate.
 *
 * Use this when the built-in helpers don't cover your filter logic:
 *
 *   const matched = filterSupport(pool, s => s.sourceType === "finding" && s.attributes.verified);
 */
export function filterSupport(refs, predicate) {
    return refs.filter(predicate);
}
//# sourceMappingURL=support.js.map