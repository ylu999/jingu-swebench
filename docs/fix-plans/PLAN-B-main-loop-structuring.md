# PLAN-B: Main Loop Partial Structuring — Per-Phase Records by Construction

**Status**: PLAN COMPLETE — Option C (Hybrid: Dual-Tool + Structured Fallback) selected
**Priority**: MEDIUM (depends on Plan A)
**Depends on**: Plan A (extract-as-gate) — complementary, not blocking

---

## Recommendation: Option C (Hybrid)

Add `phase_record` as a second tool alongside `bash`. If agent calls it, phase record
is produced by construction (zero extra LLM calls). If not, fall back to existing
`structured_extract()` (current behavior, gated by Plan A).

### Three Options Evaluated

| Option | Approach | Extra LLM calls | Risk |
|--------|----------|-----------------|------|
| A | Dual-tool (`bash` + `phase_record`) | 0 | Agent may ignore new tool |
| B | Mode switching (`response_format` at boundaries) | +1 per transition | Bedrock doesn't support `response_format` + `tools` simultaneously |
| C | **Hybrid (A + fallback)** | 0 when tool used, +1 on fallback | **Zero regression risk** |

Option B eliminated: Bedrock API limitation.

## Implementation Phases

### Phase 1: Tool Definition + Parser Extension
- `jingu_model.py`: override `_query()` to pass `tools=[BASH_TOOL, PHASE_RECORD_TOOL]`
- New: `phase_record_tool.py` — tool definition + schema builder
- `jingu_agent.py`: handle `phase_record` tool calls in `on_step_end()`

### Phase 2: Prompt Engineering
- `phase_prompt.py`: instruct agent to call `phase_record` at phase boundaries

### Phase 3: Enforcement
- `step_sections.py`: on VerdictAdvance, prefer tool-call record over `structured_extract`
- Log `extraction_method=tool_call` vs `structured_extract` vs `regex_fallback`

### Phase 4 (future): Full structuring
- Make `phase_record` mandatory when adoption >90%

## Key Files

- `mini-swe-agent/.../jingu_model.py` — `_query()` override for dual tools
- `mini-swe-agent/.../actions_toolcall.py` — parser extension for non-bash tools
- `scripts/jingu_agent.py` — phase_record tool call handling
- `scripts/step_sections.py` — VerdictAdvance: check for pre-existing tool-call records
- `scripts/phase_schemas.py` — schema source for `PHASE_RECORD_TOOL`

## Cost Impact

Current: ~$0.015-0.025/attempt for structured_extract calls
With Plan B: ~$0.006/attempt (tool schema overhead only at 90% adoption)
Net savings: ~$10-20 per 500-instance batch
