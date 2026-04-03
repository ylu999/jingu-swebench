import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { runRPPGate } from "../../../src/rpp/rpp-gate.js";
import { GateRunner } from "../../../src/gate/gate-runner.js";
// ---------------------------------------------------------------------------
// Helpers: build minimal valid RPP fixtures
// ---------------------------------------------------------------------------
function makeValidRPPRecord() {
    return {
        call_id: "call-test-001",
        steps: [
            {
                id: "s-interpretation",
                stage: "interpretation",
                content: ["interpretation content that is real"],
                references: [
                    {
                        type: "evidence",
                        source: "doc-1",
                        locator: "section-1",
                        supports: "interpretation content that is real",
                    },
                ],
            },
            {
                id: "s-reasoning",
                stage: "reasoning",
                content: ["reasoning content that is real"],
                references: [
                    {
                        type: "evidence",
                        source: "doc-1",
                        locator: "section-2",
                        supports: "reasoning content that is real",
                    },
                ],
            },
            {
                id: "s-decision",
                stage: "decision",
                content: ["decision content that is real"],
                references: [
                    {
                        type: "rule",
                        rule_id: "RULE-1",
                        supports: "decision content that is real",
                    },
                ],
            },
            {
                id: "s-action",
                stage: "action",
                content: ["action content that is real"],
                references: [
                    {
                        type: "rule",
                        rule_id: "RULE-2",
                        supports: "action content that is real",
                    },
                    {
                        type: "evidence",
                        source: "file",
                        locator: "src/gate/gate-runner.ts:1",
                        supports: "file being acted on",
                    },
                ],
            },
        ],
        response: {
            content: ["interpretation content that is real"],
            references: [
                {
                    type: "derived",
                    from_steps: ["s-interpretation", "s-action"],
                    supports: "derived from interpretation and action steps above",
                },
            ],
        },
    };
}
/**
 * A weakly_supported record: all 4 required stages are present and valid, but
 * every reference has an empty 'supports' field (SUPPORTS_TOO_VAGUE warning).
 * SUPPORTS_TOO_VAGUE is a soft/warning failure only, so overall_status = "weakly_supported"
 * and allow = true.
 */
function makeWeaklySupportedRPPRecord() {
    return {
        call_id: "call-test-weak",
        steps: [
            {
                id: "w-interpretation",
                stage: "interpretation",
                content: ["interpretation content that is real"],
                references: [
                    {
                        type: "evidence",
                        source: "doc-1",
                        locator: "section-1",
                        supports: "", // empty → SUPPORTS_TOO_VAGUE (warning)
                    },
                ],
            },
            {
                id: "w-reasoning",
                stage: "reasoning",
                content: ["reasoning content that is real"],
                references: [
                    {
                        type: "evidence",
                        source: "doc-1",
                        locator: "section-2",
                        supports: "", // empty → SUPPORTS_TOO_VAGUE (warning)
                    },
                ],
            },
            {
                id: "w-decision",
                stage: "decision",
                content: ["decision content that is real"],
                references: [
                    {
                        type: "rule",
                        rule_id: "RULE-1",
                        supports: "", // empty → SUPPORTS_TOO_VAGUE (warning)
                    },
                ],
            },
            {
                id: "w-action",
                stage: "action",
                content: ["action content that is real"],
                references: [
                    {
                        type: "rule",
                        rule_id: "RULE-2",
                        supports: "", // empty → SUPPORTS_TOO_VAGUE (warning)
                    },
                    {
                        type: "evidence",
                        source: "file",
                        locator: "src/gate/gate-runner.ts:1",
                        supports: "", // empty → SUPPORTS_TOO_VAGUE (warning)
                    },
                ],
            },
        ],
        response: {
            content: ["interpretation content that is real"],
            references: [
                {
                    type: "derived",
                    from_steps: ["w-decision", "w-action"], // non-empty, valid step ids with rule refs
                    supports: "", // empty → SUPPORTS_TOO_VAGUE (warning)
                },
            ],
        },
    };
}
function makeProposal(units, rppRecord) {
    const p = {
        id: "prop-rpp-1",
        kind: "response",
        units,
    };
    if (rppRecord !== undefined && rppRecord !== null) {
        p.rpp_record = rppRecord;
    }
    return p;
}
const noSupport = [];
function makePassingPolicy() {
    return {
        validateStructure: () => ({ kind: "structure", valid: true, errors: [] }),
        bindSupport: (unit, pool) => ({
            unit,
            supportIds: pool.map((s) => s.id),
            supportRefs: pool,
        }),
        evaluateUnit: ({ unit }) => ({
            kind: "unit",
            unitId: unit.id,
            decision: "approve",
            reasonCode: "OK",
        }),
        detectConflicts: () => [],
        render: (units, _pool, _ctx) => ({
            admittedBlocks: units.map((u) => ({
                sourceId: u.unit.id,
                content: "",
            })),
            summary: { admitted: units.length, rejected: 0, conflicts: 0 },
        }),
        buildRetryFeedback: () => ({ summary: "test", errors: [] }),
    };
}
function makeFailingPolicy() {
    return {
        ...makePassingPolicy(),
        evaluateUnit: ({ unit }) => ({
            kind: "unit",
            unitId: unit.id,
            decision: "reject",
            reasonCode: "POLICY_REJECT",
        }),
    };
}
// ---------------------------------------------------------------------------
// Tests: runRPPGate (unit)
// ---------------------------------------------------------------------------
describe("runRPPGate", () => {
    it("Test 1: null record → allow:false, rpp_status:missing", () => {
        const result = runRPPGate(null);
        assert.equal(result.allow, false);
        assert.equal(result.rpp_status, "missing");
        assert.ok(result.failures.length > 0);
        assert.equal(result.failures[0].code, "MISSING_STAGE");
    });
    it("Test 2: undefined record → allow:false, rpp_status:missing", () => {
        const result = runRPPGate(undefined);
        assert.equal(result.allow, false);
        assert.equal(result.rpp_status, "missing");
        assert.ok(result.failures.length > 0);
        assert.equal(result.failures[0].code, "MISSING_STAGE");
    });
    it("Test 3: valid RPP record → allow:true, rpp_status:valid", () => {
        const record = makeValidRPPRecord();
        const result = runRPPGate(record);
        assert.equal(result.allow, true);
        assert.equal(result.rpp_status, "valid");
        assert.equal(result.failures.length, 0);
    });
    it("Test 4: invalid RPP record (missing a stage) → allow:false, rpp_status:invalid", () => {
        const record = makeValidRPPRecord();
        // Remove the 'action' stage to trigger MISSING_STAGE
        record.steps = record.steps.filter((s) => s.stage !== "action");
        const result = runRPPGate(record);
        assert.equal(result.allow, false);
        assert.equal(result.rpp_status, "invalid");
        assert.ok(result.failures.some((f) => f.code === "MISSING_STAGE"));
    });
    it("Test 5: weakly_supported RPP (only soft failures) → allow:true, rpp_status:weakly_supported", () => {
        const record = makeWeaklySupportedRPPRecord();
        const result = runRPPGate(record);
        assert.equal(result.allow, true);
        assert.equal(result.rpp_status, "weakly_supported");
        assert.equal(result.failures.length, 0);
        assert.ok(result.warnings.length > 0);
        assert.ok(result.warnings.every((w) => w.code === "SUPPORTS_TOO_VAGUE"));
    });
});
// ---------------------------------------------------------------------------
// Tests: GateRunner integration with RPP gate
// ---------------------------------------------------------------------------
describe("GateRunner + RPP gate", () => {
    it("Test 6: valid RPP + mock passing policy → allow:true, admittedUnits non-empty", async () => {
        const rpp = makeValidRPPRecord();
        const policy = makePassingPolicy();
        const runner = new GateRunner(policy);
        const result = await runner.run(makeProposal([{ id: "u1", content: "hello" }], rpp), noSupport);
        assert.equal(result.admittedUnits.length, 1);
        assert.equal(result.rejectedUnits.length, 0);
        assert.equal(result.admittedUnits[0].status, "approved");
    });
    it("Test 7: valid RPP + mock failing policy → allow:false, rejectedUnits non-empty", async () => {
        const rpp = makeValidRPPRecord();
        const policy = makeFailingPolicy();
        const runner = new GateRunner(policy);
        const result = await runner.run(makeProposal([{ id: "u1", content: "hello" }], rpp), noSupport);
        assert.equal(result.admittedUnits.length, 0);
        assert.equal(result.rejectedUnits.length, 1);
        assert.equal(result.rejectedUnits[0].status, "rejected");
    });
});
//# sourceMappingURL=rpp-gate.test.js.map