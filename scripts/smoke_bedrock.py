#!/usr/bin/env python3
"""Smoke test: verify Bedrock + litellm model call works without parallel_tool_calls bug."""
import litellm

MODEL = "bedrock/global.anthropic.claude-sonnet-4-5-20250929-v1:0"

resp = litellm.completion(
    model=MODEL,
    messages=[{"role": "user", "content": "Reply with OK only."}],
    parallel_tool_calls=False,
    temperature=0.0,
    max_tokens=10,
)
print("status: OK")
print("response:", resp.choices[0].message.content)
