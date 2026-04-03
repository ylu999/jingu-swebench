/**
 * E-commerce catalog chatbot — product & inventory query policy for jingu-trust-gate.
 *
 * Use case: a customer asks "Does this Bluetooth headphone support noise cancellation?"
 * or "How many units are in stock?". The RAG pipeline retrieves product records as
 * evidence. The LLM proposes structured claims. jingu-trust-gate admits only claims
 * that stay within what the catalog data actually supports.
 *
 * Domain types
 *   ProductClaim   — one LLM-proposed assertion about a product
 *   CatalogAttrs   — shape of SupportRef.attributes for catalog records
 *
 * Gate rules (evaluateUnit)
 *   R1  grade=proven + no bound evidence                          → MISSING_EVIDENCE      → reject
 *   R2  claim asserts a feature not present in evidence.features  → UNSUPPORTED_FEATURE   → downgrade
 *   R3  claim asserts a specific brand/model not in evidence      → OVER_SPECIFIC_BRAND   → downgrade
 *   R4  claim asserts exact stock count but evidence.stock
 *       is a range or a different number                          → OVER_SPECIFIC_STOCK   → downgrade
 *   R5  everything else                                           → approve
 *
 * Conflict patterns (detectConflicts)
 *   STOCK_CONFLICT    blocking     — two records for the same SKU show different in-stock status
 *   FEATURE_CONFLICT  informational — two records for the same SKU disagree on a feature value
 *
 * Run:
 *   npm run build && node dist/examples/ecommerce-catalog-policy.js
 */
export {};
//# sourceMappingURL=ecommerce-catalog-policy.d.ts.map