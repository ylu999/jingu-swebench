/**
 * Invariant: no policy logic leaks into trust-gate.
 *
 * Rule: "改规则 ≠ 改 trust-gate"
 * If any check here fails, a rule was written in the wrong layer.
 *
 * What this file guards:
 *   CHECK 1 — No numeric threshold comparisons (magic numbers)
 *   CHECK 2 — No score/confidence/weight field comparisons
 *   CHECK 3 — No non-whitelisted imports from jingu-policy-core
 *   CHECK 4 — No direct domain field access in conditionals
 *
 * How to suppress a false positive:
 *   Add a line-level comment:  // policy-leak-ignore: <reason>
 *   The reason will appear in the test output so it stays reviewable.
 */
export {};
//# sourceMappingURL=policy-leak.test.d.ts.map