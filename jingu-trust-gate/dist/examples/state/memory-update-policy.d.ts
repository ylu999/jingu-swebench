/**
 * Personal memory write gate — state mutation policy for jingu-trust-gate.
 *
 * Use case: a personal assistant proposes updates to a user's memory store
 * (preferences, facts the user has stated, contact info, recurring tasks).
 * Before any write reaches system state, the gate verifies that every proposed
 * fact was actually stated by the user — not inferred, hallucinated, or carried
 * over from a different user's session.
 *
 * This is the "state" gating pattern: the gate controls what is allowed to be
 * written into persistent state, not just what is included in an LLM response.
 *
 * Domain types
 *   MemoryWrite       — one proposed write to the memory store
 *   MemoryEvidence    — shape of SupportRef.attributes for user statements
 *
 * Gate rules (evaluateUnit)
 *   R1  no "user_statement" evidence at all                   → SOURCE_UNVERIFIED   → reject
 *   R2  value was inferred, not stated directly               → INFERRED_NOT_STATED → downgrade to "inferred"
 *   R3  write targets a different userId than evidence source → SCOPE_VIOLATION     → reject
 *   R4  everything else                                       → approve
 *
 * Key idea:
 *   source_type = "user_statement" represents something the user explicitly said.
 *   An LLM may propose writes that "seem reasonable" but were never actually stated.
 *   The gate blocks those writes at the boundary — they never reach the memory store.
 *
 * Run:
 *   npm run build && node dist/examples/state/memory-update-policy.js
 */
export {};
//# sourceMappingURL=memory-update-policy.d.ts.map