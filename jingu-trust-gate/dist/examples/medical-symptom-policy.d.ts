/**
 * Medical symptom assessment — health assistant policy for jingu-trust-gate.
 *
 * Use case: a patient describes symptoms. A RAG pipeline retrieves matching
 * medical knowledge records. The LLM proposes structured claims about possible
 * conditions. jingu-trust-gate admits only claims that stay within what the
 * symptom evidence actually supports — preventing over-certain diagnosis
 * assertions from reaching the user.
 *
 * The core failure mode this prevents:
 *   LLM sees "fatigue + thirst" → asserts "You have diabetes" (grade=proven)
 *   No lab results, no confirmed diagnosis → this must never reach the user
 *
 * Domain types
 *   SymptomClaim  — one LLM-proposed assertion about a possible condition
 *   EvidenceAttrs — shape of SupportRef.attributes for symptom/test records
 *
 * Gate rules (evaluateUnit)
 *   R1  grade=proven + no bound evidence                          → MISSING_EVIDENCE       → reject
 *   R2  claim asserts a confirmed diagnosis but evidence has
 *       only symptoms, no confirmed lab/test results              → DIAGNOSIS_UNCONFIRMED  → reject
 *   R3  claim asserts a specific condition but evidence only
 *       shows "consistent with" or "may suggest"                  → OVER_CERTAIN           → downgrade
 *   R4  claim asserts a treatment/medication recommendation        → TREATMENT_NOT_ADVISED  → reject
 *       (symptom evidence never supports treatment claims)
 *   R5  everything else                                           → approve
 *
 * Conflict patterns (detectConflicts)
 *   CONDITION_CONFLICT  informational — two conditions are mutually exclusive
 *                                       but both are weakly suggested by evidence
 *
 * Run:
 *   npm run build && node dist/examples/medical-symptom-policy.js
 */
export {};
//# sourceMappingURL=medical-symptom-policy.d.ts.map