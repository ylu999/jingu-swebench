/**
 * gate_runner.js — subprocess entry point for jingu-trust-gate B1.
 *
 * Protocol:
 *   stdin:  JSON { patch_text, support_pool, proposal_id, options? }
 *   stdout: JSON { ok, admitted, rejected, reason_codes, explanation, error? }
 *
 * Called from Python via:
 *   subprocess.run(["node", "scripts/gate_runner.js"], input=json_str, capture_output=True)
 *
 * support_pool item shape (mirrors jingu-trust-gate SupportRef):
 *   { id, sourceType, sourceId, confidence?, attributes?, retrievedAt? }
 *
 * options (all optional, Layer 3 tunable):
 *   require_trajectory: bool (default true)
 *   max_files_changed: int (default 10)
 */

// Resolve paths via env vars for portability (local dev vs cloud)
const _GATE_DIST = process.env.JINGU_TRUST_GATE_DIST
  ?? `${process.env.HOME}/jingu-swebench/jingu-trust-gate/dist/src`;
const _SCRIPTS_DIR = process.env.JINGU_SWEBENCH_SCRIPTS
  ?? `${process.env.HOME}/jingu-swebench/scripts`;

const { createTrustGate } = await import(`${_GATE_DIST}/trust-gate.js`);
const { PatchAdmissionPolicy, parsePatchHunks, GATE_PARAMS } =
  await import(`${_SCRIPTS_DIR}/patch_admission_policy.js`);

// ── Noop audit writer (B1: no persistent audit file needed yet) ───────────────

const noopAuditWriter = {
  async append(_entry) {},
};

// ── Main ──────────────────────────────────────────────────────────────────────

async function main() {
  let input;
  try {
    const raw = await readStdin();
    input = JSON.parse(raw);
  } catch (e) {
    writeResult({ ok: false, error: `stdin parse error: ${e.message}` });
    process.exit(1);
  }

  const { patch_text, support_pool = [], proposal_id = "patch-proposal", options = {} } = input;

  if (!patch_text || typeof patch_text !== "string") {
    writeResult({ ok: false, error: "missing or invalid patch_text" });
    process.exit(1);
  }

  // Apply Layer 3 options
  if (options.require_trajectory !== undefined) {
    GATE_PARAMS.require_trajectory = options.require_trajectory;
  }
  if (options.max_files_changed !== undefined) {
    GATE_PARAMS.max_files_changed = options.max_files_changed;
  }

  // Parse patch → hunks
  const hunks = parsePatchHunks(patch_text);

  if (hunks.length === 0) {
    writeResult({
      ok: true,
      admitted: false,
      rejected: true,
      reason_codes: ["EMPTY_PATCH"],
      explanation: {
        totalUnits: 0,
        approved: 0,
        downgraded: 0,
        conflicts: 0,
        rejected: 0,
        retryAttempts: 1,
        gateReasonCodes: ["EMPTY_PATCH"],
      },
      admitted_units: [],
      rejected_units: [],
      retry_feedback: null,
    });
    return;
  }

  // Build Proposal
  const proposal = {
    id: proposal_id,
    kind: "mutation",
    units: hunks,
    metadata: { stage: "B1", source: "mini-swe-agent" },
  };

  // Normalize support_pool: camelCase for TS gate
  const supportPool = support_pool.map(s => ({
    id: s.id ?? s.source_id ?? `sup-${Math.random().toString(36).slice(2)}`,
    sourceType: s.sourceType ?? s.source_type ?? "unknown",
    sourceId: s.sourceId ?? s.source_id ?? s.id ?? "",
    confidence: s.confidence ?? null,
    attributes: s.attributes ?? {},
    retrievedAt: s.retrievedAt ?? s.retrieved_at ?? null,
  }));

  // Run gate
  const policy = new PatchAdmissionPolicy();
  const gate = createTrustGate({ policy, auditWriter: noopAuditWriter });

  let result;
  try {
    result = await gate.admit(proposal, supportPool);
  } catch (e) {
    writeResult({ ok: false, error: `gate.admit error: ${e.message}\n${e.stack}` });
    process.exit(1);
  }

  const explanation = gate.explain(result);

  // Determine overall admission decision:
  // admitted = all hunks either approved or downgraded (none rejected)
  const hasRejected = result.rejectedUnits.length > 0;
  const hasBlockingConflict = result.admittedUnits.some(u =>
    (u.conflictAnnotations ?? []).some(c => c.severity === "blocking")
  );

  const admitted = !hasRejected && !hasBlockingConflict;

  // Build retry feedback if rejected
  let retryFeedback = null;
  if (!admitted) {
    const allResults = [
      ...result.admittedUnits.flatMap(u => u.evaluationResults),
      ...result.rejectedUnits.flatMap(u => u.evaluationResults),
    ];
    retryFeedback = policy.buildRetryFeedback(allResults, {
      attempt: 1,
      maxRetries: 3,
      proposalId: proposal_id,
    });
  }

  // Serialize units (omit heavy content field for stdout efficiency)
  const serializeUnit = u => ({
    unit_id: u.unitId,
    status: u.status,
    file_path: u.unit.file_path,
    hunk_header: u.unit.hunk_header,
    applied_grades: u.appliedGrades ?? [],
    reason_codes: (u.evaluationResults ?? []).map(r => r.reasonCode),
    annotations: (u.evaluationResults ?? []).map(r => r.annotations ?? {}),
    conflict_annotations: u.conflictAnnotations ?? [],
  });

  writeResult({
    ok: true,
    admitted,
    rejected: !admitted,
    reason_codes: explanation.gateReasonCodes,
    explanation: {
      totalUnits: explanation.totalUnits,
      approved: explanation.approved,
      downgraded: explanation.downgraded,
      conflicts: explanation.conflicts,
      rejected: explanation.rejected,
      retryAttempts: explanation.retryAttempts,
      gateReasonCodes: explanation.gateReasonCodes,
    },
    admitted_units: result.admittedUnits.map(serializeUnit),
    rejected_units: result.rejectedUnits.map(serializeUnit),
    retry_feedback: retryFeedback,
  });
}

// ── I/O helpers ───────────────────────────────────────────────────────────────

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", chunk => { data += chunk; });
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", reject);
  });
}

function writeResult(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

main().catch(e => {
  writeResult({ ok: false, error: `unhandled error: ${e.message}\n${e.stack}` });
  process.exit(1);
});
