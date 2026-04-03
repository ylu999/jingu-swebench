"""
declaration_extractor.py — Extract type/principal declaration from agent output.

Looks for FIX_TYPE: and PRINCIPALS: lines in the last 500 chars of agent output.
Returns {"type": str, "principals": [str]} or {} if not found.

This is structural extraction — deterministic regex, no LLM.
"""

import re
from typing import TypedDict


class Declaration(TypedDict, total=False):
    type: str
    principals: list[str]


_FIX_TYPE_RE = re.compile(r"FIX_TYPE:\s*([a-z_]+)", re.IGNORECASE)
_PRINCIPALS_RE = re.compile(r"PRINCIPALS:\s*([^\n]+)", re.IGNORECASE)


def extract_declaration(agent_output: str) -> Declaration:
    """
    Extract fix type and principals from agent output.

    Scans the last 500 characters where declarations are expected to appear.
    Returns {} if FIX_TYPE is not found (opt-in gate).
    """
    if not agent_output:
        return {}

    tail = agent_output[-500:]

    type_match = _FIX_TYPE_RE.search(tail)
    if not type_match:
        return {}

    fix_type = type_match.group(1).strip().lower()

    principals: list[str] = []
    principals_match = _PRINCIPALS_RE.search(tail)
    if principals_match:
        raw = principals_match.group(1).strip()
        # Accept comma or space separated principals
        principals = [p.strip().lower() for p in re.split(r"[,\s]+", raw) if p.strip()]

    return {"type": fix_type, "principals": principals}


def extract_last_agent_message(messages: list[dict]) -> str:
    """
    Extract the last assistant message text from a traj messages list.
    Returns "" if no assistant message found.
    """
    for m in reversed(messages):
        if m.get("role") != "assistant":
            continue
        content = m.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # Claude API format: list of content blocks
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            return "\n".join(parts)
    return ""


if __name__ == "__main__":
    # Smoke test
    sample = """
After applying the fix:

FIX_TYPE: execution
PRINCIPALS: evidence_based minimal_change causality
"""
    result = extract_declaration(sample)
    assert result["type"] == "execution", f"expected execution, got {result}"
    assert "evidence_based" in result["principals"], result
    print("PASS declaration_extractor smoke test")

    # No declaration
    assert extract_declaration("some output without declaration") == {}
    print("PASS no-declaration returns empty dict")
