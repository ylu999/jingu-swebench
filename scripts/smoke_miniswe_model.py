#!/usr/bin/env python3
"""Smoke test: mini-SWE-agent model init + single query."""
from minisweagent.config import get_config_from_spec
from minisweagent.models import get_model
from minisweagent.utils.serialize import recursive_merge

configs = [
    get_config_from_spec("swebench.yaml"),
    get_config_from_spec("model.model_class=litellm"),
    get_config_from_spec("model.model_kwargs.parallel_tool_calls=false"),
]
config = {}
for c in configs:
    config = recursive_merge(config, c)
config["model"]["model_name"] = "bedrock/global.anthropic.claude-sonnet-4-5-20250929-v1:0"

model = get_model(config=config.get("model", {}))
print("model init OK:", type(model).__name__)

msgs = [
    model.format_message(role="system", content="You are a test assistant."),
    model.format_message(role="user", content="Reply with OK only."),
]
response = model.query(msgs)
print("query OK")
print("response:", str(response)[:200])
