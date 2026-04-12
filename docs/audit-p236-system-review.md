# p236 System Audit — smoke-dockerfile-verify (django__django-10097)

Date: 2026-04-12
Eval result: NOT RESOLVED (0/1)
Git commit under test: d20c673

---

## Part 1: Subsystem Audit Table

| # | Subsystem | Status | Detail |
|---|-----------|--------|--------|
| S1 | Phase Tracking | PARTIAL | OBSERVE(28 steps) -> ANALYZE(5 steps), never reached EXECUTE/VERIFY. step_events correctly recorded each step's phase. |
| S2 | Cognition Gate / Bundle Loading | NOT ACTIVE | **ROOT CAUSE CONFIRMED**: `compile_bundle()` threw exception at `jingu_agent.py:665`. Exception silently caught, fell through to hardcoded fallback (line 681). `prompt_snapshot.reasoning_protocol` contains fallback text ("STEP 1 — before writing any code"), NOT bundle-generated phase prompts. Bundle file exists in image (`/app/bundle.json`, 66KB), but compilation failed. Error message only in stdout (`[jingu_onboard] prompt load error (fallback): ...`), not in decisions.jsonl or step_events — **zero structured visibility of this critical failure**. |
| S3 | Policy / Principal | SURFACE ONLY | Agent wrote `PHASE: observe` / `PRINCIPALS: evidence_completeness` in text, but system-level enforcement absent. decisions.jsonl has only 1 record (advance OBSERVE->ANALYZE). |
| S4 | Prompt Injection | NOT ACTIVE | `phase_injection=NONE`, `principal_section=NONE`. Cognition bundle may not be loaded. |
| S5 | Response Structure | SURFACE ONLY | Agent declared phase/principals in text, but `extraction_metrics` all 0 (structured=0, regex_fallback=0, no_schema=0). Declaration extractor extracted nothing. |
| S6 | Controlled Verify | ALL FAILED | 3x `controlled_error` (timeout). Root cause: FAIL_TO_PASS=438 tests, 69 classes > 20 limit -> fallback to module scope -> timeout at 60s. Agent got zero test feedback. |
| S7 | Step Events (p228) | OK | 33 steps all have step events with phase/gate/cp_state/tool_calls/patch_non_empty. |
| S8 | Decision Logger (p229) | OK | decisions.jsonl has records. Only 1 entry because gate enforcement was not active. |
| S9 | Checkpoint (p231) | OK | step_29.json.gz exists, trigger=phase_advance, contains messages_so_far/cp_state/phase_records. |
| S10 | Prompt Snapshot (p230) | OK | prompt_snapshot.json recorded attempt/instance_id/mode/sections/total_chars. |
| S11 | Replay Engine (p232) | OK | `replay_cli.py list-checkpoints` correctly lists checkpoint; `replay --dry-run` parses checkpoint. |
| S12 | Traj Diff (p233) | OK | CLI available, requires two traj files for comparison. |
| S13 | Prompt Regression (p234-235) | OK | `ab` / `suite` / `init-golden` subcommands all present, dry-run works. |
| S14 | Run Report | OK | execution_identity (git_commit, build_timestamp), model_usage, attempt_stats all populated. |
| S15 | Heartbeat | OK | heartbeat.json has ts/done/total/last_instance/accepted_so_far. |
| S16 | S3 Upload | OK | predictions, traj, step_events, decisions, checkpoints, prompt_snapshot, run_report all uploaded. |
| S17 | Eval Pipeline | OK | SWE-bench harness downloaded predictions, built test container, ran eval, output resolved/unresolved, uploaded to S3. |
| S18 | Patch Quality | DIRECTION OK | Regex `\S+` -> `[^:@/\s]+` limits username chars. But official FAIL_TO_PASS is entire auth_tests (438 tests), patch may only fix URLValidator subset. |

### Root Cause Chain (NOT RESOLVED)

```
1. FAIL_TO_PASS = 438 tests (69 classes) -- far exceeds targeted scope 20-class limit
2. controlled_verify falls back to module scope -- runs entire auth module
3. module scope tests exceed 60s timeout -> TimeoutExpired -> controlled_error
4. 3x verify all timeout -> agent gets ZERO test feedback
5. no test feedback -> agent writes patch blind, cannot confirm correctness
6. simultaneously: cognition gate not active -> no phase injection / principal enforcement
7. agent writes own small test (passes) -> submits, but official eval uses full auth_tests -> fails
```

Three independent problems:
- **Bundle compilation failure** (S2) -- compile_bundle() exception silently caught, entire governance system falls back to hardcoded prompts. Zero visibility.
- **controlled_verify timeout** (C1+C2 below) -- agent has no test signal
- **cognition gate not active** (S2+S4 below) -- consequence of bundle failure

### Bundle Loading Trace

```
jingu_agent.py:627  compile_bundle() called
jingu_agent.py:665  except Exception -> "[jingu_onboard] prompt load error (fallback): <error>"
jingu_agent.py:669  _phase_prompt_parts is empty (bundle failed)
jingu_agent.py:681  FALLBACK: hardcoded reasoning protocol injected
                    -> prompt_snapshot shows "STEP 1 — before writing any code" (fallback text)
                    -> phase_injection = NONE (bundle prompts never loaded)
                    -> principal_section = NONE (bundle contracts never loaded)
                    -> extraction_metrics = all 0 (structured schema not available)
```

**This is the #1 issue**: bundle compilation fails silently, and the ENTIRE governance
system degrades to surface-level text matching with hardcoded prompts. No structured
extraction, no phase enforcement, no principal gates — just keyword matching on
agent output that happens to include "PHASE:" markers from the fallback prompt.

---

## Part 2: Hardcoded Limits / Thresholds / Keywords Audit

### Category 1: CRITICAL — Directly changes agent behavior, no logging/visibility

| # | File:Line | Hardcoded Value | What It Does | Impact | Logged? |
|---|-----------|----------------|--------------|--------|---------|
| C1 | `controlled_verify.py:89` | `<= 20` classes | Targeted scope upper limit. Exceeding falls back to module scope. | django-10097: 69 classes triggered module fallback -> timeout -> no test feedback | NO (only prints scope name) |
| C2 | `controlled_verify.py:343` | `timeout_s=60` | controlled_verify total timeout | Module scope can't finish in 60s -> all verify timeout -> agent blind | NO |
| C3 | `controlled_verify.py:83` | `>= 3` sentinel | P2P sentinel classes max count | May miss key regression signals | NO |
| C4 | `controlled_verify.py:414,438,443,450,457,488,497,503,534` | `timeout=15/30` | docker exec sub-operation timeouts (stash/reset/apply/cp) | Sub-op timeout exceptions silently caught | NO |
| C5 | `step_sections.py:239` | `timeout_s=45` | In-loop controlled_verify timeout | Shorter than final verify (60s), inconsistent | NO |
| C6 | `step_sections.py:1013` | `_FAKE_LOOP_LIMIT = 3` | Fake principal consecutive RETRYABLE limit | Exceeds -> force pass (bypasses gate) | NO (print only, not in decisions) |
| C7 | `step_sections.py:328` | `_EXECUTE_REDIRECT_LIMIT = 3` | EXECUTE no-progress redirect limit | Exceeds -> reject | NO (print only) |
| C8 | `step_sections.py:597` | `_AG_MAX_REJECTS = 2` | Analysis gate max reject count | Exceeds -> force pass (bypasses gate) | NO (print only) |
| C9 | `step_sections.py:676` | `_DG_MAX_REJECTS = 2` | Design gate max reject count | Exceeds -> force pass (bypasses gate) | NO (print only) |
| C10 | `step_sections.py:872` | `_RETRYABLE_LOOP_LIMIT = 3` | RETRYABLE loop limit | Exceeds -> force pass | NO (print only) |
| C11 | `retry_controller.py:37` | `NO_SIGNAL_THRESHOLD = 15` | No-signal steps -> STOP_NO_SIGNAL | Directly terminates agent | NO (internal to retry_controller) |
| C12 | `jingu_adapter.py:27` | `_MAX_PYTEST_FEEDBACK_BYTES = 2048` | Test feedback truncation limit | Critical test output may be truncated | NO |
| C13 | `principal_inference.py:106` | `_SMALL_PATCH_MAX_LINES = 30` | minimal_change inference threshold | >30 lines = "too large", not minimal | NO |
| C14 | `patch_reviewer.py:35` | `REVIEWER_MAX_TOKENS = 1024` | Reviewer LLM max output tokens | Complex reviews truncated | NO |
| C15 | `run_with_jingu_gate.py:681` | `pull_timeout: 600` | Docker image pull timeout (10 min) | First pull may exceed | NO |
| C16 | `jingu_agent.py:919` | `timeout_s=60` | Final verify timeout (hardcoded again) | Same as C2, duplicated | NO |

### Category 2: HIGH — Keyword/Pattern Matching (STRUCTURE_OVER_SURFACE violations)

| # | File:Line | Pattern Constant | What It Does | Problem |
|---|-----------|-----------------|--------------|---------|
| K1 | `signal_extraction.py:14-33` | `_SIGNAL_TOOL_NAMES`, `_SIGNAL_BASH_PATTERNS` | Determines if agent step has "signal" (write/submit/edit) | Hardcoded tool name list; new tools not in list = not counted as signal |
| K2 | `signal_extraction.py:35` | `_ENV_MUTATION_PATTERNS` | Detects agent environment mutations (pip install etc) | Hardcoded string matching |
| K3 | `analysis_gate.py:127` | `_HYPOTHESIS_PATTERNS` | Regex to detect hypothesis presence | Surface pattern matching on LLM output |
| K4 | `analysis_gate.py:211` | `_INVARIANT_SIGNALS` | Regex to detect invariant analysis | Surface pattern matching on LLM output |
| K5 | `design_gate.py:38,81,132` | `_INVARIANT_PRESERVATION_SIGNALS`, `_COMPARISON_SIGNALS`, `_COMPLETENESS_SIGNALS` | Regex to judge design quality | Surface pattern matching on LLM output |
| K6 | `principal_inference.py:92-101` | `_CAUSAL_KEYWORDS`, `_ALTERNATIVE_KEYWORDS`, `_PRESERVE_KEYWORDS` | Regex to infer principal presence from text | Surface keywords -> principal inference (P1 violation) |
| K7 | `in_loop_judge.py:61` | `_SEMANTIC_WEAKENING_PATTERNS` | Regex to detect semantic weakening | Surface pattern matching |
| K8 | `jingu_adapter.py:17-22` | `_LOCAL_PATH_PATTERNS`, `_ENV_CHECK_KEYWORDS`, `_FEEDBACK_KEYWORDS` | Detect local paths / env check behavior | Hardcoded path and keyword lists |
| K9 | `ops.py:2325` | `_PEEK_SIGNALS` | Peek signal log filter | Hardcoded signal prefix list (ops-level, acceptable) |
| K10 | `cognition_check.py:34` | `_SIGNAL_CONTRADICTIONS` | Detect contradictory signals | Hardcoded rule list |

### Category 3: Score Thresholds (principal inference — all hardcoded)

| # | File:Line | Threshold | Principal | Effect |
|---|-----------|-----------|-----------|--------|
| T1 | `principal_inference.py:192` | `>= 0.5` | causal_grounding | Below = absent |
| T2 | `principal_inference.py:225` | `>= 0.5` | evidence_linkage | Below = absent |
| T3 | `principal_inference.py:251` | `>= 0.7` | minimal_change | Below = absent |
| T4 | `principal_inference.py:278` | `>= 0.7` | ontology_alignment | Below = absent |
| T5 | `principal_inference.py:302` | `>= 0.7` | phase_boundary_discipline | Below = absent |
| T6 | `principal_inference.py:341` | `>= 0.7` | action_grounding | Below = absent |
| T7 | `principal_inference.py:402` | `>= 0.7` | constraint_satisfaction | Below = absent |
| T8 | `principal_inference.py:459` | `>= 0.7` | result_verification | Below = absent |
| T9 | `principal_inference.py:510` | `>= 0.7` | option_comparison | Below = absent |
| T10 | `principal_inference.py:545` | `>= 0.7` | uncertainty_honesty (stub) | Below = absent |
| T11 | `principal_inference.py:588` | `>= 0.7` | evidence_completeness | Below = absent |
| T12 | `principal_inference.py:634` | `>= 0.7` | differential_diagnosis | Below = absent |
| T13 | `design_gate.py:226` | `0.5` | design gate threshold | Below = reject |
| T14 | `analysis_gate.py:275` | from contract | analysis gate threshold | From contract (CORRECT - only one doing this right) |
| T15 | `analyze_principal_metrics.py:107` | `>= 0.7` | "fired" judgment | Inconsistent with T1/T2 which use 0.5 |

### Category 4: Force-Pass / Bypass Mechanisms (gates that give up)

| # | File:Line | Mechanism | Trigger | Effect |
|---|-----------|-----------|---------|--------|
| F1 | `step_sections.py:613-614` | Analysis gate FORCE_PASS | `reject_count >= _AG_MAX_REJECTS (2)` | Gate gives up after 2 rejects, allows advance |
| F2 | `step_sections.py:689-690` | Design gate FORCE_PASS | `reject_count >= _DG_MAX_REJECTS (2)` | Gate gives up after 2 rejects, allows advance |
| F3 | `step_sections.py:883` | RETRYABLE loop FORCE_PASS | `loop_count >= _RETRYABLE_LOOP_LIMIT (3)` AND no structured violation | Bypasses principal gate |
| F4 | `step_sections.py:1014` | Fake loop FORCE_PASS | `fi_loop_count >= _FAKE_LOOP_LIMIT (3)` | Bypasses fake check entirely |

---

## Part 3: Summary Statistics

- **16** CRITICAL hardcoded limits (C1-C16) — change agent behavior, no logging
- **10** keyword/pattern matching constants (K1-K10) — surface-based checks
- **15** score thresholds (T1-T15) — principal inference, all hardcoded except T14
- **4** force-pass/bypass mechanisms (F1-F4) — gates that give up silently
- **6** subsystem issues (S1-S6 from Part 1) — not active or surface only

**Total: 51 items requiring action.**

---

## Part 4: Required Fixes (Priority Order)

### P0 — Cognition Gate Activation (S1-S4)
Without this, the entire governance system is offline. No phase injection, no principal enforcement, no structured extraction.

### P1 — Controlled Verify (C1, C2, C5, C16)
Agent has no test feedback. This is the direct cause of NOT RESOLVED.
- Remove 20-class arbitrary limit or raise significantly
- Scale timeout with test count (not fixed 60s)
- Unify C2/C5/C16 into single configurable value

### P2 — Limit Trigger Logging (all C1-C16, F1-F4)
Every time a configured limit changes system behavior, emit:
```json
{"type": "limit_triggered", "limit_name": "...", "configured_value": N, "actual_value": M, "action_taken": "...", "file": "...", "line": N}
```
Must appear in both stdout (for peek) and decisions.jsonl (for replay/analysis).

### P3 — Bundle Externalization
Move all hardcoded values to onboarding bundle config:
- Timeouts (C2, C4, C5, C15, C16)
- Loop limits (C6, C7, C8, C9, C10, C11)
- Scope limits (C1, C3)
- Size limits (C12, C13, C14)
- Score thresholds (T1-T13, T15)

### P4 — Keyword/Pattern Audit (K1-K10)
Document each pattern list. For K3-K7 (LLM output surface matching): plan migration to structured checks when RPP/structured extraction is available.

### P5 — Force-Pass Audit (F1-F4)
Each force-pass must:
1. Log to decisions.jsonl with full context
2. Be configurable (max_rejects from bundle, not hardcoded)
3. Never silently bypass — always emit `[gate-force-pass]` signal

---

## Part 5: Bundle Loading Deep Dive

### What bundle.json provides (when working)

- Per-phase prompts (OBSERVE/ANALYZE/DECISION/EXECUTE/VERIFY)
- Per-phase principal requirements (required + forbidden)
- Type contracts (subtype -> required principals mapping)
- Structured output schemas (for declaration extraction)
- Gate configurations (thresholds, validation rules)
- Cognition loader data (phase definitions, principal registry)

### What happens when bundle fails (current state)

| Component | With Bundle | Without Bundle (fallback) |
|-----------|------------|--------------------------|
| Phase prompts | Dynamic per-phase from bundle | Hardcoded 4-step "REASONING PROTOCOL" |
| Principal requirements | From contracts (per-subtype) | Hardcoded `_analysis_req`, `_decision_req`, `_execute_req` |
| Structured extraction | Bundle schema -> structured JSON parse | `extraction_metrics.structured = 0`, falls to regex |
| Phase injection | Per-phase guidance from cognition | `phase_injection = NONE` |
| Principal section | Dynamic from contracts | `principal_section = NONE` |
| Gate enforcement | Full contract validation | Surface keyword matching only |
| Type contracts | Compiled from bundle | Hardcoded string in `_type_contracts_block` |

### Evidence from this run

```
prompt_snapshot.sections:
  - forbidden_actions: 486 chars (hardcoded, not from bundle)
  - reasoning_protocol: 1498 chars (FALLBACK — "STEP 1 — before writing any code")

extraction_metrics: {structured: 0, regex_fallback: 0, no_schema: 0, total: 0}
  -> structured = 0 means bundle schema was NOT available for parsing
  -> regex_fallback = 0 means even regex fallback extracted nothing
  -> total = 0 means declaration_extractor produced ZERO records

phase_injection: NONE
principal_section: NONE
```

### Why compile_bundle() likely failed

Candidates (need to reproduce):
1. `bundle.json` exists at `/app/bundle.json` but `JINGU_BUNDLE_PATH` not set -> looks in wrong place
2. `jingu_loader` package not importable (missing from Python path in container)
3. Bundle JSON schema mismatch (bundle compiled by newer jingu-cognition, loader expects older format)
4. Missing dependency in bundle_compiler.py (imports that don't exist in container)

### Required fix

1. `compile_bundle()` failure MUST be a hard error, not a silent fallback
2. Emit structured event: `{"type": "bundle_load_failure", "error": "...", "fallback_active": true}`
3. Log to decisions.jsonl so it appears in replay analysis
4. Consider: should the run ABORT if bundle fails? (governance = offline)
