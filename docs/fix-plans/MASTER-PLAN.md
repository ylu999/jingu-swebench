# Master Plan — Jingu System Activation (p236 Audit Response)

Date: 2026-04-11
Source: p236 system audit + system-level diagnosis
Audit: `docs/audit-p236-system-review.md`

---

## The Single Most Important Conclusion

> **Governance is basically offline. The system is running in degraded fallback mode.**

Direct evidence:

* bundle compile failure -> fallback prompt
* phase_injection = NONE
* principal_section = NONE
* structured extraction = 0
* gate enforcement = surface-only, not enforced

What this means:

```text
The current system ~= a vanilla agent with some regex checks
It is NOT Jingu
```

This is the most important sentence in the entire audit.

---

## Three Independent Failure Chains

The system is broken along three independent chains simultaneously:

### Chain 1: Cognition / Governance is disconnected

**bundle compile failure -> governance fully falls back**

Impact:
* No phase contract
* No principal enforcement
* No structured extraction
* All gates degrade to keyword/regex

This is why:
* extraction_metrics = 0
* principals are only "written in text"
* gates don't truly reject

Essential problem:
> **The cognition system designed for Jingu was never loaded into runtime**

### Chain 2: Verify signal is broken

**438 tests -> module fallback -> timeout -> no signal**

Impact:
* Agent gets zero feedback
* Retry is blind retry
* Judge is completely ineffective

```text
agent = writes patch -> never knows if correct or not
```

Essential problem:
> **Closed-loop is broken -> system becomes open-loop hallucination**

### Chain 3: Gates exist but surrender

**force-pass + hardcoded limits + no logging**

Impact:
* Gate rejects 2 times then lets through
* Retry loop exceeds limit then bypasses
* All critical behavior has no structured log

```text
You think you have governance, but gates automatically surrender
```

Essential problem:
> **System prioritizes "keep running" over "ensure correctness"**

---

## Cascade Relationship

```text
(1) bundle failure
   |
(2) cognition doesn't exist at runtime
   |
(3) gates can only use regex
   |
(4) gate rejects are unstable -> force-pass
   |
(5) verify timeout -> no signal
   |
(6) agent is completely blind
```

This is a **serial collapse chain**.

But simultaneously:
* verify timeout is an independent problem
* force-pass is an independent problem

So: **one primary cause + two parallel fatal problems**

---

## System Maturity Assessment

### Current state:

```text
Level 0.5 — pseudo-governed agent
```

Characteristics:
* Governance code exists
* But runtime didn't load it
* Behavior ~= ungoverned

### Target:

```text
Level 2 — enforced cognition system
```

Characteristics:
* Phase enforced
* Principal enforced
* Verify provides signal
* Retry is guided

---

## The Most Important Judgment (avoid wrong direction)

The easiest mistake to make now:

> "Let's optimize multi-agent / cognition / policy design"

But reality:

> **Governance hasn't even loaded successfully**

Correct order:

```text
1. governance must exist
2. governance must be observable
3. governance must affect behavior
4. THEN talk about cognition sophistication
```

---

## Minimum Correct Path (3 steps)

### Step 1 (do today)

* bundle failure -> hard crash (not silent fallback)
* emit structured event
* See: `PR1-bundle-failure-explicit.md`

### Step 2 (1-2 days)

* fix controlled_verify (at least make it produce signal)
* remove 20-class limit
* dynamic timeout
* See: `PR4-verify-scheduler-v2.md`

### Step 3 (3-5 days)

* all limits -> structured logging
* force-pass -> must be visible
* See: `PR3-limit-event-unification.md`

After this:

```text
Before: you don't know why the system fails
After:  every failure has a clear causal chain
```

---

## Transformation Goal

From:
```text
code contains behavior (scattered hardcoded values)
```

To:
```text
bundle declares behavior
runtime enforces behavior
events expose behavior
```

---

## PR Execution Plan (6 PRs)

| PR | Name | What | Week |
|----|------|------|------|
| PR1 | Bundle failure explicit | bundle failure visible + abort in benchmark | Week 1 |
| PR2 | Bundle schema v2 | new schema + compiler support | Week 2 |
| PR3 | Limit event unification | all limits emit structured events | Week 1 |
| PR4 | Verify scheduler v2 | batched targeted verify, no more blind timeout | Week 3 |
| PR5 | Gate config from bundle | gates read thresholds/limits from bundle | Week 4 |
| PR6 | Phase/type/principal enforcement | real contract enforcement, not surface | Week 5 |

### Week 1: PR1 + PR3
Make bundle failure and limit triggers visible.

### Week 2: PR2
Bundle schema v2 and compiler.

### Week 3: PR4
Fix verify scheduler, agent no longer blind.

### Week 4: PR5
Gates truly consume bundle config.

### Week 5: PR6
Phase/type/principal enforcement in runtime.

---

## Plan Files

* `MASTER-PLAN.md` — this file (system diagnosis + priority)
* `bundle-schema-v2.md` — full bundle.json v2 specification
* `verify-scheduler-v2.md` — verify scheduler v2 design + pseudocode
* `PR1-bundle-failure-explicit.md` — PR 1 implementation plan
* `PR2-bundle-schema-v2.md` — PR 2 implementation plan
* `PR3-limit-event-unification.md` — PR 3 implementation plan
* `PR4-verify-scheduler-v2.md` — PR 4 implementation plan
* `PR5-gate-config-hydration.md` — PR 5 implementation plan
* `PR6-phase-principal-enforcement.md` — PR 6 implementation plan

---

## One-line Summary

> **The primary task is not "design better Jingu" but "ensure Jingu actually exists at runtime".**
