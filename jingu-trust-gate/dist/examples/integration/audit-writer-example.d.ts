/**
 * FileAuditWriter — audit logging integration for jingu-trust-gate.
 *
 * Every gate admission is written to an append-only JSONL audit log.
 * This is Law 3 of jingu-trust-gate: "Every admission is audited."
 *
 * This example shows how to wire FileAuditWriter (the built-in production
 * audit writer) into the gate, and how to read the resulting JSONL log.
 *
 * In contrast to examples that use NoopAuditWriter, this example writes to
 * a real file. Each AuditEntry records: proposalId, timestamp, unit counts,
 * reason codes, and the full list of admitted/rejected unit IDs — giving you
 * a reproducible record of every gate decision.
 *
 * Run:
 *   npm run build && node dist/examples/integration/audit-writer-example.js
 *
 * Output:
 *   .jingu-trust-gate/audit.jsonl  (append-only, created in cwd)
 */
export {};
//# sourceMappingURL=audit-writer-example.d.ts.map