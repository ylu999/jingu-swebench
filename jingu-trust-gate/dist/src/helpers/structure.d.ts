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
import type { Proposal } from "../types/proposal.js";
import type { StructureValidationResult } from "../types/gate.js";
type StructureError = StructureValidationResult["errors"][number];
/** Return a StructureError array with one entry if the proposal has no units, else []. */
export declare function emptyProposalErrors(proposal: Proposal<unknown>): StructureError[];
/**
 * Return one StructureError per unit whose id field is empty or missing.
 *
 * @param units   Array of unit objects (any type).
 * @param idField Attribute name to check (default "id").
 */
export declare function missingIdErrors(units: Array<Record<string, unknown>>, idField?: string): StructureError[];
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
export declare function missingTextField(units: Array<Record<string, unknown>>, field: string, reasonCode: string, idField?: string): StructureError[];
export {};
//# sourceMappingURL=structure.d.ts.map