"""
behavior_benchmark.py — Extract behavioral signals from traj files.

Produces per-instance behavioral profile and cross-batch comparison.
Designed for P1 behavior-level benchmarking (not outcome).

Usage:
    python scripts/behavior_benchmark.py [--batches P1.1,P1.2,P1.4,P1.3p,P1.3p-fix]
"""

from __future__ import annotations

import json
import os
import sys
import glob
import argparse
from collections import defaultdict
from dataclasses import dataclass, field


# ── Batch definitions ────────────────────────────────────────────────────────

BATCH_CONFIGS = {
    "P1.1": {
        "dir": "/tmp/p11-trajs",
        "flat": True,  # {inst}.traj.json, {inst}.attempt2.traj.json
    },
    "P1.2": {
        "dir": "/tmp/p12-trajs",
        "flat": True,
    },
    "P1.4": {
        "dir": "/tmp/p14-trajs",
        "flat": False,  # attempt_{1,2}/{inst}.traj.json
    },
    "P1.3p": {
        "dir": "/tmp/p13p-trajs",
        "flat": False,
    },
    "P1.3p-fix": {
        "dir": "/tmp/p13p-fix-trajs",
        "flat": False,
    },
}

INSTANCES_10 = [
    "django__django-10097", "django__django-10999", "django__django-11087",
    "django__django-11141", "django__django-11276",
    "django__django-11095", "django__django-11099", "django__django-11119",
    "django__django-11163", "django__django-11292",
]

RESOLVED_SET = {"django__django-11095", "django__django-11099", "django__django-11119",
                "django__django-11163", "django__django-11292"}


# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class TrajBehavior:
    instance_id: str = ""
    batch: str = ""
    attempt: int = 1
    total_messages: int = 0
    total_steps: int = 0

    # Phase sequence
    phase_sequence: list[str] = field(default_factory=list)
    phase_counts: dict[str, int] = field(default_factory=dict)

    # Governance signals
    qj_count: int = 0
    qj_results: list[dict] = field(default_factory=list)  # [{target_status, direction}]

    # Redirect signals (P1.3')
    wrong_direction_redirects: int = 0
    routing_enforcement_count: int = 0
    cross_phase_redirects: list[str] = field(default_factory=list)  # ["EXECUTE->ANALYZE"]

    # Admission signals
    admission_count: int = 0
    retryable_count: int = 0
    rejected_count: int = 0
    admitted_count: int = 0

    # Gate signals
    gate_blocks: int = 0
    phase_budget_exhausted: bool = False

    # Patch signals
    has_patch: bool = False
    patch_iterations: int = 0  # number of EXECUTE records submitted

    # Phase checkpoint reminders
    checkpoint_reminders: int = 0

    # Outcome
    resolved: bool = False
    exit_reason: str = ""


def extract_behavior(traj_path: str, instance_id: str, batch: str, attempt: int) -> TrajBehavior:
    """Extract behavioral signals from a single traj file."""
    with open(traj_path) as f:
        traj = json.load(f)

    b = TrajBehavior(instance_id=instance_id, batch=batch, attempt=attempt)
    messages = traj.get("messages", [])
    b.total_messages = len(messages)

    # Track current phase from prompt injections
    current_phase = ""
    phase_sequence = []

    for i, msg in enumerate(messages):
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                str(c.get("text", "")) if isinstance(c, dict) else str(c)
                for c in content
            )
        role = msg.get("role", "")

        # ── Phase prompt injections ──
        if role == "user" and content.startswith("[Phase: "):
            phase = content.split("]")[0].replace("[Phase: ", "").strip()
            if phase != current_phase:
                current_phase = phase
                phase_sequence.append(phase)

        # ── Quick Judge ──
        if "QUICK_CHECK" in content:
            b.qj_count += 1
            qj_entry = {}
            if "TARGET PASSED" in content:
                qj_entry["target_status"] = "passed"
            elif "TARGET ERROR" in content:
                qj_entry["target_status"] = "error"
            elif "TARGET FAILED" in content:
                qj_entry["target_status"] = "failed"
            elif "TARGET MISSING" in content:
                qj_entry["target_status"] = "missing"
            # direction
            for d in ["improved", "regressed", "unchanged", "inconclusive", "first_signal"]:
                if d in content.lower():
                    qj_entry["direction"] = d
                    break
            b.qj_results.append(qj_entry)

        # ── P1.3' redirect ──
        if "WRONG PATCH DIRECTION" in content:
            b.wrong_direction_redirects += 1
            b.cross_phase_redirects.append("EXECUTE->ANALYZE")

        # ── Routing enforcement ──
        if "ROUTING ENFORCEMENT" in content:
            b.routing_enforcement_count += 1

        # ── Admission signals ──
        if "Phase record for" in content and "received" in content:
            b.admission_count += 1
        if "RETRYABLE" in content and role == "user":
            b.retryable_count += 1
        if "REJECTED" in content and role == "user" and "admission" in content.lower():
            b.rejected_count += 1
        if "ADMITTED" in content or "immediate-admission" in content:
            b.admitted_count += 1

        # ── Gate blocks ──
        if "gate_rejected" in content or "gate_blocked" in content:
            b.gate_blocks += 1

        # ── Phase budget ──
        if "phase_budget_exhausted" in content:
            b.phase_budget_exhausted = True

        # ── Checkpoint reminders ──
        if "PHASE CHECKPOINT" in content:
            b.checkpoint_reminders += 1

        # ── Exit reason ──
        if content.startswith("exit:") or "StopExecution" in content:
            b.exit_reason = content[:100]

    b.phase_sequence = phase_sequence
    b.phase_counts = {}
    for p in phase_sequence:
        b.phase_counts[p] = b.phase_counts.get(p, 0) + 1

    # Count EXECUTE submissions as patch iterations
    b.patch_iterations = b.phase_counts.get("EXECUTE", 0)

    # Check for patch in submission
    info = traj.get("info", {})
    submission = info.get("submission", "")
    b.has_patch = bool(submission and len(submission) > 10)
    b.resolved = instance_id in RESOLVED_SET

    # Step count from messages (assistant messages = steps)
    b.total_steps = sum(1 for m in messages if m.get("role") == "assistant")

    return b


# ── Traj discovery ───────────────────────────────────────────────────────────

def discover_trajs(batch_name: str) -> list[tuple[str, str, int]]:
    """Return list of (traj_path, instance_id, attempt) for a batch."""
    cfg = BATCH_CONFIGS.get(batch_name)
    if not cfg:
        return []

    results = []
    base = cfg["dir"]

    if cfg["flat"]:
        # Flat: {inst}.traj.json and {inst}.attempt2.traj.json
        for inst in INSTANCES_10:
            p1 = os.path.join(base, f"{inst}.traj.json")
            if os.path.exists(p1):
                results.append((p1, inst, 1))
            p2 = os.path.join(base, f"{inst}.attempt2.traj.json")
            if os.path.exists(p2):
                results.append((p2, inst, 2))
    else:
        # Structured: attempt_{1,2}/{inst}[/{inst}].traj.json
        for attempt in [1, 2]:
            attempt_dir = os.path.join(base, f"attempt_{attempt}")
            if not os.path.isdir(attempt_dir):
                continue
            for inst in INSTANCES_10:
                # Try both patterns
                candidates = [
                    os.path.join(attempt_dir, f"{inst}.traj.json"),
                    os.path.join(attempt_dir, inst, f"{inst}.traj.json"),
                ]
                for p in candidates:
                    if os.path.exists(p):
                        results.append((p, inst, attempt))
                        break

    return results


# ── Reporting ────────────────────────────────────────────────────────────────

def report_batch(batch_name: str, behaviors: list[TrajBehavior]) -> dict:
    """Aggregate batch-level behavioral metrics."""
    if not behaviors:
        return {}

    # Only use attempt 1 for cross-batch comparison
    a1 = [b for b in behaviors if b.attempt == 1]

    stats = {
        "batch": batch_name,
        "instances": len(a1),
        "total_steps_mean": sum(b.total_steps for b in a1) / max(len(a1), 1),
        "total_messages_mean": sum(b.total_messages for b in a1) / max(len(a1), 1),

        # QJ
        "qj_total": sum(b.qj_count for b in a1),
        "qj_per_instance": sum(b.qj_count for b in a1) / max(len(a1), 1),
        "qj_passed": sum(1 for b in a1 for q in b.qj_results if q.get("target_status") == "passed"),
        "qj_error": sum(1 for b in a1 for q in b.qj_results if q.get("target_status") == "error"),
        "qj_failed": sum(1 for b in a1 for q in b.qj_results if q.get("target_status") == "failed"),

        # Redirects
        "wrong_direction_redirects": sum(b.wrong_direction_redirects for b in a1),
        "routing_enforcement_loops": sum(b.routing_enforcement_count for b in a1),
        "cross_phase_redirects": sum(len(b.cross_phase_redirects) for b in a1),

        # Admission
        "admission_total": sum(b.admission_count for b in a1),
        "retryable_total": sum(b.retryable_count for b in a1),

        # Phase diversity
        "unique_phase_sequences": len(set(
            "->".join(b.phase_sequence) for b in a1
        )),
        "avg_phase_transitions": sum(len(b.phase_sequence) for b in a1) / max(len(a1), 1),
        "instances_reaching_judge": sum(1 for b in a1 if "JUDGE" in b.phase_counts),

        # Budget
        "phase_budget_exhausted": sum(1 for b in a1 if b.phase_budget_exhausted),

        # Patch
        "has_patch": sum(1 for b in a1 if b.has_patch),
    }
    return stats


def print_comparison(all_stats: list[dict]):
    """Print cross-batch comparison table."""
    if not all_stats:
        return

    print("\n" + "=" * 100)
    print("BEHAVIORAL BENCHMARK — Cross-Batch Comparison (attempt 1 only)")
    print("=" * 100)

    metrics = [
        ("instances", "Instances", "d"),
        ("total_steps_mean", "Avg Steps", ".0f"),
        ("total_messages_mean", "Avg Messages", ".0f"),
        ("qj_total", "QJ Total", "d"),
        ("qj_passed", "QJ Passed", "d"),
        ("qj_error", "QJ Error", "d"),
        ("wrong_direction_redirects", "Wrong-Dir Redirects", "d"),
        ("routing_enforcement_loops", "Routing Enforcement", "d"),
        ("cross_phase_redirects", "Cross-Phase Redirects", "d"),
        ("admission_total", "Admissions", "d"),
        ("retryable_total", "Retryable", "d"),
        ("unique_phase_sequences", "Unique Phase Seqs", "d"),
        ("avg_phase_transitions", "Avg Phase Transitions", ".1f"),
        ("instances_reaching_judge", "Reached JUDGE", "d"),
        ("phase_budget_exhausted", "Budget Exhausted", "d"),
        ("has_patch", "Has Patch", "d"),
    ]

    # Header
    batch_names = [s["batch"] for s in all_stats]
    header = f"{'Metric':<25}" + "".join(f"{bn:>15}" for bn in batch_names)
    print(header)
    print("-" * len(header))

    for key, label, fmt in metrics:
        row = f"{label:<25}"
        for s in all_stats:
            val = s.get(key, 0)
            row += f"{val:>15{fmt}}"
        print(row)


def print_instance_detail(batch_name: str, behaviors: list[TrajBehavior]):
    """Print per-instance detail for a batch."""
    a1 = sorted([b for b in behaviors if b.attempt == 1], key=lambda b: b.instance_id)

    print(f"\n{'=' * 100}")
    print(f"INSTANCE DETAIL — {batch_name} (attempt 1)")
    print(f"{'=' * 100}")

    header = (f"{'Instance':<35} {'Steps':>5} {'QJ':>3} {'QJ-P':>4} {'QJ-E':>4} "
              f"{'Redir':>5} {'RE':>3} {'Retry':>5} {'Phase Seq':<30} {'Resolved':>8}")
    print(header)
    print("-" * len(header))

    for b in a1:
        qj_passed = sum(1 for q in b.qj_results if q.get("target_status") == "passed")
        qj_error = sum(1 for q in b.qj_results if q.get("target_status") in ("error", "failed"))
        seq = "->".join(b.phase_sequence[:8])
        if len(b.phase_sequence) > 8:
            seq += "->..."

        print(f"{b.instance_id:<35} {b.total_steps:>5} {b.qj_count:>3} {qj_passed:>4} "
              f"{qj_error:>4} {b.wrong_direction_redirects:>5} {b.routing_enforcement_count:>3} "
              f"{b.retryable_count:>5} {seq:<30} {'YES' if b.resolved else 'no':>8}")


def print_wrong_path_recovery(all_behaviors: dict[str, list[TrajBehavior]]):
    """Print wrong-path recovery analysis across batches."""
    print(f"\n{'=' * 100}")
    print("WRONG-PATH RECOVERY ANALYSIS")
    print("=" * 100)
    print()
    print("For unresolved instances: did the system detect wrong direction and attempt recovery?")
    print()

    unresolved = [i for i in INSTANCES_10 if i not in RESOLVED_SET]

    header = f"{'Instance':<35}" + "".join(f"{bn:>15}" for bn in all_behaviors.keys())
    print(header)
    print("-" * len(header))

    for inst in sorted(unresolved):
        row = f"{inst:<35}"
        for batch_name, behaviors in all_behaviors.items():
            a1 = [b for b in behaviors if b.attempt == 1 and b.instance_id == inst]
            if not a1:
                row += f"{'N/A':>15}"
                continue
            b = a1[0]
            if b.wrong_direction_redirects > 0:
                label = f"REDIR({b.wrong_direction_redirects})"
            elif b.qj_count > 0 and any(q.get("target_status") != "passed" for q in b.qj_results):
                label = f"QJ-fail({b.qj_count})"
            elif b.qj_count > 0:
                label = f"QJ-pass({b.qj_count})"
            else:
                label = "no-signal"
            row += f"{label:>15}"
        print(row)

    # Recovery rate
    print()
    for batch_name, behaviors in all_behaviors.items():
        a1_unresolved = [b for b in behaviors if b.attempt == 1 and b.instance_id in unresolved]
        recovered = sum(1 for b in a1_unresolved if b.wrong_direction_redirects > 0)
        total = len(a1_unresolved)
        rate = recovered / total * 100 if total else 0
        print(f"  {batch_name}: wrong-path recovery attempts = {recovered}/{total} ({rate:.0f}%)")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Behavioral benchmark across batches")
    parser.add_argument("--batches", default="P1.1,P1.2,P1.4,P1.3p,P1.3p-fix",
                        help="Comma-separated batch names")
    args = parser.parse_args()

    batch_names = [b.strip() for b in args.batches.split(",")]

    all_stats = []
    all_behaviors: dict[str, list[TrajBehavior]] = {}

    for batch_name in batch_names:
        trajs = discover_trajs(batch_name)
        if not trajs:
            print(f"[WARN] No trajs found for {batch_name}, skipping")
            continue

        behaviors = []
        for path, inst, attempt in trajs:
            try:
                b = extract_behavior(path, inst, batch_name, attempt)
                behaviors.append(b)
            except Exception as e:
                print(f"[ERROR] {batch_name}/{inst}/attempt{attempt}: {e}")

        all_behaviors[batch_name] = behaviors
        stats = report_batch(batch_name, behaviors)
        all_stats.append(stats)

        # Per-instance detail
        print_instance_detail(batch_name, behaviors)

    # Cross-batch comparison
    print_comparison(all_stats)

    # Wrong-path recovery
    print_wrong_path_recovery(all_behaviors)


if __name__ == "__main__":
    main()
