# EFR Iterative Repair Validation — Experiment Plan

## Hypotheses

- **H1 (EFR invoked):** Structured feedback emits failure_type + repair_target + repair_hint + evidence_refs
- **H2 (EFR consumed):** Attempt 2 prompt explicitly references attempt 1 feedback (repair phase, test output)
- **H3 (Phase-specific routing):** Different failure types route to different repair phases
- **H4 (Repair improves efficiency):** Less wasted retry, more targeted repair → higher attempt-2 resolve rate

## Failure Taxonomy (mapping to existing code)

| User Type | Code Type | Routing |
|-----------|-----------|---------|
| F1: execution_format_error | `execution_error` | → EXECUTE |
| F2: semantic_weakening | `wrong_direction` | → ANALYZE |
| F3: insufficient_evidence | `incomplete_fix` | → DESIGN |
| F4: wrong_direction | `wrong_direction` | → ANALYZE/DECIDE |
| F5: verification_gap | `verify_gap` | → JUDGE/ANALYZE/DESIGN |

## Existing Infrastructure (already implemented)

1. `failure_classifier.py`: `classify_failure()` → 4 types, `FAILURE_ROUTING_RULES`, `FailureRecord`
2. `failure_routing.py`: `SEED_FAILURE_MATRIX` (20+ entries), `route_failure(phase, principal)`
3. `repair_prompts.py`: `build_repair_prompt()` → phase-specific repair with evidence
4. `jingu_agent.py` wiring: failure classification → repair_directive → cp_state reset → last_failure assembly
5. NBR/EFR enforcement: RuntimeError on empty feedback

## What's Missing (to implement)

### Step 1: EFR Structured Telemetry
Add signal log lines for measuring H1-H4:
- `[efr-emit]` when structured feedback is generated (failure_type, repair_target, evidence quality)
- `[efr-consume]` when attempt 2 actually uses the feedback (last_failure contains repair prompt)
- `[efr-route]` when phase-specific routing fires (next_phase != current_phase = cross-phase repair)

### Step 2: Feedback Consumption Verification
Add `[efr-ack]` signal: after attempt 2 completes, check if the agent's phase_records show
the repair target phase was actually entered.

### Step 3: Wire Missing Routes
The 3 simplest routes are already wired:
- execution_error → EXECUTE (already in FAILURE_ROUTING_RULES)
- wrong_direction → ANALYZE (already in FAILURE_ROUTING_RULES)
- incomplete_fix → DESIGN (already in FAILURE_ROUTING_RULES)
- verify_gap → EXECUTE (already in FAILURE_ROUTING_RULES)

**No new routing code needed.** The gap is telemetry, not wiring.

## Metrics

### Structure Metrics (from telemetry)
- S1: `efr_emit_rate` — % of multi-attempt instances that emit structured feedback
- S2: `efr_consume_rate` — % of attempt-2 instances where last_failure contains repair prompt
- S3: `efr_route_cross_phase_rate` — % of routes that change phase (not stay-in-phase)
- S4: `efr_ack_rate` — % of attempt-2 where agent enters the prescribed repair phase

### Behavior Metrics (from traj analysis)
- B1: `attempt2_phase_match` — does attempt 2 start in the routed phase?
- B2: `attempt2_references_feedback` — does attempt 2 traj mention failure type or test output?
- B3: `repair_efficiency` — attempt 2 step count vs attempt 1 step count

### Outcome Metrics (from eval)
- O1: `attempt2_resolve_rate` — % of attempt-2 that resolve (vs baseline)
- O2: `flip_rate` — % of instances that fail attempt 1 but resolve attempt 2
- O3: `regression_rate` — % of instances that resolve attempt 1 but fail attempt 2

## Smoke Sequence

- **Smoke 0:** 1 instance (django__django-10097) — verify telemetry lines appear in CloudWatch
- **Smoke 1:** 3 instances (10097, 10973, 11087) — verify all 3 failure types produce different routes
- **Smoke 2:** After telemetry additions — verify ack signals appear
- **Smoke 3:** 5 instances — verify cross-phase routing produces measurable behavior change

## Batch Sequence

- **Batch A:** 10-instance (first 10 of verified set) — measure S1-S4 + B1-B3
- **Batch B:** 10-instance (second 10) — replicate
- **Batch C:** 20-instance integration — final O1-O3 measurement

## Pass/Fail Criteria

| Metric | Pass | Fail |
|--------|------|------|
| S1 (efr_emit_rate) | ≥ 80% | < 50% |
| S2 (efr_consume_rate) | ≥ 80% | < 50% |
| S3 (efr_route_cross_phase_rate) | > 0% (at least 1 cross-phase) | 0% |
| S4 (efr_ack_rate) | ≥ 50% | < 20% |
| O1 (attempt2_resolve_rate) | ≥ baseline (65%) | < baseline - 10% |
| O2 (flip_rate) | > 0 flips | 0 flips |

## Execution Order

1. Add EFR telemetry signals (Step 1)
2. Add feedback consumption verification (Step 2)
3. Verify routes already wired (Step 3 — no code change expected)
4. Smoke 0/1
5. Analyze telemetry, iterate if needed
6. Smoke 2/3
7. Batch A/B
8. 20-instance integration batch
