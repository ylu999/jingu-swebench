"""Failure Event Extraction Pipeline (p216 — Wave 3).

Extracts structured FailureEvent objects from trajectory JSON files.
These events feed the routing matrix for data-driven failure routing.

Data sources within a traj:
  - jingu_body.phase_records: phase declarations with principals
  - jingu_body.principal_inference: inferred vs declared principals (diff.fake, diff.missing_required)
  - jingu_body.controlled_verify: test results (f2p_passed/failed, eval_resolved)
  - messages: fallback for older trajs without jingu_body

Each FailureEvent captures a single failure signal at a specific
(phase, principal) coordinate within an attempt.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


@dataclass
class FailureEvent:
    """A single failure signal extracted from a trajectory.

    Represents one (phase, principal, failure_type) observation
    at a specific attempt within a run.
    """

    instance_id: str
    phase: str
    field: str  # which field or area failed (e.g. "root_cause", "evidence")
    principal: str  # which principal was violated (e.g. "causal_grounding")
    reason: str  # failure reason (e.g. "missing_required", "fake", "f2p_failed")
    outcome: str  # "resolved" | "unresolved" | "regressed"
    next_phase: str  # what phase followed this failure
    attempt: int


def _extract_from_jingu_body(
    traj: dict,
    instance_id: str,
) -> list[FailureEvent]:
    """Extract failure events from jingu_body (governance-enriched traj).

    Sources:
      1. principal_inference[].diff.missing_required -> missing required principal
      2. principal_inference[].diff.fake -> declared but not behaviorally present
      3. principal_inference[].diff.missing_expected -> missing expected principal
      4. controlled_verify -> f2p test failure signals
      5. phase_records with admission status -> gate rejections
    """
    jb = traj.get("jingu_body", {})
    if not jb:
        return []

    events: list[FailureEvent] = []

    # Determine outcome from controlled_verify
    cv = jb.get("controlled_verify", {})
    f2p_passed = cv.get("f2p_passed") or 0
    f2p_failed = cv.get("f2p_failed") or 0
    eval_resolved = cv.get("eval_resolved", False)
    p2p_failed = cv.get("p2p_failed") or 0

    if eval_resolved:
        overall_outcome = "resolved"
    elif p2p_failed > 0:
        overall_outcome = "regressed"
    else:
        overall_outcome = "unresolved"

    # Extract phase sequence for next_phase tracking
    phase_records = jb.get("phase_records", [])
    phase_sequence = [pr.get("phase", "UNKNOWN") for pr in phase_records]

    # 1. Principal inference failures
    pi_list = jb.get("principal_inference", [])
    for idx, pi in enumerate(pi_list):
        phase = pi.get("phase", "UNKNOWN")
        subtype = pi.get("subtype", "")
        diff = pi.get("diff", {})
        # next phase: the phase after this one in sequence, or same phase
        next_idx = idx + 1
        next_phase = phase_sequence[next_idx] if next_idx < len(phase_sequence) else phase

        # Missing required principals
        for principal in diff.get("missing_required", []):
            events.append(FailureEvent(
                instance_id=instance_id,
                phase=phase,
                field=subtype or "principal_gate",
                principal=principal,
                reason="missing_required",
                outcome=overall_outcome,
                next_phase=next_phase,
                attempt=1,  # will be overridden by caller if multi-attempt
            ))

        # Fake principals (declared but not behaviorally present)
        for principal in diff.get("fake", []):
            events.append(FailureEvent(
                instance_id=instance_id,
                phase=phase,
                field=subtype or "principal_gate",
                principal=principal,
                reason="fake_declaration",
                outcome=overall_outcome,
                next_phase=next_phase,
                attempt=1,
            ))

        # Missing expected principals (softer signal)
        for principal in diff.get("missing_expected", []):
            events.append(FailureEvent(
                instance_id=instance_id,
                phase=phase,
                field=subtype or "principal_gate",
                principal=principal,
                reason="missing_expected",
                outcome=overall_outcome,
                next_phase=next_phase,
                attempt=1,
            ))

    # 2. Test failure signals from controlled_verify
    if f2p_failed > 0 and not eval_resolved:
        # Determine the phase where execution happened
        exec_phase = "EXECUTE"
        for pr in reversed(phase_records):
            if pr.get("phase", "").upper() in ("EXECUTE", "EXECUTION"):
                exec_phase = pr.get("phase", "EXECUTE")
                break

        if f2p_passed == 0:
            failure_reason = "wrong_direction"
        elif f2p_passed > 0:
            failure_reason = "incomplete_fix"
        else:
            failure_reason = "test_failure"

        events.append(FailureEvent(
            instance_id=instance_id,
            phase=exec_phase,
            field="controlled_verify",
            principal="execution_correctness",
            reason=failure_reason,
            outcome=overall_outcome,
            next_phase=exec_phase,
            attempt=1,
        ))

    # 3. P2P regression signal
    if p2p_failed > 0:
        events.append(FailureEvent(
            instance_id=instance_id,
            phase="EXECUTE",
            field="controlled_verify",
            principal="minimal_change",
            reason="p2p_regression",
            outcome="regressed",
            next_phase="EXECUTE",
            attempt=1,
        ))

    return events


def _extract_from_messages(
    traj: dict,
    instance_id: str,
) -> list[FailureEvent]:
    """Extract failure events from raw messages (older traj format).

    Parses assistant messages for phase declarations and tool outputs
    for test failure signals.
    """
    messages = traj.get("messages", [])
    if not messages:
        return []

    events: list[FailureEvent] = []
    current_phase = "UNKNOWN"
    has_patch = False
    test_failed = False

    for msg in messages:
        role = msg.get("role", "")
        content = str(msg.get("content", ""))

        if role == "assistant":
            # Detect phase declarations
            m = re.search(
                r"PHASE:\s*(UNDERSTAND|OBSERVE|ANALYZE|DECIDE|EXECUTE|JUDGE)",
                content,
                re.IGNORECASE,
            )
            if m:
                current_phase = m.group(1).upper()

            # Detect if a patch was written
            extra = msg.get("extra", {})
            actions = extra.get("actions", [])
            for action in actions:
                if isinstance(action, dict):
                    tool = action.get("tool", action.get("name", ""))
                    if any(t in str(tool).lower() for t in ("edit", "write", "create", "str_replace")):
                        has_patch = True

        elif role == "tool":
            # Detect test failures from tool output
            if "FAILED" in content or "failed" in content:
                test_failed = True

    # Generate events based on observed signals
    info = traj.get("info", {})
    exit_status = info.get("exit_status", "")

    if not has_patch:
        events.append(FailureEvent(
            instance_id=instance_id,
            phase=current_phase,
            field="patch",
            principal="action_grounding",
            reason="no_patch_produced",
            outcome="unresolved",
            next_phase=current_phase,
            attempt=1,
        ))
    elif test_failed:
        events.append(FailureEvent(
            instance_id=instance_id,
            phase="EXECUTE",
            field="test_output",
            principal="execution_correctness",
            reason="test_failure",
            outcome="unresolved",
            next_phase="EXECUTE",
            attempt=1,
        ))

    return events


def extract_failure_events(traj_path: str) -> list[FailureEvent]:
    """Parse trajectory JSON -> list of FailureEvent.

    Reads traj file, finds gate rejection events,
    extracts phase, principal, failure reason, and tracks
    whether the issue was resolved in subsequent attempts.

    Handles both governance-enriched (jingu_body) and
    older message-only traj formats.

    Args:
        traj_path: path to a .traj.json file

    Returns:
        List of FailureEvent objects extracted from the trajectory.
    """
    with open(traj_path) as f:
        traj = json.load(f)

    instance_id = traj.get("instance_id", "")
    if not instance_id:
        instance_id = Path(traj_path).stem.replace(".traj", "")

    # Prefer jingu_body extraction (richer data)
    if "jingu_body" in traj and traj["jingu_body"]:
        events = _extract_from_jingu_body(traj, instance_id)
    else:
        events = _extract_from_messages(traj, instance_id)

    return events


def extract_failure_events_from_dict(
    traj: dict,
    instance_id: str = "",
    attempt: int = 1,
) -> list[FailureEvent]:
    """Extract failure events from an already-loaded traj dict.

    Useful for processing trajs loaded from S3 or other sources.

    Args:
        traj: loaded traj dictionary
        instance_id: override instance_id (uses traj's if empty)
        attempt: attempt number to assign to events

    Returns:
        List of FailureEvent objects.
    """
    iid = instance_id or traj.get("instance_id", "unknown")

    if "jingu_body" in traj and traj["jingu_body"]:
        events = _extract_from_jingu_body(traj, iid)
    else:
        events = _extract_from_messages(traj, iid)

    # Override attempt number
    for ev in events:
        ev.attempt = attempt

    return events


def extract_from_batch_dir(batch_dir: str) -> list[FailureEvent]:
    """Extract failure events from all trajs in a batch directory.

    Walks the directory tree looking for *.traj.json files,
    infers attempt number from directory structure.

    Args:
        batch_dir: path to batch results directory

    Returns:
        Combined list of FailureEvent objects from all trajs.
    """
    all_events: list[FailureEvent] = []
    batch_path = Path(batch_dir)

    for traj_file in sorted(batch_path.rglob("*.traj.json")):
        # Infer attempt number from path (e.g. attempt_1/...)
        attempt = 1
        for part in traj_file.parts:
            m = re.match(r"attempt_(\d+)", part)
            if m:
                attempt = int(m.group(1))
                break

        try:
            events = extract_failure_events(str(traj_file))
            for ev in events:
                ev.attempt = attempt
            all_events.extend(events)
        except (json.JSONDecodeError, OSError) as e:
            print(f"WARN: failed to process {traj_file}: {e}")

    return all_events


def failure_events_to_dicts(events: list[FailureEvent]) -> list[dict]:
    """Convert FailureEvent list to list of dicts for JSON serialization."""
    return [asdict(ev) for ev in events]
