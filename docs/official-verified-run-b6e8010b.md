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
| **Resolved** | ✅ 1/1 | (eval pending) | (eval pending) |
| Model | claude-sonnet-4-5 | claude-sonnet-4-5 | claude-sonnet-4-5 |
| Thinking | reasoning_effort=high | budget_tokens=10000 | budget_tokens=10000 |
| API Calls | 21 | 35 | 45 |
| Cost | $0.236 | $0.328 | $0.399 |
| Wall time | — | 464s | 324s |
| Attempts | 1 | 1 | 1 |

**Notes:**
- v1 baseline (temperature=0, no extended thinking): 54 calls, $0.566 — wasted steps on pip install + file write failures
- v2 uses extended thinking (budget_tokens=10000) matching official config
- Baseline v2: 35 calls vs official 21 — gap likely due to mini_version differences (v2.0.0 vs our version)
- Jingu v2: 45 calls — higher than baseline because jingu gate adds overhead (declaration protocol + hint injection)
- Eval (resolved/unresolved) to be run separately
