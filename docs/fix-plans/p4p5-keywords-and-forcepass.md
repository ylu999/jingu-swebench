# P4+P5 — Keywords/Patterns Audit + Force-Pass Audit

**Priority:** MEDIUM-LOW (after P0-P3)
**Audit items:** K1-K10 (keywords), F1-F4 (force-pass)
**Impact:** Surface pattern matching on LLM output violates STRUCTURE_OVER_SURFACE; force-pass gates silently bypass governance.

---

## Part A: Keyword/Pattern Audit (K1-K10)

### Classification

| # | Pattern | Type | Action |
|---|---------|------|--------|
| K1 | `_SIGNAL_TOOL_NAMES` | System signal detection | KEEP — detects tool usage, not LLM text. But must be extensible. |
| K2 | `_ENV_MUTATION_PATTERNS` | System signal detection | KEEP — detects forbidden commands. Structural (command matching). |
| K3 | `_HYPOTHESIS_PATTERNS` | LLM output surface matching | MIGRATE — P1 violation. Move to RPP structured check when available. |
| K4 | `_INVARIANT_SIGNALS` | LLM output surface matching | MIGRATE — P1 violation. |
| K5 | `_INVARIANT_PRESERVATION_SIGNALS` etc | LLM output surface matching | MIGRATE — P1 violation. 3 pattern lists in design_gate. |
| K6 | `_CAUSAL_KEYWORDS` etc | LLM output surface matching | MIGRATE — P1 violation. Principal inference from keywords. |
| K7 | `_SEMANTIC_WEAKENING_PATTERNS` | LLM output surface matching | MIGRATE — P1 violation. |
| K8 | `_LOCAL_PATH_PATTERNS` etc | System signal detection | KEEP — detects structural patterns in commands. |
| K9 | `_PEEK_SIGNALS` | Ops tooling | KEEP — ops-level, not gate. |
| K10 | `_SIGNAL_CONTRADICTIONS` | System signal detection | KEEP — rule-based contradiction detection. |

### Summary

- **KEEP (5):** K1, K2, K8, K9, K10 — system/structural pattern matching (acceptable)
- **MIGRATE (5):** K3, K4, K5, K6, K7 — LLM output surface matching (P1 violation)

### Migration Plan for K3-K7

These cannot be fixed today — they require RPP structured extraction to be working (depends on P0 bundle activation). Plan:

1. **Document each pattern** with its intended semantic check
2. **Add `[surface_check]` prefix** to all log lines from these patterns (visibility)
3. **When RPP structured extraction works** (post-P0):
   - K3 (hypothesis): check `rpp.steps[reasoning].content` for distinct hypothesis structures
   - K4 (invariant): check `rpp.steps[reasoning].references` for invariant-type refs
   - K5 (design signals): check `rpp.steps[decision].content` for comparison/preservation structures
   - K6 (principal keywords): already partially structural via `principal_inference.py` scoring — improve signal sources
   - K7 (weakening): check diff structure for semantic weakening patterns

### Immediate Fix for K1-K2

Make extensible via bundle config (P3):
```python
# Instead of hardcoded list:
_SIGNAL_TOOL_NAMES = bundle_config.get("signal.tool_names", ["edit", "write", "create", ...])
```

---

## Part B: Force-Pass Audit (F1-F4)

### Current Behavior

All 4 force-pass mechanisms share the same anti-pattern:
1. Gate rejects N times
2. Counter reaches limit
3. `print()` a message to stdout
4. Silently set status = ADMITTED / passed = True
5. No record in `decisions.jsonl`

### Required Fix (all 4)

Each force-pass MUST:

#### 1. Log to decisions.jsonl

```python
# F1 example (analysis gate):
if not _analysis_verdict.passed and _ag_reject_count >= _AG_MAX_REJECTS:
    _force_pass_record = {
        "type": "gate_force_pass",
        "gate": "analysis_gate",
        "reject_count": _ag_reject_count,
        "max_rejects": _AG_MAX_REJECTS,
        "failed_rules": _analysis_verdict.failed_rules,
        "scores": _analysis_verdict.scores,
        "action": "force_pass_advance",
    }
    log_decision(_force_pass_record)  # to decisions.jsonl
    log_limit("analysis_gate_force_pass", _AG_MAX_REJECTS, _ag_reject_count,
              "force_pass", "step_sections.py", 613)  # P2 limit logger
```

#### 2. Emit peek-visible signal

```python
print(f"    [FORCE_PASS] analysis_gate: {_ag_reject_count}/{_AG_MAX_REJECTS} rejects -> bypassing", flush=True)
```

#### 3. Be configurable (via P3 bundle)

```python
_AG_MAX_REJECTS = _cfg("gates.analysis_max_rejects", 2)
```

### Per-Item Details

| # | Gate | Current Limit | Structured Violation Exempt? | Notes |
|---|------|--------------|------------------------------|-------|
| F1 | Analysis gate | 2 rejects | No | Simple counter |
| F2 | Design gate | 2 rejects | No | Simple counter |
| F3 | RETRYABLE loop | 3 loops | Yes — `missing_root_cause`, `missing_plan`, `plan_not_grounded` exempt | Good design — structured violations never bypassed |
| F4 | Fake loop | 3 loops | No — but selective (only bypasses specific principals) | Reasonable — per-principal bypass, not blanket |

**F3 and F4 have better design than F1/F2.** F3 explicitly exempts structural violations from bypass. F4 does per-principal selective bypass. F1/F2 are blanket force-pass with no nuance.

### Recommended Changes

- **F1/F2:** Increase default from 2 to 3 (matches F3/F4). Add structural violation exemption (same pattern as F3).
- **F3:** Already well-designed. Just add logging + configurability.
- **F4:** Already well-designed. Just add logging + configurability.

## Verification

1. Run with known gate rejection scenario
2. Check `decisions.jsonl` has `gate_force_pass` records for every bypass
3. Check peek output shows `[FORCE_PASS]` signals
4. Verify F1/F2 structural exemption works (structural violations never force-passed)

## Dependencies

- P2 (limit logging) should be done first — provides the `log_limit()` function
- P3 (bundle externalization) makes limits configurable
- P0 (bundle) needed for RPP-based migration of K3-K7
