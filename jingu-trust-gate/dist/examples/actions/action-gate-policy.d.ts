/**
 * Irreversible-action gate — action proposal policy for jingu-trust-gate.
 *
 * Use case: an AI assistant can take write-side actions that change the world:
 * send_email, delete_file, publish_post, transfer_funds, archive_thread.
 * Because these are irreversible or high-risk, the gate is stricter than for
 * read-only tool calls: every action needs explicit user authorization, high-risk
 * irreversible actions require user confirmation, and destructive operations
 * require an explicit deletion request.
 *
 * Domain types
 *   ActionProposal    — one proposed irreversible action
 *   ActionContextAttrs — shape of SupportRef.attributes for authorization evidence
 *
 * Gate rules (evaluateUnit)
 *   R1  no evidence of type "explicit_user_request"                           → INTENT_NOT_ESTABLISHED      → reject
 *   R2  riskLevel=high + isReversible=false + no "user_confirmation" evidence → CONFIRM_REQUIRED             → reject
 *   R3  justification is empty or < 20 chars                                  → WEAK_JUSTIFICATION           → reject (not downgrade — actions need strong justification)
 *   R4  actionName contains "delete"|"remove"|"drop" + no explicit deletion request → DESTRUCTIVE_WITHOUT_AUTHORIZATION → reject
 *   R5  everything else                                                        → approve
 *
 * Conflict patterns (detectConflicts)
 *   CONTRADICTORY_ACTIONS  blocking — e.g. send email to user AND delete user's account
 *
 * Run:
 *   npm run build && node dist/examples/action-gate-policy.js
 */
export {};
//# sourceMappingURL=action-gate-policy.d.ts.map