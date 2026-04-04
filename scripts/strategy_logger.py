"""
strategy_logger.py — p178 strategy learning v1: JSONL log for attempt outcomes.

Records one StrategyLogEntry per attempt. Data is consumed by:
  - aggregate_strategies.py (offline: computes win rates per bucket)
  - retry_controller.py (online: ε-greedy hint selection from strategy_table.json)

Signal scope (p177 verified, p178 constraint from user):
  - failure_class: from classify_failure() — deterministic, always reliable
  - control_action: from RetryPlan — determines retry path
  - steps_since_last_signal: from compute_steps_since_last_signal() — p164 runner layer
  - enforced_violation_codes: ENV_LEAKAGE_HARDCODE_PATH | PLAN_NO_FEEDBACK_LOOP only
  - declared-only principals: logged for observability, NOT used in bucket key

Bucket key: (failure_class, enforced_viol_key)
  enforced_viol_key = "|".join(sorted(enforced_violation_codes)) or ""
  This keeps the bucket space small and signal-clean.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class StrategyLogEntry:
    timestamp: str
    instance_id: str
    attempt_id: int                 # attempt that generated the hint (1-based)
    failure_class: str              # v1: from retry_controller.classify_failure()
    failure_class_v2: str           # p179 v2: from classify_failure_v2() using tests_delta
    control_action: str             # CONTINUE | ADJUST | STOP_NO_SIGNAL | STOP_FAIL
    steps_since_last_signal: int    # p164 runner layer
    enforced_violation_codes: list[str]   # only ENV_LEAKAGE_HARDCODE_PATH / PLAN_NO_FEEDBACK_LOOP
    hint_used: str                  # next_attempt_prompt[:300] — the actual hint applied
    # ── p178.1: retry-level reward (primary learning signal) ─────────────────
    next_attempt_admitted: bool     # did attempt N+1 get admitted by the gate?
    next_attempt_has_patch: bool    # did attempt N+1 produce any patch at all?
    # ── instance-level outcome (auxiliary, not used as primary reward) ────────
    instance_final_admitted: bool   # did any attempt get admitted for this instance?
    # legacy: kept for backward compat with existing code, not used in bucketing
    outcome: str                    # solved | unsolved (derived from instance_final_admitted)
    # ── p179: signal repair fields (primary reward channel) ──────────────────
    tests_delta: Optional[int]      # tests_passed_after - tests_passed_before (None if baseline unknown)
    tests_passed_before: int        # passing test count from prev attempt (-1 if unknown)
    tests_passed_after: int         # passing test count from this attempt (-1 if unknown)
    files_written_paths: list[str]  # actual file paths modified (from patch + tool calls)
    # Logged for observability only — NOT used in bucket key
    principals_declared: list[str] = field(default_factory=list)


def make_bucket_key(failure_class: str, enforced_violation_codes: list[str]) -> str:
    """
    Deterministic bucket key from (failure_class, enforced_violation_codes).

    Uses only verified, enforceable signals — not declared-only principals.
    Example: "no_effect_patch" or "exploration_loop|ENV_LEAKAGE_HARDCODE_PATH"
    """
    viol_key = "|".join(sorted(enforced_violation_codes))
    if viol_key:
        return f"{failure_class}|{viol_key}"
    return failure_class


def make_bucket_key_v2(failure_class_v2: str, enforced_violation_codes: list[str]) -> str:
    """
    p179 bucket key using signal-aware failure_class_v2.

    Buckets: no_patch_or_invalid | no_test_progress | positive_delta_unresolved | signal_missing
    Violation codes are still appended (same enforcement logic as v1).
    """
    viol_key = "|".join(sorted(enforced_violation_codes))
    if viol_key:
        return f"{failure_class_v2}|{viol_key}"
    return failure_class_v2


# ── I/O ───────────────────────────────────────────────────────────────────────

def log_strategy_entry(entry: StrategyLogEntry, log_path: str | Path) -> None:
    """Append one entry to the JSONL log file (atomic line append)."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(entry), ensure_ascii=False)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_strategy_log(log_path: str | Path) -> list[StrategyLogEntry]:
    """Load all entries from a JSONL log file. Returns [] if file missing."""
    log_path = Path(log_path)
    if not log_path.exists():
        return []
    entries = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                # p179 backward compat: fill missing new fields with defaults
                d.setdefault("tests_passed_before", -1)
                d.setdefault("tests_passed_after", -1)
                d.setdefault("files_written_paths", [])
                d.setdefault("failure_class_v2", "signal_missing")
                entries.append(StrategyLogEntry(**d))
            except (json.JSONDecodeError, TypeError):
                pass  # skip malformed lines
    return entries


# ── Factory ───────────────────────────────────────────────────────────────────

def make_entry(
    instance_id: str,
    attempt_id: int,
    failure_class: str,
    control_action: str,
    steps_since_last_signal: int,
    enforced_violation_codes: list[str],
    hint_used: str,
    # p178.1: retry-level reward fields (primary)
    next_attempt_admitted: bool = False,
    next_attempt_has_patch: bool = False,
    instance_final_admitted: bool = False,
    # legacy outcome field (derived from instance_final_admitted)
    outcome: str = "unsolved",
    # p179: signal repair fields
    tests_delta: Optional[int] = None,
    tests_passed_before: int = -1,
    tests_passed_after: int = -1,
    files_written_paths: Optional[list[str]] = None,
    failure_class_v2: str = "signal_missing",
    principals_declared: Optional[list[str]] = None,
) -> StrategyLogEntry:
    """Construct a StrategyLogEntry with a UTC timestamp."""
    # derive legacy outcome from instance_final_admitted if not explicitly set
    if outcome == "unsolved" and instance_final_admitted:
        outcome = "solved"
    return StrategyLogEntry(
        timestamp=datetime.now(timezone.utc).isoformat(),
        instance_id=instance_id,
        attempt_id=attempt_id,
        failure_class=failure_class,
        failure_class_v2=failure_class_v2,
        control_action=control_action,
        steps_since_last_signal=steps_since_last_signal,
        enforced_violation_codes=list(enforced_violation_codes),
        hint_used=hint_used[:300],
        next_attempt_admitted=next_attempt_admitted,
        next_attempt_has_patch=next_attempt_has_patch,
        instance_final_admitted=instance_final_admitted,
        outcome=outcome,
        tests_delta=tests_delta,
        tests_passed_before=tests_passed_before,
        tests_passed_after=tests_passed_after,
        files_written_paths=list(files_written_paths or []),
        principals_declared=list(principals_declared or []),
    )
