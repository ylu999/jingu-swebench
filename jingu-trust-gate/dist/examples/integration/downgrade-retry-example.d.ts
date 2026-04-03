/**
 * Downgrade retry loop — retryOnDecisions integration for jingu-trust-gate.
 *
 * By default, the gate only retries on "reject" decisions.
 * Setting retryOnDecisions: ["reject", "downgrade"] causes the gate to also
 * retry when any unit is downgraded — useful when you want the LLM to try to
 * produce a fully-verified response rather than accepting a degraded one.
 *
 * This example shows:
 *   1. Default behavior  — downgraded units are admitted; no retry triggered.
 *   2. retryOnDecisions  — downgraded units trigger a retry loop.
 *   3. RetryFeedback     — what the LLM receives explaining why it needs to retry.
 *
 * The same policy (LegalClaimPolicy) is used in both runs so you can compare
 * the outcomes directly.
 *
 * Run:
 *   npm run build && node dist/examples/integration/downgrade-retry-example.js
 */
export {};
//# sourceMappingURL=downgrade-retry-example.d.ts.map