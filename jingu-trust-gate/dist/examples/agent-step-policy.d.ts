/**
 * Research agent — next-step proposal policy for jingu-trust-gate.
 *
 * Use case: a multi-step research agent proposes what to do next in a task.
 * Each proposed step must justify itself against already-available context.
 * The gate enforces that steps citing required context actually have that
 * context available, that synthesis/writing steps are grounded in findings,
 * and that weak justifications are flagged before being executed.
 *
 * Domain types
 *   AgentStepProposal — one proposed next step in a research task
 *   StepContextAttrs  — shape of SupportRef.attributes for available context items
 *
 * Gate rules (evaluateUnit)
 *   R1  grade=required + requiredContext cites IDs not in pool                → MISSING_CONTEXT         → reject
 *   R2  stepType=synthesize|write but no evidence with type="finding"         → INSUFFICIENT_FINDINGS  → reject
 *   R3  justification is empty or < 10 chars                                  → WEAK_JUSTIFICATION     → downgrade to "optional"
 *   R4  everything else                                                        → approve
 *
 * Conflict patterns (detectConflicts)
 *   REDUNDANT_STEP  informational — two steps have identical stepType + overlapping requiredContext
 *
 * Run:
 *   npm run build && node dist/examples/agent-step-policy.js
 */
export {};
//# sourceMappingURL=agent-step-policy.d.ts.map