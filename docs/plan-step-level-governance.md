# Plan: Step-Level Governance — From Boundary Control to Continuous Admission

## Context

p15 smoke test confirmed: Phase Submission Enforcement (boundary-level) eliminates Pattern A
(protocol violation at ANALYZE). But exposed a deeper gap: agent stuck in DECIDE for 36 steps
with zero governance intervention. Checkpoint escalation fired reminders but agent ignored them.

Root cause: governance only activates at phase boundaries (VerdictAdvance). If agent never
attempts to advance, governance is a NO-OP inside the phase.

## Goal

Upgrade from `phase-boundary governance` to `continuous step-level governance`:
- Every step must produce a structured proposal (or be rejected)
- No-submission timeout = hard failure (not reminder)
- Phase-specific allowed behaviors enforced per step

## Work Streams (4 phases, sequential dependencies)

### WS-1: Hard No-Submission Timeout (P0 — today)
Convert checkpoint escalation Level 2 from "warning + force" to "hard protocol violation + STOP".
Agent gets K steps to submit; after K, execution terminates or force-redirects.

Files: `scripts/step_sections.py`, `scripts/step_monitor_state.py`
Depends on: nothing (builds on existing checkpoint code)

### WS-2: Hard Submission Deadline with Force (P1)
**REVISED** after API analysis: `tool_choice` forcing blocks bash — agent CANNOT call both
submit_phase_record and bash in the same response. Therefore, tool-only enforcement on
every step is impossible without breaking code exploration.

New approach: "Submission Deadline" pattern:
- Agent explores freely with bash (tool_choice=auto)
- At step K-1 (one before deadline), inject warning + set force flag
- At step K, force tool_choice=submit_phase_record (agent loses bash for 1 step)
- If agent still doesn't submit, protocol_violation → STOP

This gives agent (K-1) free steps + 1 forced step before termination.
Phase-specific K values: ANALYZE=12, DECIDE=8, EXECUTE=10, OBSERVE=15.

Files: `scripts/step_sections.py`, `scripts/step_monitor_state.py`, `mini-swe-agent/jingu_model.py`
Depends on: WS-1 (hard timeout as the terminal enforcement)

### WS-3: Failure Attribution + Targeted Retry (P2)
After controlled_verify failure, classify failure type and route retry to specific phase
(DECIDE for wrong_direction, EXECUTE for incomplete_fix, DESIGN for regression).

Files: new `scripts/attribution_engine.py`, `scripts/step_sections.py`, `scripts/retry_controller.py`
Depends on: WS-1 (hard stops prevent infinite loops)

### WS-4: Exploration Enforcement (P3)
Prevent repeated decisions/patches across retries. Track decision signatures and patch hashes.
Force new direction on DECIDE retry.

Files: `scripts/step_monitor_state.py`, `scripts/step_sections.py`
Depends on: WS-3 (attribution tells us WHEN to force new direction)

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Tool-only forcing blocks bash (CONFIRMED) | High | Use "submission deadline" pattern: force only on the K-th step, not every step |
| Hard timeout causes premature STOP | Medium | Set K conservatively (15 for ANALYZE, 8 for DECIDE, 6 for EXECUTE) |
| Exploration enforcement rejects valid similar approaches | Medium | Use semantic similarity threshold, not exact match |
| Attribution misroutes retry | Low | Fallback to full retry if attribution confidence < 0.5 |

## Success Criteria

1. DECIDE stall count = 0 (no phase with 15+ steps without submission)
2. tool_call_rate ~100% (every step produces structured output)
3. same_patch_repeated = 0 across retries
4. resolved_rate >= 23/30 on django benchmark set
