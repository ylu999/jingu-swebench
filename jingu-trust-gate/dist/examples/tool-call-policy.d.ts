/**
 * Tool-calling assistant — tool call proposal policy for jingu-trust-gate.
 *
 * Use case: an assistant proposes tool calls to retrieve real-world data.
 * Before any tool is executed, the gate validates that the call is grounded in
 * actual user intent, that the expected value is stated, and that the call
 * does not duplicate a result already present in the context.
 *
 * Domain types
 *   ToolCallProposal — one proposed tool invocation
 *   CallContextAttrs — shape of SupportRef.attributes for conversation context items
 *
 * Gate rules (evaluateUnit)
 *   R1  justification is empty or generic ("to help the user")               → WEAK_JUSTIFICATION      → downgrade to "optional"
 *   R2  grade=necessary but no evidence that user actually requested this     → INTENT_NOT_ESTABLISHED  → reject
 *   R3  tool call duplicates a prior_result already in evidence               → REDUNDANT_CALL          → reject
 *   R4  expectedValue is empty                                                → MISSING_EXPECTED_VALUE  → downgrade to "optional"
 *   R5  everything else                                                       → approve
 *
 * No conflict detection in this example — tool calls don't conflict with each other.
 *
 * Run:
 *   npm run build && node dist/examples/tool-call-policy.js
 */
export {};
//# sourceMappingURL=tool-call-policy.d.ts.map