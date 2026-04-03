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
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
// ---------------------------------------------------------------------------
// File scanner
// ---------------------------------------------------------------------------
const SRC_ROOT = new URL("../../src", import.meta.url).pathname;
function allTsFiles(dir) {
    const results = [];
    for (const entry of readdirSync(dir)) {
        const full = join(dir, entry);
        if (statSync(full).isDirectory()) {
            results.push(...allTsFiles(full));
        }
        else if (entry.endsWith(".ts") && !entry.endsWith(".d.ts")) {
            results.push(full);
        }
    }
    return results;
}
function scan(files, pattern, checkName, ignoreTag) {
    const violations = [];
    for (const file of files) {
        const src = readFileSync(file, "utf-8");
        const lines = src.split("\n");
        for (let i = 0; i < lines.length; i++) {
            const line = lines[i];
            if (line.includes(ignoreTag))
                continue;
            // reset lastIndex for global regexes between lines
            pattern.lastIndex = 0;
            const match = pattern.exec(line);
            if (match) {
                violations.push({
                    file: relative(SRC_ROOT, file),
                    line: i + 1,
                    col: match.index + 1,
                    check: checkName,
                    text: line.trim(),
                });
            }
        }
    }
    return violations;
}
function formatViolations(vs) {
    return vs
        .map((v) => `  ${v.file}:${v.line}:${v.col}  [${v.check}]\n    ${v.text}`)
        .join("\n");
}
const files = allTsFiles(SRC_ROOT);
// ---------------------------------------------------------------------------
// Whitelist for policy-core imports
// ---------------------------------------------------------------------------
// trust-gate has ZERO dependency on jingu-policy-core.
// RPP types (RPPRecord, RPPFailure, validateRPP) now come from jingu-protocol.
// Any import from jingu-policy-core in src/ is a layer violation.
const POLICY_CORE_IMPORT_WHITELIST = [];
// ---------------------------------------------------------------------------
// CHECK 1 — No numeric threshold comparisons
//
// Catches:  score > 0.7,  confidence >= 3,  warnings.length < 2
// Rationale: thresholds are rule parameters; they live in policy-core.
//
// False positives:
//   - "length > 0" checks (is-empty) are mechanism, not policy.
//     These are explicitly allowed — the pattern skips them.
//   - "=== 0" / "!== 0" are also allowed for the same reason.
// ---------------------------------------------------------------------------
test("CHECK 1: no numeric threshold comparisons in src/", () => {
    // Matches: > 0.N, >= 0.N, < 0.N, <= 0.N  (float thresholds)
    // Matches: > N, >= N, < N, <= N  where N >= 2  (integer thresholds; skips 0 and 1)
    //   N=0 is the is-empty pattern (.length > 0, .length === 0) — allowed
    //   N=1 is a cardinality check (.length > 1, "more than one") — allowed
    const THRESHOLD = /[><]=?\s*(0\.\d+|\b[2-9]\d*\b)/g;
    const vs = scan(files, THRESHOLD, "NUMERIC_THRESHOLD", "policy-leak-ignore");
    assert.equal(vs.length, 0, `Policy leakage — numeric thresholds found in trust-gate/src/:\n${formatViolations(vs)}\n\n` +
        `Thresholds are rule parameters and must live in policy-core.\n` +
        `To suppress a false positive add:  // policy-leak-ignore: <reason>`);
});
// ---------------------------------------------------------------------------
// CHECK 2 — No score/confidence/weight field comparisons
//
// Catches:  result.score > X,  unit.confidence < Y,  .weight >=
// Rationale: these fields are outputs of policy evaluation; trust-gate
//   must not re-interpret them — it only routes based on overall_status.
// ---------------------------------------------------------------------------
test("CHECK 2: no score/confidence/weight field comparisons in src/", () => {
    const SCORE_FIELD = /\.(score|confidence|weight|probability|threshold)\s*[><=!]/g;
    const vs = scan(files, SCORE_FIELD, "SCORE_COMPARISON", "policy-leak-ignore");
    assert.equal(vs.length, 0, `Policy leakage — score/confidence/weight comparisons found in trust-gate/src/:\n${formatViolations(vs)}\n\n` +
        `These fields are outputs of policy-core evaluation; trust-gate must not re-interpret them.\n` +
        `Route on overall_status or decision instead.\n` +
        `To suppress a false positive add:  // policy-leak-ignore: <reason>`);
});
// ---------------------------------------------------------------------------
// CHECK 3 — No non-whitelisted imports from jingu-policy-core
//
// Catches:  import { evaluateV4, allPolicies, P1 } from "jingu-policy-core"
// Rationale: trust-gate must not call policy evaluation functions directly.
//   It calls them via the GatePolicy interface (caller-supplied) or via the
//   one sanctioned entry point: validateRPP (for the RPP gate module).
//
// Whitelist: RPPRecord, RPPFailure, RPPValidationResult, validateRPP
// ---------------------------------------------------------------------------
test("CHECK 3: no non-whitelisted imports from jingu-policy-core in src/", () => {
    const violations = [];
    for (const file of files) {
        const src = readFileSync(file, "utf-8");
        const lines = src.split("\n");
        for (let i = 0; i < lines.length; i++) {
            const line = lines[i];
            if (line.includes("policy-leak-ignore"))
                continue;
            if (!line.includes("jingu-policy-core"))
                continue;
            // Extract all imported symbols from this line
            // Handles: import { A, B, C } from "..."  and  import type { A } from "..."
            const symbolMatch = line.match(/import\s+(?:type\s+)?\{([^}]+)\}/);
            if (!symbolMatch)
                continue;
            const symbols = symbolMatch[1]
                .split(",")
                .map((s) => s.trim())
                .filter(Boolean);
            for (const sym of symbols) {
                if (!POLICY_CORE_IMPORT_WHITELIST.includes(sym)) {
                    violations.push({
                        file: relative(SRC_ROOT, file),
                        line: i + 1,
                        col: line.indexOf(sym) + 1,
                        check: "NON_WHITELISTED_IMPORT",
                        text: `'${sym}' — ${line.trim()}`,
                    });
                }
            }
        }
    }
    assert.equal(violations.length, 0, `Policy leakage — non-whitelisted jingu-policy-core imports found in trust-gate/src/:\n${formatViolations(violations)}\n\n` +
        `Allowed symbols: ${POLICY_CORE_IMPORT_WHITELIST.join(", ")}\n` +
        `If a new symbol is needed, justify it and add to the whitelist in this file.\n` +
        `To suppress a false positive add:  // policy-leak-ignore: <reason>`);
});
// ---------------------------------------------------------------------------
// CHECK 4 — No direct domain field access in conditionals
//
// Catches:  if (proposal.claims  if (record.stage  if (unit.reasoning
// Rationale: trust-gate must not look inside domain objects to make decisions.
//   Domain field inspection is what policy-core's evaluateUnit() does.
//   If trust-gate needs to act on a domain field, that logic belongs in a
//   GatePolicy implementation or in policy-core.
//
// Allowed:
//   - proposal.id, proposal.kind, proposal.units  (structural envelope fields)
//   - unit.id, unit.unitId  (identity fields)
//   - evaluationResult.decision, .reasonCode, .unitId  (gate protocol fields)
// ---------------------------------------------------------------------------
test("CHECK 4: no direct domain field access in conditionals in src/", () => {
    // Domain fields: anything that looks like reading semantic content of a proposal/unit
    // Heuristic: if (...proposal.<semantic>  or  if (...unit.<semantic>  or  if (...record.<semantic>
    // where <semantic> is NOT a structural/identity field.
    const STRUCTURAL_FIELDS = new Set([
        "id", "unitId", "kind", "units", "length",
        "proposalId", "proposalKind",
        "decision", "reasonCode", "newGrade", "annotations",
        "errors", "conflictCode", "unitIds", "sources", "severity",
        "supportIds", "supportRefs", "status", "appliedGrades",
        "evaluationResults", "conflictAnnotations",
    ]);
    const violations = [];
    for (const file of files) {
        const src = readFileSync(file, "utf-8");
        const lines = src.split("\n");
        for (let i = 0; i < lines.length; i++) {
            const line = lines[i];
            if (line.includes("policy-leak-ignore"))
                continue;
            // Look for: if (...  with a domain object field access
            // Pattern: "if" or ternary "?" with  proposal./unit./record.  followed by a non-structural field
            const ifMatch = line.match(/\bif\s*\(.*\b(proposal|unit|record|claim)\s*\.\s*([a-zA-Z_]\w*)/);
            if (!ifMatch)
                continue;
            const fieldName = ifMatch[2];
            if (STRUCTURAL_FIELDS.has(fieldName))
                continue;
            violations.push({
                file: relative(SRC_ROOT, file),
                line: i + 1,
                col: line.indexOf(ifMatch[0]) + 1,
                check: "DOMAIN_FIELD_IN_CONDITIONAL",
                text: line.trim(),
            });
        }
    }
    assert.equal(violations.length, 0, `Policy leakage — domain field access in conditionals found in trust-gate/src/:\n${formatViolations(violations)}\n\n` +
        `trust-gate must not inspect domain object contents to make decisions.\n` +
        `Move domain field logic into a GatePolicy.evaluateUnit() implementation or into policy-core.\n` +
        `To suppress a false positive add:  // policy-leak-ignore: <reason>`);
});
//# sourceMappingURL=policy-leak.test.js.map