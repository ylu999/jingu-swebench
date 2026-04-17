"""Replay L1: Governance Scorer — outcome-blind governance metrics from traj artifacts.

Reads step_events.jsonl and decisions.jsonl (per-attempt) from S3 or local disk.
Produces governance metrics without any reference to gold patches or answers.

Usage:
    from replay.scoring.governance import score_governance
    result = score_governance("/path/to/instance_dir", attempt=1)
    # or from S3:
    result = score_governance_s3("batch-name", "django__django-10914", attempt=1)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# ── Governance metrics ──────────────────────────────────────────────────────

@dataclass
class GovernanceMetrics:
    """L1 governance metrics — no gold answer needed."""

    # Phase discipline
    phase_sequence: list[str] = field(default_factory=list)
    total_steps: int = 0
    steps_per_phase: dict[str, int] = field(default_factory=dict)
    phase_advance_count: int = 0
    phase_budget_exceeded_count: int = 0

    # Redirect / retry (P1.1 core)
    redirect_count: int = 0
    retry_count: int = 0
    redirects: list[dict[str, Any]] = field(default_factory=list)
    retries: list[dict[str, Any]] = field(default_factory=list)

    # Effective redirect: did behavior change after redirect?
    effective_redirect_count: int = 0
    effective_redirect_rate: float = 0.0

    # Retry specificity
    generic_retry_count: int = 0
    specific_retry_count: int = 0
    generic_retry_ratio: float = 0.0

    # Admission
    admitted_record_count: int = 0
    tolerated_advance_count: int = 0

    # Post-redirect behavior shift
    post_redirect_behavior_shifts: list[dict[str, Any]] = field(default_factory=list)

    # Composite score (0.0 - 1.0)
    governance_score: float = 0.0


def score_governance(instance_dir: str | Path, attempt: int = 1) -> GovernanceMetrics:
    """Score governance from local step_events.jsonl + decisions.jsonl.

    Args:
        instance_dir: path to the instance output directory (contains attempt_N/)
        attempt: which attempt to score (1-based)

    Returns:
        GovernanceMetrics with all L1 governance signals.
    """
    base = Path(instance_dir) / f"attempt_{attempt}"
    events = _read_jsonl(base / "step_events.jsonl")
    decisions = _read_jsonl(base / "decisions.jsonl")
    return _compute_metrics(events, decisions)


def score_governance_s3(
    batch_name: str,
    instance_id: str,
    attempt: int = 1,
    bucket: str = "jingu-swebench-results",
) -> GovernanceMetrics:
    """Score governance from S3 artifacts.

    Downloads step_events.jsonl and decisions.jsonl from S3, scores them.
    """
    import tempfile
    import boto3

    s3 = boto3.client("s3", region_name="us-west-2")
    prefix = f"{batch_name}/{instance_id}/attempt_{attempt}/"

    events: list[dict] = []
    decisions: list[dict] = []

    for suffix, target in [("step_events.jsonl", events), ("decisions.jsonl", decisions)]:
        key = prefix + suffix
        try:
            obj = s3.get_object(Bucket=bucket, Key=key)
            body = obj["Body"].read().decode("utf-8")
            for line in body.strip().split("\n"):
                if line.strip():
                    target.append(json.loads(line))
        except Exception:
            pass  # file may not exist

    return _compute_metrics(events, decisions)


# ── Internal scoring logic ──────────────────────────────────────────────────

def _compute_metrics(
    events: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> GovernanceMetrics:
    """Core scoring logic. Pure function, no I/O."""
    m = GovernanceMetrics()

    # ── Phase sequence from step events ──
    if events:
        m.total_steps = len(events)
        prev_phase = None
        for e in events:
            phase = e.get("phase")
            if phase and phase != prev_phase:
                m.phase_sequence.append(phase)
                prev_phase = phase
            if phase:
                m.steps_per_phase[phase] = m.steps_per_phase.get(phase, 0) + 1

    # ── Parse decisions ──
    for d in decisions:
        dt = d.get("decision_type", "")
        verdict = d.get("verdict", "")
        reason = d.get("reason_text", "")
        signals = d.get("signals_evaluated") or {}

        if dt == "phase_advance":
            m.phase_advance_count += 1

        elif dt == "gate_verdict":
            if "phase_budget_exceeded" in reason:
                m.phase_budget_exceeded_count += 1
            if verdict == "tolerated_advance":
                m.tolerated_advance_count += 1

        elif dt == "gate_redirect":
            m.redirect_count += 1
            m.redirects.append({
                "step": d.get("step_n"),
                "from": d.get("phase_from"),
                "to": d.get("phase_to"),
                "reason": reason,
                "strategy": signals.get("strategy"),
                "repair_hints": signals.get("repair_hints", []),
            })

        elif dt == "gate_retry":
            m.retry_count += 1
            hints = signals.get("repair_hints", [])
            entry = {
                "step": d.get("step_n"),
                "phase": d.get("phase_from"),
                "reason": reason,
                "strategy": signals.get("strategy"),
                "repair_hints": hints,
            }
            m.retries.append(entry)
            if hints and any(len(h) > 10 for h in hints):
                m.specific_retry_count += 1
            else:
                m.generic_retry_count += 1

    # ── Effective redirect detection ──
    # A redirect is "effective" if the agent's behavior changes after it:
    # specifically, if there are steps in the redirected-to phase that show
    # different activity than before the redirect.
    for redir in m.redirects:
        redir_step = redir["step"]
        redir_to = redir["to"]
        if _detect_behavior_shift(events, redir_step, redir_to):
            m.effective_redirect_count += 1
            m.post_redirect_behavior_shifts.append({
                "redirect_step": redir_step,
                "target_phase": redir_to,
                "shift_detected": True,
            })

    m.effective_redirect_rate = (
        m.effective_redirect_count / m.redirect_count
        if m.redirect_count > 0 else 0.0
    )

    # ── Generic retry ratio ──
    total_retries = m.specific_retry_count + m.generic_retry_count
    m.generic_retry_ratio = (
        m.generic_retry_count / total_retries
        if total_retries > 0 else 0.0
    )

    # ── Admitted records (from cp_state_snapshot) ──
    if events:
        last_event = events[-1]
        cp = last_event.get("cp_state_snapshot") or {}
        m.admitted_record_count = cp.get("phase_records_count", 0)

    # ── Composite governance score ──
    m.governance_score = _compute_composite_score(m)

    return m


def _detect_behavior_shift(
    events: list[dict[str, Any]],
    redirect_step: int,
    target_phase: str,
) -> bool:
    """Detect whether agent behavior changed after a redirect.

    A shift is detected if, after the redirect step, the agent:
    1. Actually enters the target phase, AND
    2. Performs different actions (reads different files, or starts gathering evidence)
       compared to what it was doing before the redirect.
    """
    pre_redirect_files: set[str] = set()
    post_redirect_files: set[str] = set()
    post_redirect_in_target = False

    for e in events:
        step = e.get("step_n", 0)
        phase = e.get("phase", "")
        files_read = e.get("files_read", [])
        files_written = e.get("files_written", [])
        all_files = set(files_read + files_written)

        if step < redirect_step:
            pre_redirect_files.update(all_files)
        elif step > redirect_step:
            if phase == target_phase:
                post_redirect_in_target = True
                post_redirect_files.update(all_files)

    if not post_redirect_in_target:
        return False

    # Shift = agent explored new files in the target phase
    new_files = post_redirect_files - pre_redirect_files
    return len(new_files) > 0 or len(post_redirect_files) > 0


def _compute_composite_score(m: GovernanceMetrics) -> float:
    """Compute a composite governance score (0.0 - 1.0).

    Scoring rubric:
    - Phase discipline (0.3): proper phase sequence, no skipping ANALYZE
    - Redirect effectiveness (0.3): redirects happen and are effective
    - Retry specificity (0.2): retries carry specific hints, not generic
    - Admission discipline (0.2): records are admitted before advance
    """
    score = 0.0

    # Phase discipline (0.3)
    phase_score = 0.3
    if m.phase_sequence:
        # Penalty for skipping ANALYZE and going straight to EXECUTE
        seq_str = "→".join(m.phase_sequence)
        if "OBSERVE→EXECUTE" in seq_str or "DECIDE→EXECUTE" in seq_str:
            phase_score -= 0.15
        # Bonus for complete phase coverage
        covered = set(m.phase_sequence)
        if "OBSERVE" in covered and "ANALYZE" in covered:
            phase_score = min(phase_score + 0.05, 0.3)
    score += max(0.0, phase_score)

    # Redirect effectiveness (0.3)
    if m.redirect_count > 0:
        score += 0.3 * m.effective_redirect_rate
    else:
        # No redirects needed (agent was good enough) — neutral, give partial credit
        score += 0.15

    # Retry specificity (0.2)
    if m.retry_count > 0:
        specificity = 1.0 - m.generic_retry_ratio
        score += 0.2 * specificity
    else:
        # No retries needed — neutral
        score += 0.1

    # Admission discipline (0.2)
    if m.admitted_record_count > 0:
        score += 0.2
    elif m.total_steps > 0:
        # Steps happened but no records admitted — poor discipline
        score += 0.05

    return round(min(1.0, score), 3)


# ── Utilities ───────────────────────────────────────────────────────────────

def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file, returning empty list on any error."""
    result: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    result.append(json.loads(line))
    except Exception:
        pass
    return result


def format_report(m: GovernanceMetrics) -> str:
    """Format a human-readable governance report."""
    lines = [
        "=== Governance Report (L1) ===",
        f"Total steps: {m.total_steps}",
        f"Phase sequence: {' → '.join(m.phase_sequence)}",
        f"Steps per phase: {m.steps_per_phase}",
        "",
        f"Phase advances: {m.phase_advance_count}",
        f"Phase budget exceeded: {m.phase_budget_exceeded_count}",
        f"Tolerated advances: {m.tolerated_advance_count}",
        "",
        f"Redirects: {m.redirect_count}",
        f"  Effective: {m.effective_redirect_count} ({m.effective_redirect_rate:.0%})",
        f"Retries: {m.retry_count}",
        f"  Specific: {m.specific_retry_count}",
        f"  Generic: {m.generic_retry_count} (ratio: {m.generic_retry_ratio:.0%})",
        "",
        f"Admitted records: {m.admitted_record_count}",
        "",
        f"Governance score: {m.governance_score:.3f}",
    ]
    if m.redirects:
        lines.append("")
        lines.append("Redirect details:")
        for r in m.redirects:
            lines.append(f"  Step {r['step']}: {r['from']} → {r['to']} ({r['reason']})")
            if r.get("repair_hints"):
                lines.append(f"    hints: {r['repair_hints']}")
    if m.retries:
        lines.append("")
        lines.append("Retry details:")
        for r in m.retries:
            lines.append(f"  Step {r['step']}: {r['phase']} ({r['reason']})")
            if r.get("repair_hints"):
                lines.append(f"    hints: {r['repair_hints']}")
    return "\n".join(lines)
