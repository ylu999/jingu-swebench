/**
 * Legal contract analysis — contract review assistant policy for jingu-trust-gate.
 *
 * Use case: a lawyer or business user asks "Does this contract have a termination
 * clause?" or "What are the penalty terms?". The RAG pipeline retrieves relevant
 * contract clauses as evidence. The LLM proposes structured claims. jingu-trust-gate
 * admits only claims that match actual clause text — preventing the LLM from
 * inventing clause names, inventing specific figures, or asserting the presence
 * of terms that do not appear in the retrieved text.
 *
 * The core failure mode this prevents:
 *   Contract has "cancellation conditions" but no explicit "termination clause"
 *   LLM asserts "The contract includes a termination clause" → legal hallucination
 *
 * Domain types
 *   ContractClaim  — one LLM-proposed assertion about contract content
 *   ClauseAttrs    — shape of SupportRef.attributes for contract clause records
 *
 * Gate rules (evaluateUnit)
 *   R1  grade=proven + no bound evidence                              → MISSING_EVIDENCE      → reject
 *   R2  claim uses a specific legal term (e.g. "termination clause")
 *       not present verbatim in evidence clause text                  → TERM_NOT_IN_EVIDENCE  → reject
 *   R3  claim asserts a specific figure (penalty %, dollar amount,
 *       notice period days) not present in evidence                   → OVER_SPECIFIC_FIGURE  → downgrade
 *   R4  claim asserts obligation or right that the clause text
 *       does not explicitly grant                                     → SCOPE_EXCEEDED        → downgrade
 *   R5  everything else                                               → approve
 *
 * Conflict patterns (detectConflicts)
 *   CLAUSE_CONFLICT  blocking — two clauses in evidence directly contradict
 *                               each other on the same right or obligation
 *
 * Run:
 *   npm run build && node dist/examples/legal-contract-policy.js
 */
export {};
//# sourceMappingURL=legal-contract-policy.d.ts.map