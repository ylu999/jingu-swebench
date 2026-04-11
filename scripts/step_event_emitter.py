"""Step-level event emitter — writes structured per-step events to JSONL.

Each attempt gets its own ``attempt_N/step_events.jsonl`` file.
Events are append-only and flushed immediately for crash safety.

p228: step-level event log for post-hoc visibility.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class StepEvent:
    """One structured event per agent step."""

    step_n: int
    timestamp_ms: float
    phase: str | None = None
    gate_verdict: str | None = None
    gate_reason: str | None = None
    cp_state_snapshot: dict[str, Any] | None = None
    tool_calls_count: int = 0
    files_read: list[str] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    step_duration_ms: float = 0.0
    patch_non_empty: bool = False
    env_error: bool = False


class StepEventEmitter:
    """Writes StepEvent records as JSON lines to ``attempt_N/step_events.jsonl``.

    Usage::

        emitter = StepEventEmitter(output_dir / instance_id, attempt=1)
        emitter.emit(event)
        emitter.close()
    """

    def __init__(self, base_dir: Path, attempt: int) -> None:
        self._dir = base_dir / f"attempt_{attempt}"
        self._fh: Any | None = None
        try:
            os.makedirs(self._dir, exist_ok=True)
            self._fh = open(self._dir / "step_events.jsonl", "a", encoding="utf-8")
        except Exception as exc:
            print(f"    [step-emitter] WARNING: could not open step_events.jsonl: {exc}", flush=True)
            self._fh = None

    def emit(self, event: StepEvent) -> None:
        """Append one JSON line. Never raises."""
        if self._fh is None:
            return
        try:
            line = json.dumps(dataclasses.asdict(event), ensure_ascii=False)
            self._fh.write(line + "\n")
            self._fh.flush()
        except Exception as exc:
            print(f"    [step-emitter] WARNING: emit failed: {exc}", flush=True)

    def close(self) -> None:
        """Flush and close the file handle. Safe to call multiple times."""
        if self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            except Exception:
                pass
            self._fh = None


def extract_tool_usage(messages: list[dict[str, Any]], step_n: int) -> tuple[int, list[str], list[str]]:
    """Extract tool call counts and file paths from recent agent messages.

    Scans the last few messages for tool_call / tool entries belonging to the
    current step.  Classification:

    - read/cat/view → files_read
    - write/edit/patch/create → files_written

    Returns (tool_calls_count, files_read, files_written).
    Never raises.
    """
    tool_calls_count = 0
    files_read: list[str] = []
    files_written: list[str] = []

    try:
        # Look at last 20 messages (generous window for one step)
        recent = messages[-20:] if len(messages) > 20 else messages
        for msg in recent:
            role = msg.get("role", "")

            # Count tool calls from assistant messages
            if role == "assistant":
                tool_calls = msg.get("tool_calls", [])
                if isinstance(tool_calls, list):
                    tool_calls_count += len(tool_calls)
                    for tc in tool_calls:
                        fn = tc.get("function", {})
                        name = fn.get("name", "").lower()
                        args = fn.get("arguments", "")
                        # Try to extract file path from arguments
                        _path = _extract_path_from_args(args)
                        if not _path:
                            continue
                        if any(kw in name for kw in ("read", "cat", "view", "open")):
                            files_read.append(_path)
                        elif any(kw in name for kw in ("write", "edit", "patch", "create", "str_replace")):
                            files_written.append(_path)
    except Exception:
        pass

    return tool_calls_count, files_read, files_written


def _extract_path_from_args(args: str | dict[str, Any]) -> str:
    """Best-effort path extraction from tool call arguments. Never raises."""
    try:
        if isinstance(args, str):
            args = json.loads(args)
        if isinstance(args, dict):
            for key in ("path", "file_path", "file", "filename", "file_name"):
                val = args.get(key)
                if isinstance(val, str) and val:
                    return val
    except Exception:
        pass
    return ""
