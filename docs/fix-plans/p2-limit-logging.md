# P2 — Limit Trigger Logging Fix Plan

**Priority:** HIGH (visibility for all limits)
**Audit items:** C1-C16, F1-F4
**Impact:** Every configured limit that changes system behavior must be observable.

---

## Problem

51 hardcoded limits/thresholds/patterns, NONE of which emit structured log events when triggered. When a limit changes agent behavior, the only evidence is a `print()` to stdout (sometimes not even that). This makes it impossible to:
- Debug why an agent took a certain path
- Replay and understand gate decisions
- Tune limits based on data

## Design

### Event Schema

Every limit trigger emits:
```json
{
  "type": "limit_triggered",
  "limit_name": "controlled_verify_class_limit",
  "configured_value": 20,
  "actual_value": 69,
  "action_taken": "fallback_to_module_scope",
  "file": "controlled_verify.py",
  "line": 89
}
```

### Dual Output

1. **stdout** — `[limit] controlled_verify_class_limit: 69 > 20 -> fallback_to_module_scope` (for peek)
2. **decisions.jsonl** — full JSON record (for replay/analysis)

### Implementation Helper

Add to `scripts/limit_logger.py`:
```python
import json, time

_decisions_file = None  # set by run_with_jingu_gate.py at startup

def set_decisions_file(path: str):
    global _decisions_file
    _decisions_file = path

def log_limit(limit_name: str, configured: int | float, actual: int | float,
              action: str, file: str, line: int):
    record = {
        "type": "limit_triggered",
        "ts": time.time(),
        "limit_name": limit_name,
        "configured_value": configured,
        "actual_value": actual,
        "action_taken": action,
        "file": file,
        "line": line,
    }
    print(f"    [limit] {limit_name}: {actual} vs {configured} -> {action}", flush=True)
    if _decisions_file:
        with open(_decisions_file, "a") as f:
            f.write(json.dumps(record) + "\n")
```

## Items to Instrument

### Critical Limits (C1-C16)

| Item | Where to add `log_limit()` | limit_name |
|------|---------------------------|------------|
| C1 | `controlled_verify.py:89` (when `len > 20`) | `cv_class_limit` |
| C2 | `controlled_verify.py` (on TimeoutExpired) | `cv_timeout` |
| C3 | `controlled_verify.py:83` (sentinel cap) | `cv_sentinel_limit` |
| C4 | `controlled_verify.py:414+` (sub-op timeout) | `cv_subop_timeout` |
| C5 | `step_sections.py:239` (inner verify timeout) | `inner_verify_timeout` |
| C6 | `step_sections.py:1014` (fake loop limit) | `fake_loop_limit` |
| C7 | `step_sections.py:328` (execute redirect) | `execute_redirect_limit` |
| C8 | `step_sections.py:613` (analysis gate force) | `analysis_gate_force_pass` |
| C9 | `step_sections.py:689` (design gate force) | `design_gate_force_pass` |
| C10 | `step_sections.py:883` (retryable loop) | `retryable_loop_force_pass` |
| C11 | `retry_controller.py:37` (no signal) | `no_signal_threshold` |
| C12 | `jingu_adapter.py:27` (feedback truncation) | `pytest_feedback_truncation` |
| C13 | `principal_inference.py:106` (patch lines) | `small_patch_limit` |
| C14 | `patch_reviewer.py:35` (reviewer tokens) | `reviewer_max_tokens` |
| C15 | `run_with_jingu_gate.py:681` (pull timeout) | `docker_pull_timeout` |
| C16 | `jingu_agent.py:919` (final verify timeout) | `final_verify_timeout` |

### Force-Pass Mechanisms (F1-F4)

| Item | Where | limit_name |
|------|-------|------------|
| F1 | `step_sections.py:613-614` | `analysis_gate_force_pass` |
| F2 | `step_sections.py:689-690` | `design_gate_force_pass` |
| F3 | `step_sections.py:883` | `retryable_contract_bypass` |
| F4 | `step_sections.py:1014` | `fake_selective_bypass` |

## Verification

1. Run 1 instance with known limit triggers (django__django-10097 triggers C1, C2)
2. Check `decisions.jsonl` contains `limit_triggered` records
3. Check `peek` output shows `[limit]` lines
4. Verify no limit trigger goes unlogged

## Dependencies

- P1 may change C1/C2 behavior, but logging should be added regardless
- `limit_logger.py` is a new file — add to Dockerfile COPY
