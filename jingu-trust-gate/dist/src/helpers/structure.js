/**
 * Structure validation helpers.
 *
 * Thin helpers for the boilerplate that appears at the top of every
 * validateStructure() implementation: empty proposal check, missing id,
 * empty required text fields.
 *
 * What these helpers do NOT do:
 * - No schema inference or reflection
 * - No required-field declarations
 * - No domain-specific field names
 *   (caller always passes field name and accessor explicitly)
 */
/** Return a StructureError array with one entry if the proposal has no units, else []. */
export function emptyProposalErrors(proposal) {
    if (!proposal.units || proposal.units.length === 0) {
        return [{ field: "units", reasonCode: "EMPTY_PROPOSAL" }];
    }
    return [];
}
/**
 * Return one StructureError per unit whose id field is empty or missing.
 *
 * @param units   Array of unit objects (any type).
 * @param idField Attribute name to check (default "id").
 */
export function missingIdErrors(units, idField = "id") {
    return units
        .filter(u => !u[idField] || String(u[idField]).trim() === "")
        .map(() => ({ field: idField, reasonCode: "MISSING_UNIT_ID" }));
}
/**
 * Return one StructureError per unit whose text field is empty or missing.
 *
 * @param units       Array of unit objects.
 * @param field       Attribute name to validate.
 * @param reasonCode  reasonCode to set on each error.
 * @param idField     Attribute used to identify the unit in the error message.
 *
 * @example
 *   errors.push(...missingTextField(proposal.units, "description", "EMPTY_DESCRIPTION"));
 */
export function missingTextField(units, field, reasonCode, idField = "id") {
    return units
        .filter(u => !u[field] || String(u[field]).trim() === "")
        .map(u => ({
        field,
        reasonCode,
        message: `unit ${u[idField] ?? "?"}: ${field} is empty or missing`,
    }));
}
//# sourceMappingURL=structure.js.map