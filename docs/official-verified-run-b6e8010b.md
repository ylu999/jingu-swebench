# Official Verified Run: b6e8010b

Collection: `20260217_mini-v2.0.0_claude-4-5-sonnet-high` / `737e5dd2`

## Key Metadata

| Field | Value |
|-------|-------|
| Agent Run ID | `b6e8010b` |
| Instance | `django__django-11099` |
| Resolved | `1` (True) |
| Exit Status | Submitted |
| API Calls | 21 |
| Cost | $0.236 |
| mini_version | 2.1.0 |
| trajectory_format | mini-swe-agent-1.1 |
| Transcript | `5bbb55bf` |
| Total messages | 44 |

## Model Config

```json
{
  "model_name": "anthropic/claude-sonnet-4-5-20250929",
  "model_kwargs": {
    "drop_params": true,
    "temperature": null,
    "extra_headers": {
      "anthropic-beta": "interleaved-thinking-2025-05-14"
    },
    "reasoning_effort": "high",
    "parallel_tool_calls": true
  }
}
```

**Note:** `reasoning_effort: high` = Anthropic direct API extended thinking.
Bedrock equivalent: `thinking: {type: enabled, budget_tokens: 10000}` + `temperature: 1`.

## Problem

`ASCIIUsernameValidator` and `UnicodeUsernameValidator` use `r'^[\w.@+-]+$'`.
Python `$` matches before trailing newline, so `username\n` is accepted.

## Fix

Change `^...$` → `\A...\Z` in both validators in `django/contrib/auth/validators.py`.

## Transcript URL

https://docent.transluce.org/dashboard/737e5dd2-8555-435a-9fbd-1c6907c972f1/agent_run/b6e8010b-bedc-4c38-8e1d-93e6ce53ae6a

---

## 3-Way Comparison: django__django-11099

| | Official (b6e8010b) | Our Baseline (v2) | Our Jingu (v2) |
|---|---|---|---|
| **Resolved** | ✅ 1/1 | ✅ 1/1 | ✅ 1/1 |
| Model | claude-sonnet-4-5 | claude-sonnet-4-5 | claude-sonnet-4-5 |
| Thinking | reasoning_effort=high | reasoning_effort=high | reasoning_effort=high |
| API Calls | 21 | 30 | 44 |
| Cost | $0.236 | $0.360 | $0.432 |
| Wall time | — | 416s | 409s |
| Attempts | 1 | 1 | 1 (gate: no rescue needed) |

**Notes:**
- v1 baseline (temperature=0, no extended thinking): 54 calls, $0.566 — wasted steps on pip install + file write failures
- v2 (Bedrock): thinking param silently ignored on Bedrock Sonnet 4.5 — thinking_blocks=0 in trajectory
- v4 (this run): Anthropic direct API + reasoning_effort=high (= budget_tokens=4096 via litellm) — correct extended thinking
- Baseline v4: 30 calls vs official 21 — still a gap, likely mini_version differences (2.1.0 same but our config layer adds overhead)
- Jingu v4: 44 calls — higher than baseline; jingu gate adds declaration protocol + controlled_verify overhead
- Jingu attempt1 accepted (gate passed) → no attempt2 needed; resolved on first try
