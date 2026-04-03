/**
 * HPC GPU cluster — SRE incident investigation policy for jingu-trust-gate.
 *
 * Use case: an agent collects kernel logs, DCGM metrics, k8s events, and
 * PyTorch logs from a failed training job, packages them as a SupportRef pool,
 * then asks an LLM to propose structured DiagnosticClaims. jingu-trust-gate
 * admits only claims that stay within what the evidence actually supports.
 *
 * Domain types
 *   DiagnosticClaim  — one LLM-proposed assertion about the incident
 *   ObsAttributes    — shape of SupportRef.attributes for HPC observations
 *
 * Gate rules (evaluateUnit)
 *   R1/R2  grade=proven|derived + no bound evidence  → MISSING_EVIDENCE  → reject
 *   R3     "permanently damaged / must be replaced"  → UNSUPPORTED_SEVERITY → downgrade
 *          without a confirmed-loss signal (nvml/dmesg "GPU lost")
 *   R4     "all nodes / all other nodes / entire cluster" but pool covers  → UNSUPPORTED_SCOPE → downgrade
 *          fewer than 2 distinct nodes
 *   R5     specific numeric value in claim does not match evidence.value   → OVER_SPECIFIC_METRIC → downgrade
 *   R6     everything else                                                 → approve
 *
 * Conflict patterns (detectConflicts)
 *   NODE_HEALTH_CONFLICT      blocking     — same node claimed both healthy and failed
 *   TEMPORAL_METRIC_CONFLICT  informational — same node+metric has two different values in pool
 *
 * Run:
 *   npm run build && node dist/examples/hpc-diagnostic-policy.js
 */
export {};
//# sourceMappingURL=hpc-diagnostic-policy.d.ts.map