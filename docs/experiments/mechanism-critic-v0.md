# Experiment: Mechanism Critic v0 — Offline Validation

## Config
- **Model**: claude-sonnet-4-5-20250929 (critic)
- **Agent model**: claude-sonnet-4-6 (patches being critiqued)
- **Type**: Offline — critic sees problem+tests+agent_patch, NOT gold patch
- **Target instances**: 10097, 10999, 11265 (all wrong_patch from baseline)

## Hypothesis
A second LLM call (critic) can identify mechanism gaps in an agent's failed patch
without seeing the gold patch. If successful, this feedback could be injected into
retry attempts to guide the agent toward the correct fix.

## Critic Design
- **Input**: problem_statement, failing_test_names, agent_patch
- **NOT given**: gold patch (critic must reason from first principles)
- **Output**: missing_mechanism, incorrect_assumption, suggested_fix, confidence

## Results

### Instance 10097 (URLValidator regex)
- **Agent bug**: Password char class `[^\s@/]` missing `:` — gold is `[^\s:@/]`
- **Critic detected issue**: YES
- **Critic matches gold mechanism**: NO
  - Critic went too far — proposed RFC-compliant percent-encoding validation
  - Missed the specific single-character omission (`:` in password class)
  - Over-engineered diagnosis: correct direction but wrong granularity
- **Critic novel insight**: NO
- **Confidence**: high (misplaced)

### Instance 10999 (parse_duration negative regex)
- **Agent bug**: Only fixed lookahead `(?=-?\d+:-?\d+)`, kept `-?` in individual groups
- **Gold mechanism**: Extract sign as separate `(?P<sign>-?)` group, remove `-?` from hour/min/sec
- **Critic detected issue**: YES
- **Critic matches gold mechanism**: PARTIAL
  - Correctly identified: individual groups having `-?` allows inconsistent per-component signs
  - Did NOT name the specific solution (separate sign group)
  - Suggested "additional logic" or "normalize signs after matching" — vague but directionally correct
- **Critic novel insight**: YES — noted PostgreSQL format handling consideration
- **Confidence**: high

### Instance 11265 (FilteredRelation exclude)
- **Agent bug**: Only copied `_filtered_relations`, missing `trim_start` filtered_relation check
- **Gold mechanism**: (1) copy `_filtered_relations` + (2) prevent trimming INNER JOINs from filtered relations in `trim_start`
- **Critic detected issue**: YES
- **Critic matches gold mechanism**: PARTIAL
  - Correctly identified: patch is incomplete, more than `_filtered_relations` copy needed
  - Wrong additional fix: suggested alias_map/join copying instead of `trim_start` check
  - Directionally correct (knew something else was missing), specifically wrong
- **Critic novel insight**: NO
- **Confidence**: high

## Summary

| Instance | critic_detected_issue | critic_matches_gold | critic_novel_insight | confidence |
|----------|----------------------|--------------------|--------------------|------------|
| 10097 | YES | NO | NO | high |
| 10999 | YES | PARTIAL | YES | high |
| 11265 | YES | PARTIAL | NO | high |

**Brain success gate**: >= 2/3 where detected=YES AND matches!=NO → **2/3 PASS** (10999, 11265)

## Key Findings

1. **Detection rate: 3/3** — critic always detects something is wrong (trivial given we tell it the patch failed)
2. **Mechanism accuracy: 1/3 NO, 2/3 PARTIAL, 0/3 YES** — critic identifies the right direction but not the exact fix
3. **Over-engineering tendency**: critic proposes more complex solutions than needed (10097 RFC compliance, 11265 alias_map)
4. **Confidence miscalibration**: all "high" confidence including the NO match (10097)

## Implications for In-Loop Use

### Positive signals
- PARTIAL matches provide useful direction for retry (10999: "sign consistency", 11265: "more changes needed")
- Even wrong-specific diagnosis might nudge agent away from repeating same mistake

### Concerns
- **Over-engineering**: critic's suggested fixes are more complex than gold — could lead agent further astray
- **Vague suggestions**: "additional logic" / "normalize after matching" — not actionable enough for an agent
- **False confidence**: high confidence on wrong diagnosis (10097) could mislead strongly

### Recommendation
Critic signal is **directionally useful but not precise enough** for hard gating.
Potential use: soft signal injected as "reviewer feedback" in retry context, NOT as a directive.
Need to test whether PARTIAL-quality feedback actually changes agent behavior on retry.

## Decision
PENDING — report to brain for next directive.
