#!/usr/bin/env python3
"""traj_diff.py — Compare two traj.json files step-by-step, find divergence points.

Usage:
  from traj_diff import compare_trajs, format_comparison
  result = compare_trajs(original_traj, replayed_traj)
  print(format_comparison(result))
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
from typing import Optional


# ---------------------------------------------------------------------------
# ANSI colors (reuse pattern from replay_traj.py)
# ---------------------------------------------------------------------------

class C:
    """ANSI color codes — disabled when not a TTY."""
    ENABLED = sys.stdout.isatty()

    @staticmethod
    def _w(code: str, text: str) -> str:
        if not C.ENABLED:
            return text
        return f"{code}{text}\033[0m"

    @classmethod
    def bold(cls, t: str) -> str:    return cls._w("\033[1m", t)
    @classmethod
    def red(cls, t: str) -> str:     return cls._w("\033[31m", t)
    @classmethod
    def green(cls, t: str) -> str:   return cls._w("\033[32m", t)
    @classmethod
    def yellow(cls, t: str) -> str:  return cls._w("\033[33m", t)
    @classmethod
    def blue(cls, t: str) -> str:    return cls._w("\033[34m", t)
    @classmethod
    def magenta(cls, t: str) -> str: return cls._w("\033[35m", t)
    @classmethod
    def cyan(cls, t: str) -> str:    return cls._w("\033[36m", t)
    @classmethod
    def dim(cls, t: str) -> str:     return cls._w("\033[2m", t)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class DivergencePoint:
    """First point where two trajectories diverge."""
    step_n: int                 # 1-indexed step where behavior differs
    original_action: str        # what the original run did
    replayed_action: str        # what the replayed run did
    category: str               # tool_choice | tool_args | reasoning | phase_advance | early_stop | length
    original_phase: str
    replayed_phase: str
    confidence: float           # 0.0 - 1.0


@dataclass
class StepSummary:
    """Lightweight summary of a single agent step for comparison."""
    index: int
    phase: Optional[str]
    tool_calls: list[str]       # list of "tool_name(args_preview)"
    has_text_only: bool         # True if assistant message with no tool calls
    text_preview: str           # first 200 chars of assistant content
    files_written: list[str]
    is_submit: bool


# ---------------------------------------------------------------------------
# Step extraction from traj messages
# ---------------------------------------------------------------------------

def _detect_phase(content: str) -> Optional[str]:
    """Detect declared phase from assistant message content."""
    m = re.search(
        r"PHASE:\s*(UNDERSTAND|OBSERVE|ANALYZE|DECIDE|EXECUTE|JUDGE)",
        content, re.IGNORECASE,
    )
    return m.group(1).upper() if m else None


def _parse_action(action: dict | str) -> str:
    """Parse a single action into a comparable string representation."""
    if isinstance(action, str):
        return action[:200]

    if "command" in action:
        return f"bash({action['command'][:150]})"

    tool = action.get("tool", action.get("name", "unknown"))
    inp = action.get("input", action.get("arguments", {}))
    if isinstance(inp, dict):
        path = inp.get("path", inp.get("file_path", inp.get("filename", "")))
        if path:
            return f"{tool}({path})"
        preview = ", ".join(f"{k}={str(v)[:50]}" for k, v in list(inp.items())[:2])
        return f"{tool}({preview})"
    return f"{tool}({str(inp)[:100]})"


def _extract_tool_name(action_str: str) -> str:
    """Extract just the tool name from a parsed action string."""
    m = re.match(r"^(\w+)\(", action_str)
    return m.group(1) if m else action_str


def _detect_files_written(actions: list[dict | str]) -> list[str]:
    """Detect files written from raw actions."""
    written = []
    write_tools = {"edit_file", "write_file", "create_file", "str_replace_editor", "str_replace"}
    for action in actions:
        if not isinstance(action, dict):
            continue
        tool = action.get("tool", action.get("name", "")).lower()
        inp = action.get("input", action.get("arguments", {}))
        if isinstance(inp, dict):
            path = inp.get("path", inp.get("file_path", ""))
            if path and any(t in tool for t in write_tools):
                written.append(path)
        cmd = action.get("command", "")
        if cmd and (">" in cmd or "tee " in cmd):
            # bash redirect — rough heuristic
            m = re.search(r">\s*([/\w._-]+)", cmd)
            if m:
                written.append(m.group(1))
    return written


def _is_submit_action(actions: list[dict | str]) -> bool:
    """Check if any action is a submission."""
    for action in actions:
        if isinstance(action, dict):
            tool = action.get("tool", action.get("name", "")).lower()
            if "submit" in tool:
                return True
            cmd = action.get("command", "")
            if cmd and "submit" in cmd.lower():
                return True
        elif isinstance(action, str) and "submit" in action.lower():
            return True
    return False


def extract_steps(traj: dict) -> list[StepSummary]:
    """Extract step summaries from a traj dict.

    A step = one assistant message + its tool call actions.
    """
    messages = traj.get("messages", [])
    steps: list[StepSummary] = []
    current_phase: Optional[str] = None
    step_idx = 0

    for msg in messages:
        role = msg.get("role", "")
        if role != "assistant":
            continue

        step_idx += 1
        content = str(msg.get("content", ""))
        extra = msg.get("extra", {})
        raw_actions = extra.get("actions", [])

        # Detect phase
        phase = _detect_phase(content)
        if phase:
            current_phase = phase
        display_phase = current_phase or ""

        # Parse tool calls
        tool_calls = [_parse_action(a) for a in raw_actions]
        has_text_only = len(tool_calls) == 0

        # File operations
        files_written = _detect_files_written(raw_actions)
        is_submit = _is_submit_action(raw_actions)

        steps.append(StepSummary(
            index=step_idx,
            phase=display_phase,
            tool_calls=tool_calls,
            has_text_only=has_text_only,
            text_preview=content[:200],
            files_written=files_written,
            is_submit=is_submit,
        ))

    return steps


# ---------------------------------------------------------------------------
# Divergence detection
# ---------------------------------------------------------------------------

def _similarity(a: str, b: str) -> float:
    """Compute string similarity ratio (0.0 - 1.0)."""
    if a == b:
        return 1.0
    return SequenceMatcher(None, a[:500], b[:500]).ratio()


def find_divergence(
    original_traj: dict,
    replayed_traj: dict,
) -> Optional[DivergencePoint]:
    """Find the first point where two trajectories diverge.

    Returns None if trajectories are identical (same tool calls in same order).
    """
    orig_steps = extract_steps(original_traj)
    repl_steps = extract_steps(replayed_traj)

    min_len = min(len(orig_steps), len(repl_steps))

    for i in range(min_len):
        os = orig_steps[i]
        rs = repl_steps[i]

        # Compare tool calls
        orig_tools = os.tool_calls
        repl_tools = rs.tool_calls

        # Case 1: one has tool calls, other is text-only
        if os.has_text_only != rs.has_text_only:
            return DivergencePoint(
                step_n=i + 1,
                original_action=orig_tools[0] if orig_tools else f"text: {os.text_preview[:80]}",
                replayed_action=repl_tools[0] if repl_tools else f"text: {rs.text_preview[:80]}",
                category="reasoning",
                original_phase=os.phase or "",
                replayed_phase=rs.phase or "",
                confidence=0.9,
            )

        # Case 2: both have tool calls — compare them
        if orig_tools and repl_tools:
            # Compare the primary tool call (first one)
            orig_primary = orig_tools[0]
            repl_primary = repl_tools[0]

            orig_name = _extract_tool_name(orig_primary)
            repl_name = _extract_tool_name(repl_primary)

            if orig_name != repl_name:
                return DivergencePoint(
                    step_n=i + 1,
                    original_action=orig_primary,
                    replayed_action=repl_primary,
                    category="tool_choice",
                    original_phase=os.phase or "",
                    replayed_phase=rs.phase or "",
                    confidence=0.95,
                )

            # Same tool name — check args similarity
            sim = _similarity(orig_primary, repl_primary)
            if sim < 0.9:
                return DivergencePoint(
                    step_n=i + 1,
                    original_action=orig_primary,
                    replayed_action=repl_primary,
                    category="tool_args",
                    original_phase=os.phase or "",
                    replayed_phase=rs.phase or "",
                    confidence=1.0 - sim,  # lower similarity = higher confidence of divergence
                )

        # Case 3: both text-only — compare text similarity
        if os.has_text_only and rs.has_text_only:
            sim = _similarity(os.text_preview, rs.text_preview)
            if sim < 0.5:
                return DivergencePoint(
                    step_n=i + 1,
                    original_action=f"text: {os.text_preview[:80]}",
                    replayed_action=f"text: {rs.text_preview[:80]}",
                    category="reasoning",
                    original_phase=os.phase or "",
                    replayed_phase=rs.phase or "",
                    confidence=1.0 - sim,
                )

        # Case 4: phase divergence (even if tools match)
        if os.phase and rs.phase and os.phase != rs.phase:
            return DivergencePoint(
                step_n=i + 1,
                original_action=orig_tools[0] if orig_tools else f"text: {os.text_preview[:80]}",
                replayed_action=repl_tools[0] if repl_tools else f"text: {rs.text_preview[:80]}",
                category="phase_advance",
                original_phase=os.phase,
                replayed_phase=rs.phase,
                confidence=0.85,
            )

    # Case 5: different lengths — one stopped earlier
    if len(orig_steps) != len(repl_steps):
        shorter = "original" if len(orig_steps) < len(repl_steps) else "replayed"
        longer_steps = repl_steps if shorter == "original" else orig_steps
        at_step = min_len + 1

        # Determine if the shorter one submitted (early_stop) or just ran out (length)
        shorter_steps = orig_steps if shorter == "original" else repl_steps
        shorter_submitted = any(s.is_submit for s in shorter_steps)
        category = "early_stop" if shorter_submitted else "length"

        next_action = ""
        if min_len < len(longer_steps):
            ns = longer_steps[min_len]
            next_action = ns.tool_calls[0] if ns.tool_calls else f"text: {ns.text_preview[:80]}"

        return DivergencePoint(
            step_n=at_step,
            original_action=next_action if shorter == "replayed" else "(ended)",
            replayed_action=next_action if shorter == "original" else "(ended)",
            category=category,
            original_phase=orig_steps[-1].phase if orig_steps else "",
            replayed_phase=repl_steps[-1].phase if repl_steps else "",
            confidence=0.8,
        )

    return None


# ---------------------------------------------------------------------------
# Comprehensive comparison
# ---------------------------------------------------------------------------

def _extract_phases(traj: dict) -> list[str]:
    """Extract phase sequence from jingu_body or step content."""
    jb = traj.get("jingu_body", {})
    phase_records = jb.get("phase_records", [])
    if phase_records:
        return [pr.get("phase", "?") for pr in phase_records]
    # Fallback: extract from messages
    phases = []
    for msg in traj.get("messages", []):
        if msg.get("role") != "assistant":
            continue
        content = str(msg.get("content", ""))
        phase = _detect_phase(content)
        if phase and (not phases or phases[-1] != phase):
            phases.append(phase)
    return phases


def _extract_files_modified(traj: dict) -> list[str]:
    """Extract files modified from jingu_body or step analysis."""
    jb = traj.get("jingu_body", {})
    files = jb.get("files_written", [])
    if files:
        return files
    # Fallback: extract from steps
    steps = extract_steps(traj)
    all_files = []
    for s in steps:
        all_files.extend(s.files_written)
    return sorted(set(all_files))


def _has_submission(traj: dict) -> bool:
    """Check if traj has a non-empty submission."""
    info = traj.get("info", {})
    submission = info.get("submission", "")
    return bool(submission and submission.strip())


def compare_trajs(original_traj: dict, replayed_traj: dict) -> dict:
    """Comprehensive comparison of two trajectories.

    Returns a dict with divergence point, step counts, phases, files, submission status.
    """
    divergence = find_divergence(original_traj, replayed_traj)

    orig_steps = extract_steps(original_traj)
    repl_steps = extract_steps(replayed_traj)

    return {
        "divergence": asdict(divergence) if divergence else None,
        "original_steps": len(orig_steps),
        "replayed_steps": len(repl_steps),
        "original_phases": _extract_phases(original_traj),
        "replayed_phases": _extract_phases(replayed_traj),
        "original_files_modified": _extract_files_modified(original_traj),
        "replayed_files_modified": _extract_files_modified(replayed_traj),
        "original_submitted": _has_submission(original_traj),
        "replayed_submitted": _has_submission(replayed_traj),
    }


# ---------------------------------------------------------------------------
# Human-readable formatting
# ---------------------------------------------------------------------------

def format_comparison(comparison: dict) -> str:
    """Format a comparison dict as a human-readable string."""
    lines: list[str] = []
    div = comparison.get("divergence")

    # Header
    lines.append(C.bold("=" * 70))
    lines.append(C.bold("  TRAJECTORY COMPARISON"))
    lines.append(C.bold("=" * 70))
    lines.append("")

    # Summary
    orig_n = comparison["original_steps"]
    repl_n = comparison["replayed_steps"]
    lines.append(f"  Steps:  original={orig_n}  replayed={repl_n}  "
                 f"delta={repl_n - orig_n:+d}")

    # Phases
    orig_phases = comparison["original_phases"]
    repl_phases = comparison["replayed_phases"]
    lines.append(f"  Phases: original={' -> '.join(orig_phases) if orig_phases else '(none)'}")
    lines.append(f"          replayed={' -> '.join(repl_phases) if repl_phases else '(none)'}")
    if orig_phases == repl_phases:
        lines.append(f"          {C.green('(identical phase sequence)')}")
    else:
        lines.append(f"          {C.yellow('(phase sequences differ)')}")

    # Submission
    orig_sub = comparison["original_submitted"]
    repl_sub = comparison["replayed_submitted"]
    lines.append(f"  Submit: original={'yes' if orig_sub else 'no'}  "
                 f"replayed={'yes' if repl_sub else 'no'}")

    # Files modified
    orig_files = set(comparison["original_files_modified"])
    repl_files = set(comparison["replayed_files_modified"])
    common = orig_files & repl_files
    only_orig = orig_files - repl_files
    only_repl = repl_files - orig_files

    lines.append("")
    lines.append(C.bold("  FILES MODIFIED"))
    if common:
        for f in sorted(common):
            lines.append(f"    {C.dim('both:')} {f}")
    if only_orig:
        for f in sorted(only_orig):
            lines.append(f"    {C.red('orig only:')} {f}")
    if only_repl:
        for f in sorted(only_repl):
            lines.append(f"    {C.green('repl only:')} {f}")
    if not orig_files and not repl_files:
        lines.append(f"    {C.dim('(none)')}")

    # Divergence
    lines.append("")
    lines.append(C.bold("  DIVERGENCE"))
    if not div:
        lines.append(f"    {C.green('No divergence detected — trajectories are identical.')}")
    else:
        step_n = div["step_n"]
        cat = div["category"]
        conf = div["confidence"]

        color_fn = C.red if conf > 0.7 else C.yellow
        lines.append(f"    {color_fn(f'Divergence at step {step_n} (category: {cat}, confidence: {conf:.2f})')}")
        lines.append("")

        # Phase context
        orig_phase = div["original_phase"]
        repl_phase = div["replayed_phase"]
        if orig_phase or repl_phase:
            lines.append(f"    Phase: original={orig_phase or '?'}  replayed={repl_phase or '?'}")

        # Side-by-side divergent actions
        lines.append("")
        lines.append(f"    {C.bold('Original:')}  {div['original_action']}")
        lines.append(f"    {C.bold('Replayed:')}  {div['replayed_action']}")

        # Category explanation
        lines.append("")
        explanations = {
            "tool_choice": "Agent chose a different tool at this step.",
            "tool_args": "Same tool, but different arguments (different target file or content).",
            "reasoning": "One trajectory used a tool call while the other produced text-only reasoning.",
            "phase_advance": "Agent declared a different phase at this step.",
            "early_stop": "One trajectory submitted and stopped while the other continued.",
            "length": "Trajectories have different lengths (one ran more steps).",
        }
        explanation = explanations.get(cat, "")
        if explanation:
            lines.append(f"    {C.dim(explanation)}")

    lines.append("")
    lines.append(C.bold("=" * 70))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point (for standalone testing)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    if len(sys.argv) < 3:
        print("Usage: python traj_diff.py <original.traj.json> <replayed.traj.json>")
        sys.exit(1)

    with open(sys.argv[1]) as f:
        orig = json.load(f)
    with open(sys.argv[2]) as f:
        repl = json.load(f)

    result = compare_trajs(orig, repl)
    print(format_comparison(result))
