/**
 * RPP Three-Layer Architecture Demo
 *
 * Scenario: an AI agent is about to delete a production config file.
 *
 * This demo runs the same action through the gate 6 times, each time
 * with a different RPP record. You will see exactly what gets blocked,
 * why, and how project policy tightens the gate beyond the core invariants.
 *
 * Run: npm run demo:rpp
 */
import { runRPPGate } from "../src/rpp/rpp-gate.js";
// ---------------------------------------------------------------------------
// Terminal output helpers
// ---------------------------------------------------------------------------
const RESET = "\x1b[0m";
const RED = "\x1b[31m";
const GREEN = "\x1b[32m";
const YELLOW = "\x1b[33m";
const CYAN = "\x1b[36m";
const BOLD = "\x1b[1m";
const DIM = "\x1b[2m";
function header(title) {
    console.log(`\n${BOLD}${"─".repeat(70)}${RESET}`);
    console.log(`${BOLD}${CYAN}  ${title}${RESET}`);
    console.log(`${BOLD}${"─".repeat(70)}${RESET}`);
}
function section(label) {
    console.log(`\n${DIM}  ${label}${RESET}`);
}
function printResult(label, result, expectAllow) {
    const icon = result.allow ? `${GREEN}✓ ALLOWED${RESET}` : `${RED}✗ BLOCKED${RESET}`;
    const correct = result.allow === expectAllow;
    const check = correct ? `${GREEN}(expected)${RESET}` : `${RED}(UNEXPECTED!)${RESET}`;
    console.log(`\n  ${BOLD}${label}${RESET}`);
    console.log(`  Gate decision: ${icon} ${check}`);
    console.log(`  Status:        ${result.rpp_status}`);
    if (result.failures.length > 0) {
        console.log(`  ${RED}Failures:${RESET}`);
        for (const f of result.failures) {
            console.log(`    ${RED}•${RESET} [${f.code}]${f.stage ? ` @ ${f.stage}` : ""}`);
            console.log(`      ${DIM}${f.detail}${RESET}`);
        }
    }
    if (result.warnings.length > 0) {
        console.log(`  ${YELLOW}Warnings:${RESET}`);
        for (const w of result.warnings) {
            console.log(`    ${YELLOW}•${RESET} [${w.code}]`);
        }
    }
    if (result.allow) {
        console.log(`  ${GREEN}Provenance chain verified — action authorized.${RESET}`);
    }
}
// ---------------------------------------------------------------------------
// Scenario: agent is about to run `rm -rf /etc/app/prod.config`
// ---------------------------------------------------------------------------
header("RPP Gate Demo — 'Delete prod config' scenario");
console.log(`
  An AI agent has decided to delete a production config file.
  Before the tool call executes, the gate validates the RPP record.
  We run the same action 6 times with different RPP records.
`);
// ---------------------------------------------------------------------------
// ROUND 1 — No RPP at all
// ---------------------------------------------------------------------------
section("ROUND 1 — No RPP record (agent forgot to include it)");
printResult("null record → gate", runRPPGate(null), false /* expect: blocked */);
// ---------------------------------------------------------------------------
// ROUND 2 — RPP present but missing the action stage
// ---------------------------------------------------------------------------
section("ROUND 2 — RPP present, but action stage is missing");
console.log(`  ${DIM}  The agent wrote interpretation + reasoning + decision, but no action step.${RESET}`);
console.log(`  ${DIM}  "I thought about it" is not the same as "I declared what I will do."${RESET}`);
const missingActionRecord = {
    call_id: "demo-missing-action",
    steps: [
        {
            id: "s-interp",
            stage: "interpretation",
            content: ["User asked to clean up stale config files"],
            references: [{ type: "evidence", source: "user_input", locator: "message.current", supports: "task description" }],
        },
        {
            id: "s-reason",
            stage: "reasoning",
            content: ["prod.config has not been modified in 90 days and matches the backup"],
            references: [{ type: "evidence", source: "file", locator: "/etc/app/prod.config", supports: "staleness evidence" }],
        },
        {
            id: "s-decision",
            stage: "decision",
            content: ["Delete prod.config — it is stale and backed up"],
            references: [{ type: "rule", rule_id: "RUL-002", supports: "deletion authorized by cleanup rule" }],
        },
        // action stage intentionally omitted
    ],
    response: {
        content: ["Deleting prod.config"],
        references: [{ type: "derived", from_steps: ["s-decision"], supports: "derived from decision" }],
    },
};
printResult("missing action stage → gate", runRPPGate(missingActionRecord), false);
// ---------------------------------------------------------------------------
// ROUND 3 — Action stage present but no evidence ref
// ---------------------------------------------------------------------------
section("ROUND 3 — Action stage has no evidence reference");
console.log(`  ${DIM}  Rule says deletion is allowed. But which file? On which server?${RESET}`);
console.log(`  ${DIM}  A rule authorizes a *category* of action. Evidence grounds the *specific* thing.${RESET}`);
const noEvidenceRecord = {
    call_id: "demo-no-evidence",
    steps: [
        {
            id: "s-interp",
            stage: "interpretation",
            content: ["User asked to clean up stale config files"],
            references: [{ type: "evidence", source: "user_input", locator: "message.current", supports: "task description" }],
        },
        {
            id: "s-reason",
            stage: "reasoning",
            content: ["prod.config is stale"],
            references: [{ type: "evidence", source: "file", locator: "/etc/app/prod.config", supports: "file exists and is stale" }],
        },
        {
            id: "s-decision",
            stage: "decision",
            content: ["Delete stale config"],
            references: [{ type: "rule", rule_id: "RUL-002", supports: "cleanup rule authorizes deletion" }],
        },
        {
            id: "s-action",
            stage: "action",
            content: ["Run: rm -rf /etc/app/prod.config"],
            references: [
                { type: "rule", rule_id: "RUL-002", supports: "authorized by cleanup rule" },
                // no evidence ref — gate must block this
            ],
        },
    ],
    response: {
        content: ["Deleting prod.config"],
        references: [{ type: "derived", from_steps: ["s-decision", "s-action"], supports: "derived from decision and action" }],
    },
};
printResult("action no evidence → gate", runRPPGate(noEvidenceRecord), false);
// ---------------------------------------------------------------------------
// ROUND 4 — Structurally valid, but policy blocks unknown rule id
// ---------------------------------------------------------------------------
section("ROUND 4 — Structurally valid, but policy: allowed_rule_ids in effect");
console.log(`  ${DIM}  This project only allows RUL-001 through RUL-004.${RESET}`);
console.log(`  ${DIM}  The agent cited RUL-999 which is not in the project registry.${RESET}`);
console.log(`  ${DIM}  Without policy: this passes. With policy: blocked.${RESET}`);
const unknownRuleRecord = {
    call_id: "demo-unknown-rule",
    steps: [
        {
            id: "s-interp",
            stage: "interpretation",
            content: ["User asked to clean up stale config files"],
            references: [{ type: "evidence", source: "user_input", locator: "message.current", supports: "task description" }],
        },
        {
            id: "s-reason",
            stage: "reasoning",
            content: ["prod.config is stale"],
            references: [{ type: "evidence", source: "file", locator: "/etc/app/prod.config", supports: "file is stale" }],
        },
        {
            id: "s-decision",
            stage: "decision",
            content: ["Delete stale config"],
            references: [{ type: "rule", rule_id: "RUL-999", supports: "some rule I made up" }],
            // ^^^^^^^ not in project registry
        },
        {
            id: "s-action",
            stage: "action",
            content: ["Run: rm -rf /etc/app/prod.config"],
            references: [
                { type: "rule", rule_id: "RUL-999", supports: "authorized" },
                { type: "evidence", source: "file", locator: "/etc/app/prod.config", supports: "target file" },
            ],
        },
    ],
    response: {
        content: ["Deleting prod.config"],
        references: [{ type: "derived", from_steps: ["s-decision", "s-action"], supports: "derived from decision and action" }],
    },
};
const projectPolicy = {
    allowed_rule_ids: ["RUL-001", "RUL-002", "RUL-003", "RUL-004"],
    allowed_method_ids: ["RCA-001", "OBS-001", "DBG-001", "LST-001"],
    action_evidence_sources: ["file", "test_output", "log", "approval"],
};
console.log(`\n  ${DIM}Without policy:${RESET}`);
printResult("unknown rule, no policy", runRPPGate(unknownRuleRecord), true);
console.log(`\n  ${DIM}With policy (allowed_rule_ids: ["RUL-001"..."RUL-004"]):${RESET}`);
printResult("unknown rule + policy", runRPPGate(unknownRuleRecord, { policy: projectPolicy }), false);
// ---------------------------------------------------------------------------
// ROUND 5 — Policy + ExtraCheck: production files need approval evidence
// ---------------------------------------------------------------------------
section("ROUND 5 — ExtraCheck: prod files require approval evidence");
console.log(`  ${DIM}  Project rule: any action touching /etc/app/prod.* must cite an approval.${RESET}`);
console.log(`  ${DIM}  This is too specific for core invariants — it lives in rpp-checks.js.${RESET}`);
const requireApprovalForProd = (record, _ctx) => {
    const actionStep = record.steps.find(s => s.stage === "action");
    if (!actionStep)
        return [];
    const touchesProd = actionStep.content.some(c => c.includes("/etc/app/prod"));
    if (!touchesProd)
        return [];
    const hasApproval = actionStep.references.some(r => r.type === "evidence" && r.source === "approval");
    if (hasApproval)
        return [];
    return [{
            code: "ACTION_NO_EVIDENCE",
            stage: "action",
            detail: "Actions touching /etc/app/prod.* must cite an approval evidence ref (source: 'approval').",
        }];
};
const validButNoApproval = {
    call_id: "demo-no-approval",
    steps: [
        {
            id: "s-interp",
            stage: "interpretation",
            content: ["User asked to clean up stale config files"],
            references: [{ type: "evidence", source: "user_input", locator: "message.current", supports: "task description" }],
        },
        {
            id: "s-reason",
            stage: "reasoning",
            content: ["prod.config is stale"],
            references: [{ type: "evidence", source: "file", locator: "/etc/app/prod.config", supports: "file is stale" }],
        },
        {
            id: "s-decision",
            stage: "decision",
            content: ["Delete stale config"],
            references: [{ type: "rule", rule_id: "RUL-002", supports: "cleanup rule" }],
        },
        {
            id: "s-action",
            stage: "action",
            content: ["Run: rm -rf /etc/app/prod.config"],
            references: [
                { type: "rule", rule_id: "RUL-002", supports: "authorized" },
                { type: "evidence", source: "file", locator: "/etc/app/prod.config", supports: "target file" },
                // missing: { type: "evidence", source: "approval", locator: "ticket-4821", supports: "approved by ops" }
            ],
        },
    ],
    response: {
        content: ["Deleting prod.config"],
        references: [{ type: "derived", from_steps: ["s-decision", "s-action"], supports: "derived from decision and action" }],
    },
};
console.log(`\n  ${DIM}Without extraCheck (core gate alone):${RESET}`);
printResult("no approval, no extra check", runRPPGate(validButNoApproval), true);
console.log(`\n  ${DIM}With extraCheck (project plugin):${RESET}`);
printResult("no approval + extra check", runRPPGate(validButNoApproval, {
    policy: projectPolicy,
    extraChecks: [requireApprovalForProd],
}), false);
// ---------------------------------------------------------------------------
// ROUND 6 — Fully compliant: all layers satisfied
// ---------------------------------------------------------------------------
section("ROUND 6 — Fully compliant record");
console.log(`  ${DIM}  All core invariants satisfied. Rule id in project registry.${RESET}`);
console.log(`  ${DIM}  Approval evidence present. Gate allows.${RESET}`);
const fullyCompliantRecord = {
    call_id: "demo-compliant",
    steps: [
        {
            id: "s-interp",
            stage: "interpretation",
            content: ["User asked to clean up stale config files"],
            references: [{ type: "evidence", source: "user_input", locator: "message.current", supports: "task description" }],
        },
        {
            id: "s-reason",
            stage: "reasoning",
            content: ["prod.config has not been modified in 90 days and matches the backup"],
            references: [{ type: "evidence", source: "log", locator: "audit-log:2026-01-01", supports: "staleness confirmed" }],
        },
        {
            id: "s-decision",
            stage: "decision",
            content: ["Delete prod.config — stale, backed up, approved"],
            references: [{ type: "rule", rule_id: "RUL-002", supports: "cleanup rule authorizes deletion of stale files" }],
        },
        {
            id: "s-action",
            stage: "action",
            content: ["Run: rm -rf /etc/app/prod.config"],
            references: [
                { type: "rule", rule_id: "RUL-002", supports: "authorized by cleanup rule" },
                { type: "evidence", source: "file", locator: "/etc/app/prod.config", supports: "target file confirmed to exist and be stale" },
                { type: "evidence", source: "approval", locator: "ticket-4821", supports: "ops team approved deletion on 2026-03-29" },
            ],
        },
    ],
    response: {
        content: ["Deleting prod.config"],
        references: [{ type: "derived", from_steps: ["s-decision", "s-action"], supports: "derived from decision and action" }],
    },
};
printResult("fully compliant + policy + extra check", runRPPGate(fullyCompliantRecord, {
    policy: projectPolicy,
    extraChecks: [requireApprovalForProd],
}), true);
// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------
header("Summary");
console.log(`
  Layer 1 — Core invariants (jingu-protocol):
    Round 1: no RPP record               → BLOCKED  (MISSING_STAGE)
    Round 2: action stage missing        → BLOCKED  (MISSING_STAGE)
    Round 3: action has no evidence      → BLOCKED  (ACTION_NO_EVIDENCE)

  Layer 2 — Project policy (rpp-policy.json):
    Round 4: rule_id not in registry     → BLOCKED  (UNKNOWN_RULE_ID)
             same record without policy  → allowed  (core alone is insufficient)

  Layer 3 — Project plugin (rpp-checks.js):
    Round 5: prod file, no approval      → BLOCKED  (extra check fires)
             same record without plugin  → allowed  (policy alone is insufficient)

  Round 6: all layers satisfied          → ALLOWED
`);
//# sourceMappingURL=rpp-three-layer-demo.js.map