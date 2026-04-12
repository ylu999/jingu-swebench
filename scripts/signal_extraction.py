"""
Signal extraction — stateless fact extraction from agent messages.

Extracted from run_with_jingu_gate.py (p225-02).

Pure functions: given ONE step's observable data, return what happened.
No history, no control decisions, no governance.
"""


# ── Signal detection constants ────────────────────────────────────────────────

# Tool names that indicate a write or submit action (non-bash structured calls)
_SIGNAL_TOOL_NAMES: frozenset[str] = frozenset({
    "edit_file", "write_file", "create_file",
    "str_replace_editor", "str_replace", "apply_patch",
    "bash_write", "patch", "submit",
})

# Bash command fragments that indicate a write or submit signal
# Covers: shell file writes (cat > file, tee file), submit sentinel, inline patches
_SIGNAL_BASH_PATTERNS: tuple[str, ...] = (
    "cat >",           # shell file write: cat > /path/to/file
    "tee ",            # shell file write via tee
    "COMPLETE_TASK_AND_SUBMIT",   # SWE-bench submit sentinel
    "> /testbed/",     # redirect-write into testbed
    "str_replace",     # bash str_replace call
    "apply_patch",     # bash apply_patch call
)



# Bash command fragments that indicate environment mutation inside agent loop.
# ENVIRONMENT_NOT_AGENT_WORK: agent must not install packages or modify env during reasoning.
_ENV_MUTATION_PATTERNS: tuple[str, ...] = (
    "pip install",
    "pip3 install",
    "uv pip install",
    "uv add ",
    "python setup.py install",
    "python setup.py develop",
    "poetry install",
    "conda install",
    "apt install",
    "apt-get install",
    "dnf install",
    "yum install",
    "brew install",
)


# ── Signal extraction functions ───────────────────────────────────────────────

def _msg_has_env_mutation(msg: dict) -> tuple[bool, str]:
    """
    Return (True, trigger) if an assistant message attempts environment mutation.

    Detects pip install, setup.py install, conda install, etc. inside agent steps.
    These belong to infrastructure/harness, not agent reasoning.
    Violation: ENVIRONMENT_MUTATION_IN_AGENT_LOOP
    """
    def _check_cmd(cmd: str) -> str | None:
        cmd_lower = cmd.lower()
        for pat in _ENV_MUTATION_PATTERNS:
            if pat in cmd_lower:
                return pat
        return None

    # Source 1: structured tool_calls bash commands
    for tc in msg.get("tool_calls", []):
        if tc.get("function", {}).get("name", "").lower() == "bash":
            try:
                import json as _json
                args = tc.get("function", {}).get("arguments", "")
                cmd = (_json.loads(args) if isinstance(args, str) else args).get("command", "")
            except Exception:
                cmd = ""
            trigger = _check_cmd(cmd)
            if trigger:
                return True, trigger

    # Source 2: extra.actions
    for action in msg.get("extra", {}).get("actions", []):
        cmd = action.get("command", "") if isinstance(action, dict) else ""
        trigger = _check_cmd(cmd)
        if trigger:
            return True, trigger

    return False, ""


def _msg_has_signal(msg: dict) -> bool:
    """
    Return True if an assistant message contains at least one write/submit signal.

    Checks two sources:
    1. msg.tool_calls[].function.name — structured tool calls (str_replace_editor etc.)
    2. msg.extra.actions[].command — bash shell commands (cat >, SUBMIT sentinel etc.)

    Both formats appear in real trajs: structured tool calls are in tool_calls,
    the corresponding shell commands are mirrored in extra.actions with a 'command' key.
    """
    # Source 1: structured tool_calls (non-bash tool names)
    for tc in msg.get("tool_calls", []):
        name = tc.get("function", {}).get("name", "").lower()
        if any(sig in name for sig in _SIGNAL_TOOL_NAMES):
            return True
        # bash tool — check command content below
        if name == "bash":
            cmd = ""
            try:
                import json as _json
                args = tc.get("function", {}).get("arguments", "")
                cmd = (_json.loads(args) if isinstance(args, str) else args).get("command", "")
            except Exception:
                pass
            if any(p in cmd for p in _SIGNAL_BASH_PATTERNS):
                return True

    # Source 2: extra.actions (may have 'tool' key or just 'command' key)
    for action in msg.get("extra", {}).get("actions", []):
        if not isinstance(action, dict):
            action_str = str(action).lower()
            if any(sig in action_str for sig in _SIGNAL_TOOL_NAMES):
                return True
            continue
        # Structured action with tool name
        tool_name = action.get("tool", action.get("name", "")).lower()
        if tool_name and any(sig in tool_name for sig in _SIGNAL_TOOL_NAMES):
            return True
        # Shell command content
        cmd = action.get("command", "")
        if cmd and any(p in cmd for p in _SIGNAL_BASH_PATTERNS):
            return True

    return False


def compute_steps_since_last_signal(traj_msgs: list[dict]) -> int:
    """
    Count consecutive trailing steps with no write/submit signal.

    p164 runner layer: feeds steps_since_last_signal into build_retry_plan()
    for P7 no-signal detection (STOP_NO_SIGNAL when >= NO_SIGNAL_THRESHOLD).

    A "step" is one assistant turn. A "signal" is any write or submit action.
    Counts from the end of the conversation backward to the most recent signal.

    Signal detection covers both structured tool calls (str_replace_editor etc.)
    and bash shell commands (cat > file, COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT).
    """
    steps_without_signal = 0
    for msg in reversed(traj_msgs):
        if msg.get("role") != "assistant":
            continue
        # Plan-C: skip structured_extract traj entries
        if msg.get("extra", {}).get("type", "").startswith("structured_extract_"):
            continue
        if _msg_has_signal(msg):
            break
        steps_without_signal += 1
    return steps_without_signal
