/**
 * BI analytics assistant — business intelligence query policy for jingu-trust-gate.
 *
 * Use case: a business analyst asks "How much did revenue grow last month?" or
 * "Which region performed best in Q3?". The pipeline retrieves metric records
 * from a data warehouse. The LLM proposes structured claims. jingu-trust-gate
 * admits only claims where the math and comparisons are grounded in the actual
 * retrieved numbers — preventing the LLM from inventing percentages, cherry-
 * picking periods, or asserting trends that the data does not support.
 *
 * The core failure mode this prevents:
 *   evidence: Jan=100k, Feb=110k, some transactions missing
 *   LLM asserts "Revenue grew 15%" → wrong calculation, no caveat about missing data
 *
 * Domain types
 *   MetricClaim   — one LLM-proposed assertion about a business metric
 *   MetricAttrs   — shape of SupportRef.attributes for data warehouse records
 *
 * Gate rules (evaluateUnit)
 *   R1  grade=proven + no bound evidence                              → MISSING_EVIDENCE      → reject
 *   R2  claim asserts a specific percentage/ratio computed from
 *       evidence values, but the math does not check out              → INCORRECT_CALCULATION → reject
 *   R3  claim asserts a trend ("grew", "declined") but evidence
 *       only covers one period (no prior period to compare)           → MISSING_BASELINE      → downgrade
 *   R4  claim asserts completeness ("total revenue", "all regions")
 *       but evidence records are marked as partial/incomplete         → INCOMPLETE_DATA       → downgrade
 *   R5  everything else                                               → approve
 *
 * Conflict patterns (detectConflicts)
 *   METRIC_CONFLICT  blocking — two records report the same metric for
 *                               the same period with different values
 *
 * Run:
 *   npm run build && node dist/examples/bi-analytics-policy.js
 */
export {};
//# sourceMappingURL=bi-analytics-policy.d.ts.map