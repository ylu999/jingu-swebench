/**
 * B1 PatchAdmissionPolicy — post-hoc gate for SWE-bench patch admission.
 *
 * Use case: mini-SWE-agent produces a patch. Before the patch is submitted
 * as a prediction, jingu-trust-gate evaluates it against trajectory evidence.
 *
 * Unit type: PatchHunk
 *   One unit = one diff hunk (@@ ... @@).
 *   The gate evaluates each hunk independently for structural validity and
 *   trajectory support, then admits or rejects the whole patch accordingly.
 *
 * Support pool (built from traj.json by jingu_gate_bridge.py):
 *   - source_type: "exit_status"      — agent exit status (e.g. "submitted", "LimitsExceeded")
 *   - source_type: "apply_result"     — did git apply succeed? (attributes.success: bool)
 *   - source_type: "test_output"      — test run output excerpt (attributes.passed: bool)
 *   - source_type: "task_description" — problem_statement excerpt (always present)
 *   - source_type: "jingu_body"       — structured agent behavior summary (B1+, optional)
 *
 * Gate rules (evaluateUnit):
 *   R1  hunk has no diff markers (--- / +++ / @@)     → PARSE_FAILED           → reject
 *   R2  no trajectory evidence in support pool        → NO_TRAJECTORY_EVIDENCE → downgrade to "speculative"
 *   R3  apply_result support says success=false       → APPLY_FAILED           → reject
 *   R4  exit_status is LimitsExceeded (no submit)     → LIMITS_EXCEEDED        → downgrade to "speculative"
 *   R5  jingu_body says no files_written (if present) → NO_FILES_WRITTEN       → downgrade to "speculative"
 *   R6  all checks pass                               → approve
 *
 * Conflict detection:
 *   C1  two hunks touch overlapping line ranges in the same file → OVERLAPPING_HUNKS → blocking
 *
 * Stage: B1 (trust-gate active, no policy-core cognition yet).
 * Layer 2 tunable: strictness (reject vs downgrade thresholds), evidence requirements.
 * Layer 3 tunable: min_hunk_lines, max_files_changed.
 */

// Resolve jingu-trust-gate dist — use env var for portability (local dev vs cloud)
import { homedir } from "os";
const _HOME = process.env.HOME || homedir();
const _GATE_DIST = process.env.JINGU_TRUST_GATE_DIST
  ?? `${_HOME}/jingu-swebench/jingu-trust-gate/dist/src`;
const { approve, reject, downgrade, firstFailing } =
  await import(`${_GATE_DIST}/helpers/index.js`);

// ── Layer 3 runtime params (loop-tunable) ─────────────────────────────────────

export const GATE_PARAMS = {
  min_hunk_lines: 1,       // min lines changed per hunk to be considered non-trivial
  max_files_changed: 10,   // reject if patch touches more than N files
  require_trajectory: true, // if false, R2 becomes a warning not a downgrade
};

// ── Domain types (JSDoc) ──────────────────────────────────────────────────────

/**
 * @typedef {Object} PatchHunk
 * @property {string} id            - unique hunk id, e.g. "hunk-0"
 * @property {string} file_path     - target file path from +++ b/... line
 * @property {string} hunk_header   - the @@ -N,M +N,M @@ line
 * @property {string} content       - full hunk text including header
 * @property {string[]} evidence_refs - SupportRef.sourceId values that justify this hunk
 * @property {number} old_start     - old file line start
 * @property {number} old_count     - old file line count
 * @property {number} new_start     - new file line start
 * @property {number} new_count     - new file line count
 */

// ── Policy ────────────────────────────────────────────────────────────────────

export class PatchAdmissionPolicy {

  // Step 1 — proposal-level structural check
  validateStructure(proposal) {
    const errors = [];
    if (!proposal.units || proposal.units.length === 0) {
      errors.push({
        field: "units",
        reasonCode: "EMPTY_PATCH",
        message: "Patch has no hunks — nothing to admit",
      });
      return { kind: "structure", valid: false, errors };
    }

    const filesChanged = new Set(proposal.units.map(u => u.file_path)).size;
    if (filesChanged > GATE_PARAMS.max_files_changed) {
      errors.push({
        field: "units",
        reasonCode: "TOO_MANY_FILES",
        message: `Patch touches ${filesChanged} files (limit: ${GATE_PARAMS.max_files_changed})`,
      });
    }

    return { kind: "structure", valid: errors.length === 0, errors };
  }

  // Step 2 — bind support to each unit
  bindSupport(unit, supportPool) {
    // Bind global evidence (apply_result, exit_status, task_description, jingu_body) to every hunk
    const globalTypes = new Set(["apply_result", "exit_status", "task_description", "jingu_body"]);
    const global = supportPool.filter(s => globalTypes.has(s.sourceType));

    // Also bind any refs explicitly declared in unit.evidence_refs
    const explicit = supportPool.filter(s => unit.evidence_refs.includes(s.sourceId));

    const all = [...new Map([...global, ...explicit].map(s => [s.id, s])).values()];
    return {
      unit,
      supportIds: all.map(s => s.id),
      supportRefs: all,
    };
  }

  // Step 3 — unit-level semantic evaluation
  evaluateUnit(uws, ctx) {
    return firstFailing([
      this._checkStructure(uws),
      this._checkApplyFailed(uws),
      this._checkLimitsExceeded(uws),
      this._checkTrajectoryEvidence(uws),
      this._checkJinguBody(uws),
    ]) ?? approve(uws.unit.id);
  }

  _checkStructure(uws) {
    const { content, hunk_header } = uws.unit;
    if (!hunk_header || !hunk_header.startsWith("@@")) {
      return reject(uws.unit.id, "PARSE_FAILED", {
        note: "Hunk has no @@ header — diff is malformed",
      });
    }
    if (!content || content.trim().length < 3) {
      return reject(uws.unit.id, "EMPTY_HUNK", {
        note: "Hunk content is empty or trivial",
      });
    }
    return undefined;
  }

  _checkApplyFailed(uws) {
    const applyRef = uws.supportRefs.find(s => s.sourceType === "apply_result");
    if (applyRef && applyRef.attributes?.success === false) {
      return reject(uws.unit.id, "APPLY_FAILED", {
        note: `git apply failed: ${applyRef.attributes?.error ?? "unknown error"}`,
        applyRef: applyRef.sourceId,
      });
    }
    return undefined;
  }

  _checkLimitsExceeded(uws) {
    const exitRef = uws.supportRefs.find(s => s.sourceType === "exit_status");
    if (exitRef && exitRef.attributes?.status === "LimitsExceeded") {
      // LimitsExceeded means agent ran out of steps — patch may be incomplete
      return downgrade(uws.unit.id, "LIMITS_EXCEEDED", "speculative", {
        note: "Agent hit step limit before submitting — patch may be incomplete. "
            + "Admitted as speculative.",
        exitRef: exitRef.sourceId,
      });
    }
    return undefined;
  }

  _checkTrajectoryEvidence(uws) {
    if (!GATE_PARAMS.require_trajectory) return undefined;
    const hasEvidence = uws.supportRefs.some(s =>
      ["apply_result", "exit_status", "test_output"].includes(s.sourceType)
    );
    if (!hasEvidence) {
      return downgrade(uws.unit.id, "NO_TRAJECTORY_EVIDENCE", "speculative", {
        note: "No trajectory evidence bound to this hunk. "
            + "Patch admitted as speculative — no execution confirmation.",
      });
    }
    return undefined;
  }

  _checkJinguBody(uws) {
    // R5: if jingu_body is present, validate consistency with patch
    // NOTE: patch-derived files_written is always authoritative (ground truth).
    // This rule only fires when patch_files_changed == 0 AND files_written == 0,
    // meaning the patch truly has no file changes at all.
    const bodyRef = uws.supportRefs.find(s => s.sourceType === "jingu_body");
    if (!bodyRef) return undefined;  // jingu_body is optional — skip if absent

    const attrs = bodyRef.attributes ?? {};
    const patchFiles = attrs.patch_files_changed ?? 0;
    const filesWritten = Array.isArray(attrs.files_written) ? attrs.files_written.length : 0;

    // Only downgrade if the patch itself has no file changes (not just no traj write signal)
    // files_written from patch is ground truth — if patch touched files, agent did write
    if (patchFiles === 0 && filesWritten === 0 && (attrs.patch_hunks ?? 0) > 0) {
      return downgrade(uws.unit.id, "NO_FILES_WRITTEN", "speculative", {
        note: "jingu_body: patch has hunks but reports 0 files changed. "
            + "Patch structure may be malformed — admitted as speculative.",
        bodyRef: bodyRef.sourceId,
      });
    }

    return undefined;
  }

  // Step 4 — cross-unit conflict detection
  detectConflicts(units, supportPool) {
    const conflicts = [];
    // Group hunks by file
    const byFile = new Map();
    for (const uws of units) {
      const fp = uws.unit.file_path;
      if (!byFile.has(fp)) byFile.set(fp, []);
      byFile.get(fp).push(uws);
    }

    for (const [filePath, fileUnits] of byFile) {
      for (let i = 0; i < fileUnits.length; i++) {
        for (let j = i + 1; j < fileUnits.length; j++) {
          const a = fileUnits[i].unit;
          const b = fileUnits[j].unit;
          if (_rangesOverlap(a.old_start, a.old_count, b.old_start, b.old_count)) {
            conflicts.push({
              unitIds: [a.id, b.id],
              conflictCode: "OVERLAPPING_HUNKS",
              sources: [],
              severity: "blocking",
              description: `Hunks ${a.id} and ${b.id} overlap in ${filePath} `
                         + `(lines ${a.old_start}-${a.old_start + a.old_count} vs `
                         + `${b.old_start}-${b.old_start + b.old_count})`,
            });
          }
        }
      }
    }
    return conflicts;
  }

  // Step 5 — render admitted units → VerifiedContext
  render(admittedUnits, supportPool, context) {
    const blocks = admittedUnits.map(u => {
      const grade = u.appliedGrades[u.appliedGrades.length - 1] ?? "proven";
      const exitRef = supportPool.find(s => s.sourceType === "exit_status");
      const exitStatus = exitRef?.attributes?.status ?? "unknown";
      return {
        sourceId: u.unitId,
        content: `${u.unit.file_path}: ${u.unit.hunk_header}`,
        grade,
        conflictNote: u.conflictAnnotations.length > 0
          ? u.conflictAnnotations.map(c => c.conflictCode).join(", ")
          : null,
        unsupportedAttributes: u.status === "downgraded"
          ? [u.evaluationResults[0]?.reasonCode ?? "UNKNOWN"]
          : [],
      };
    });

    const speculative = admittedUnits.filter(u => {
      const g = u.appliedGrades[u.appliedGrades.length - 1];
      return g === "speculative";
    });

    return {
      admittedBlocks: blocks,
      summary: {
        admitted: admittedUnits.length,
        rejected: 0, // filled in by TrustGate.render()
        conflicts: admittedUnits.filter(u => u.status === "approved_with_conflict").length,
      },
      instructions: speculative.length > 0
        ? `${speculative.length} hunk(s) admitted as speculative (incomplete trajectory evidence). `
        + `Review carefully before applying.`
        : "All hunks admitted with trajectory evidence.",
    };
  }

  // Step 6 — retry feedback
  buildRetryFeedback(unitResults, context) {
    const failed = unitResults.filter(r => r.decision === "reject");
    const hints = {
      PARSE_FAILED: "The patch is malformed — ensure it contains valid diff markers (---, +++, @@).",
      APPLY_FAILED: "The patch failed to apply — check for merge conflicts or incorrect line numbers.",
      EMPTY_PATCH: "No patch was generated — the agent must produce a diff before submitting.",
      EMPTY_HUNK: "A hunk has no content — ensure the diff includes actual line changes.",
      TOO_MANY_FILES: `Patch touches too many files. Focus on the minimum files needed.`,
      NO_FILES_WRITTEN: "Agent trajectory shows no files were written — make sure to actually apply your fix before submitting.",
    };

    const errors = failed.map(r => ({
      reasonCode: r.reasonCode,
      unitId: r.unitId,
      details: r.annotations ?? {},
    }));

    const hintLines = [...new Set(failed.map(r => hints[r.reasonCode]))]
      .filter(Boolean)
      .map(h => `- ${h}`)
      .join("\n");

    return {
      summary: `${failed.length} hunk(s) rejected on attempt ${context.attempt}/${context.maxRetries}.\n`
             + (hintLines ? `\nFix required:\n${hintLines}` : ""),
      errors,
    };
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function _rangesOverlap(startA, countA, startB, countB) {
  const endA = startA + countA;
  const endB = startB + countB;
  return startA < endB && startB < endA;
}

/**
 * Parse a unified diff text into PatchHunk units.
 * Called by gate_runner.js to build the Proposal from a raw patch string.
 *
 * @param {string} patchText - unified diff string
 * @returns {PatchHunk[]}
 */
export function parsePatchHunks(patchText) {
  const lines = patchText.split("\n");
  const hunks = [];
  let currentFile = "";
  let hunkIdx = 0;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // Track current file from +++ b/path
    if (line.startsWith("+++ b/")) {
      currentFile = line.slice(6).trim();
      continue;
    }
    if (line.startsWith("+++ /dev/null")) {
      currentFile = "(deleted)";
      continue;
    }

    // Hunk header
    const m = line.match(/^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/);
    if (!m) continue;

    const oldStart  = parseInt(m[1], 10);
    const oldCount  = m[2] !== undefined ? parseInt(m[2], 10) : 1;
    const newStart  = parseInt(m[3], 10);
    const newCount  = m[4] !== undefined ? parseInt(m[4], 10) : 1;

    // Collect hunk body
    const hunkLines = [line];
    let j = i + 1;
    while (j < lines.length) {
      const nl = lines[j];
      if (/^(@@ |diff --git |--- |[+]{3} )/.test(nl)) break;
      hunkLines.push(nl);
      j++;
    }

    hunks.push({
      id: `hunk-${hunkIdx++}`,
      file_path: currentFile || "(unknown)",
      hunk_header: line,
      content: hunkLines.join("\n"),
      evidence_refs: [],  // bridge fills these in from traj
      old_start: oldStart,
      old_count: oldCount,
      new_start: newStart,
      new_count: newCount,
    });
  }

  return hunks;
}
