# Eval Report — ECS Batch 03

**Date:** 2026-04-02  
**Dataset:** SWE-bench Lite (django subset, 5 instances)  
**Instances:** django__django-10914, 10924, 11001, 11019, 11039  
**Pipeline:** mini-swe-agent + Jingu (B1 Gate + B2 Reviewer + B3 Retry Controller)  
**Infrastructure:** ECS EC2 launch type, 4 parallel workers, Docker-in-Docker (vfs)

---

## Result Summary

| Instance | Attempts | Best Attempt | Gate | Score | Cost | Wall Time |
|----------|----------|-------------|------|-------|------|-----------|
| django-10914 | 2 | 1 | ADMITTED | 950 | $1.22 | 781s |
| django-10924 | 2 | 1 | ADMITTED | 950 | $1.60 | 823s |
| django-11001 | 2 | 1 | ADMITTED | 950 | $0.90 | 681s |
| django-11019 | 2 | 1 | ADMITTED | 950 | $2.87 | 1171s |
| django-11039 | 2 | 1 | ADMITTED | 950 | $1.27 | 547s |
| **Total** | **10** | — | **5/5 ADMITTED** | — | **$7.85** | **1321s (wall)** |

Parallelism gain: 4002s sequential → 1321s wall time (×3.0)

---

## What This Result Proves

### 1. Infrastructure validation (primary goal of this batch)

- ECS EC2 launch type with `privileged: true` supports Docker-in-Docker ✓
- `vfs` storage driver resolves overlay-on-overlay problem in nested Docker ✓
- `pull_timeout=600s` handles cold image pulls for `swebench/sweb.eval.*` ✓
- Gate runner (`gate_runner.js`) works correctly as ESM in container ✓
- S3 results upload via boto3 works end-to-end (14 files uploaded) ✓
- Bedrock inference-profile IAM policy grants correct model access ✓

### 2. Jingu component behavior

**B1 Gate** ran on every attempt. All attempt 1 patches were ADMITTED at score=950.  
Gate correctly did not reject any structurally valid, correctly-targeted patch.  
Current gate function: structural admission check (non-empty diff, target file present, patch applies). Not a semantic correctness gate.

**B3 Retry Controller** was invoked for all 5 instances after attempt 1.  
4 of 5 times, attempt 1 was already correct — controller correctly recognized this and issued  
"do not change" guidance ("Submit immediately without changes").  
1 of 5 times (django-11001), controller issued substantive corrective guidance (see below).

**B2 Reviewer** was not in the decisive path this batch — all attempt 1 patches were ADMITTED,  
so best_attempt=1 for all instances regardless of reviewer verdict.

---

## The One Substantive Case: django__django-11001

This is the only instance where B3 Retry Controller demonstrably changed the outcome quality.

**Attempt 1 patch (26 lines):**
```python
# Workaround: normalize SQL before regex match
sql_normalized = ' '.join(sql.split())
without_ordering = self.ordering_parts.search(sql_normalized).group(1)
```
Applied at two locations. Technically works but treats the symptom, not the root cause.

**Retry Controller diagnosis:**
- Failure type classified: `no_effect_patch` (patch written, tests ran, failure unchanged)
- Root cause identified: "regex `(.*)\s(ASC|DESC)(.*)` does not handle multiline SQL"
- Hint: "Find the exact code path the failing test exercises. Fix ordering_parts regex directly."

**Attempt 2 patch (13 lines, 1 location):**
```python
self.ordering_parts = re.compile(r'(.*)\s(ASC|DESC)(.*)', re.DOTALL)
```
One-line root-cause fix. Patch halved in size, approach changed from workaround to correct fix.

**Signal:** Retry controller's value is not "try again" but "change direction." The second attempt
used a different, smaller, more correct fix — not the same approach repeated.

---

## What This Result Does NOT Prove

**This batch cannot demonstrate that Jingu improves benchmark resolved rate**, for a structural reason:

All 5 instances were solved on attempt 1. Jingu's main working area — rescuing failed first
attempts through failure classification and directed retry — was not exercised. The sample is
too easy for the model (claude-sonnet on django medium-difficulty bugs) to show recovery value.

The correct interpretation is:

> Jingu did not harm any instance that was already solvable on attempt 1.
> Jingu showed directional correction capability on at least one case.
> This batch is insufficient to prove Jingu improves pass rate, because the baseline already passes.

---

## Component Characterization (current state)

| Component | Current Role | Limitation |
|-----------|-------------|------------|
| **B1 Gate** | Structural admission check — catches empty/malformed patches | Not a semantic correctness gate; cannot detect "wrong fix" |
| **B2 Reviewer** | Semantic quality verdict (APPROVED / DOWNGRADED / REJECTED) | Only enters decisive path when gate produces REJECTED; not triggered this batch |
| **B3 Retry Controller** | Failure classification (FT1–FT5) + directed retry hint | Requires a real failure signal to be useful; wasted on already-passing attempts |

---

## Next Experiment Design

To demonstrate Jingu's value, the next batch must satisfy:

**Criterion: baseline first-attempt instability.**  
Select instances where a raw baseline (mini-swe-agent, no Jingu, single attempt) has
~40–60% pass rate. Jingu's recovery value is only visible in this range.

**Three target categories:**

### A. Wrong-direction failures (FT1)
Instances where the model finds the right file but applies an incorrect algorithm fix.
- Jingu value to measure: does retry controller redirect from workaround to root cause?
- Key metrics: `patch_lines_before / patch_lines_after`, `workaround_rate`, `root_cause_rate`

### B. Large-patch / over-engineering failures (FT3)
Instances where the model produces a structurally valid but semantically incorrect patch.
- Jingu value to measure: does gate + reviewer narrow the patch?
- Key metrics: touched files, hunk count, patch size, final accepted patch minimality

### C. First-attempt fail, second-attempt recoverable
Instances where baseline attempt 1 fails (gate REJECTED or tests fail), but the problem
is not fundamentally beyond the model.
- Jingu value to measure: second-attempt recovery rate vs baseline second-attempt recovery rate
- Key metric: `recovery_rate = (instances solved on attempt 2) / (instances failed on attempt 1)`

**Proposed comparison:**

| Condition | Description |
|-----------|-------------|
| Baseline | mini-swe-agent, 1 attempt, no gate, no retry |
| Jingu-2shot | mini-swe-agent, 2 attempts, B1 gate + B3 retry controller |
| Jingu-full | mini-swe-agent, 2 attempts, B1 + B2 + B3 |

Run each on 20–50 instances from categories A/B/C above.  
Primary metric: **resolved rate** (binary, ground truth from SWE-bench harness).  
Secondary metrics: recovery rate, patch minimality, root-cause fix ratio.

---

## One-Sentence Summary

> Jingu showed no regression on easy instances and directional correction on one wrong-direction
> case (django-11001: workaround → root-cause fix), but this batch is insufficient to prove
> improved benchmark resolved rate — the sample is too easy and Jingu's recovery mechanisms
> were not exercised.
