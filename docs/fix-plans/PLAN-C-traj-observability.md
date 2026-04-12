# PLAN-C: Traj Observability for Structured Extract Calls

**Status**: PLAN COMPLETE — ready for implementation
**Priority**: HIGH (prerequisite for verifying A and B)
**Depends on**: nothing
**Blocks**: verification of Plan A effectiveness

---

## Problem

`JinguModel.structured_extract()` makes an independent LLM call with `response_format=json_schema`
but does NOT record the call in `traj.json`. This means:

- Cannot replay what the extraction LLM saw or returned
- Cannot verify Plan A's gate behavior from traj alone
- Governance three-question test Q3 fails: "replay causally complete?" = NO

## Solution

Record structured_extract calls as synthetic message pairs in `agent_self.messages`,
distinguishable by `extra.type` field.

## Message Format

```python
# Request entry
{"role": "user", "content": extraction_prompt, "extra": {
    "type": "structured_extract_request",
    "phase": "ANALYZE", "schema_name": "...", "schema": {...},
    "accumulated_text_chars": 4231, "phase_hint": "...",
    "timestamp": 1712345678.123,
}}

# Response entry
{"role": "assistant", "content": '{"phase": "ANALYZE", ...}', "extra": {
    "type": "structured_extract_response",
    "phase": "ANALYZE", "success": True, "fields": [...],
    "response": {...}, "cost": 0.0032, "timestamp": 1712345679.456,
}}
```

## Implementation Steps

### Step 1: Add ExtractRecord to JinguModel
- File: `mini-swe-agent/jingu_model.py`
- Add `ExtractRecord` dataclass
- Store `self._last_extract_record` after each `structured_extract()` call
- Record: prompt, schema, raw response, parsed result, cost, timestamps

### Step 2: Append extraction messages at call site
- File: `scripts/step_sections.py` (lines 476-509)
- After `structured_extract()`, read `_last_extract_record`
- Append request + response messages to `agent_self.messages`

### Step 3: Guard downstream consumers
- `scripts/jingu_adapter.py` `extract_jingu_body()`: skip extraction messages
- `scripts/run_with_jingu_gate.py` `_try_parse_structured_output()`: skip extraction messages
- Add utility: `is_extraction_message(msg) -> bool`

### Step 4: Update replay_traj.py
- Render extraction entries as distinct steps in replay output

### Step 5: Cost tracking
- Add `litellm.cost_calculator.completion_cost(response)` to `structured_extract()`

## Verification

1. Traj contains extraction request/response pairs after VerdictAdvance
2. `jq '[.messages[] | select(.extra.type != null) | .extra.type]' traj.json` lists entries
3. `extract_jingu_body()` output unchanged (extraction messages skipped)
4. `replay_traj.py` displays extraction calls as distinct steps
5. Cost included in per-attempt summary

## Key Files

- `mini-swe-agent/jingu_model.py` — ExtractRecord + _last_extract_record
- `scripts/step_sections.py` — append messages at call site
- `scripts/jingu_adapter.py` — skip guard in extract_jingu_body()
- `scripts/run_with_jingu_gate.py` — skip guard in _try_parse_structured_output()
- `scripts/replay_traj.py` — display extraction entries
