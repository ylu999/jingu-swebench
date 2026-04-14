#!/usr/bin/env python3
"""
analyze_cognition_effectiveness.py — p207-P10: Cognition System Effectiveness Telemetry

Measures how effective the cognition system (phase records, principal inference,
admission gate, verify gate) is across a batch of SWE-bench runs.

Data sources:
  1. traj.json files — jingu_body["phase_records"], jingu_body["principal_inference"]
  2. Log lines (CloudWatch or local file) — [phase_record], [principal_gate],
     [principal_inference], [verify_gate] signal lines

Usage:
  # From traj files (primary)
  python scripts/analyze_cognition_effectiveness.py --results results/batch-p25

  # From log file (supplementary gate-fire data)
  python scripts/analyze_cognition_effectiveness.py --results results/batch-p25 --log-file logs/batch.txt

  # From CloudWatch task
  python scripts/analyze_cognition_effectiveness.py --results results/batch-p25 --task-id <ecs-task-id>

  # JSON output only (for piping)
  python scripts/analyze_cognition_effectiveness.py --results results/batch-p25 --json-only
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterator


# ── Constants ────────────────────────────────────────────────────────────────

ALL_PHASES = ["UNDERSTAND", "OBSERVE", "ANALYZE", "DECIDE", "DESIGN", "EXECUTE", "JUDGE"]

ALL_PRINCIPALS = [
    "causal_grounding",
    "evidence_linkage",
    "minimal_change",
    "ontology_alignment",
    "phase_boundary_discipline",
    "action_grounding",
    "option_comparison",
    "constraint_satisfaction",
    "result_verification",
    "uncertainty_honesty",
]


# ── Traj file iteration (reused pattern from analyze_principal_metrics.py) ───

def _iter_traj_files(results_dir: str) -> Iterator[tuple[str, int, Path]]:
    """
    Walk results_dir for traj.json files.

    Supports two directory layouts:
      1. results/attempt_N/instance_id/instance_id.traj.json  (standard)
      2. results/instance_id/instance_id.traj.json            (flat)

    Yields: (instance_id, attempt_id, traj_file_path)
    """
    base = Path(results_dir)
    if not base.exists():
        print(f"[cognition] WARNING: results dir does not exist: {results_dir}", file=sys.stderr)
        return

    # Layout 1: attempt_N subdirs
    found_attempt_dirs = list(base.glob("attempt_*"))
    if found_attempt_dirs:
        for attempt_dir in sorted(found_attempt_dirs):
            if not attempt_dir.is_dir():
                continue
            try:
                attempt_id = int(attempt_dir.name.split("_")[-1])
            except ValueError:
                attempt_id = 0
            for instance_dir in sorted(attempt_dir.iterdir()):
                if not instance_dir.is_dir():
                    continue
                instance_id = instance_dir.name
                traj_file = instance_dir / f"{instance_id}.traj.json"
                if traj_file.exists():
                    yield instance_id, attempt_id, traj_file
        return

    # Layout 2: flat — instance dirs directly under results_dir
    for instance_dir in sorted(base.iterdir()):
        if not instance_dir.is_dir() or instance_dir.name.startswith("."):
            continue
        instance_id = instance_dir.name
        traj_file = instance_dir / f"{instance_id}.traj.json"
        if traj_file.exists():
            yield instance_id, 1, traj_file


# ── Phase Coverage Analysis ──────────────────────────────────────────────────

def analyze_phase_coverage(
    traj_data: dict[str, list[dict]],
) -> dict:
    """
    Per-phase structure coverage: which phases have structured output.

    traj_data: {instance_id: [phase_record_dict, ...]}

    Returns:
      {
        phase: {
          "instances_entered": int,
          "instances_with_structure": int,  # has principals or evidence_refs
          "coverage_rate": float,
        }
      }
    """
    phase_stats: dict[str, dict] = {}
    for phase in ALL_PHASES:
        entered = 0
        structured = 0
        for _iid, records in traj_data.items():
            phase_records = [r for r in records if (r.get("phase") or "").upper() == phase]
            if phase_records:
                entered += 1
                # "structured" = has at least one principal or evidence_ref
                has_structure = any(
                    r.get("principals") or r.get("evidence_refs")
                    for r in phase_records
                )
                if has_structure:
                    structured += 1
        phase_stats[phase] = {
            "instances_entered": entered,
            "instances_with_structure": structured,
            "coverage_rate": round(structured / entered, 4) if entered > 0 else 0.0,
        }
    return phase_stats


# ── Principal Metrics ────────────────────────────────────────────────────────

def analyze_principal_metrics(
    pi_data: dict[str, list[dict]],
) -> dict:
    """
    Per-principal metrics from principal_inference entries.

    pi_data: {instance_id: [principal_inference_entry, ...]}

    Returns per-principal:
      declaration_rate, inference_rate, fake_rate, bypass_rate
    """
    total_entries = sum(len(entries) for entries in pi_data.values())
    if total_entries == 0:
        return {p: _empty_principal_metric() for p in ALL_PRINCIPALS}

    metrics: dict[str, dict] = {}
    for principal in ALL_PRINCIPALS:
        declared_count = 0
        inferred_count = 0
        fake_count = 0

        for _iid, entries in pi_data.items():
            for entry in entries:
                declared = [d.lower() for d in (entry.get("declared") or [])]
                inferred = entry.get("inferred") or {}
                present = [p.lower() for p in (inferred.get("present") or [])]
                diff = entry.get("diff") or {}
                fake = [f.lower() for f in (diff.get("fake") or [])]

                if principal in declared:
                    declared_count += 1
                if principal in present:
                    inferred_count += 1
                if principal in fake:
                    fake_count += 1

        metrics[principal] = {
            "declared_count": declared_count,
            "declaration_rate": round(declared_count / total_entries, 4) if total_entries > 0 else 0.0,
            "inferred_count": inferred_count,
            "inference_rate": round(inferred_count / total_entries, 4) if total_entries > 0 else 0.0,
            "fake_count": fake_count,
            "fake_rate": round(fake_count / max(declared_count, 1), 4),
        }

    return metrics


def _empty_principal_metric() -> dict:
    return {
        "declared_count": 0,
        "declaration_rate": 0.0,
        "inferred_count": 0,
        "inference_rate": 0.0,
        "fake_count": 0,
        "fake_rate": 0.0,
    }


# ── Gate Fire Metrics (from log lines) ──────────────────────────────────────

# Log line patterns:
#   [principal_gate] eval_phase=... admission=ADMITTED/RETRYABLE/REJECTED reasons=...
#   [principal_inference] FAKE_RETRYABLE: ...
#   [principal_gate] ESCALATE_CONTRACT_BUG: ... → contract_bypass ADMITTED
#   [verify_gate] prerequisite=pass/fail(...) controlled_verify=run/skipped

RE_PRINCIPAL_GATE = re.compile(
    r"\[principal_gate\].*?admission=(ADMITTED|RETRYABLE|REJECTED)"
)
RE_PRINCIPAL_GATE_ESCALATE = re.compile(
    r"\[principal_gate\] ESCALATE_CONTRACT_BUG:"
)
RE_FAKE_RETRYABLE = re.compile(
    r"\[principal_inference\] FAKE_RETRYABLE:"
)
RE_FAKE_LOOP_ESCALATE = re.compile(
    r"\[principal_inference\] ESCALATE_FAKE_LOOP:"
)
RE_FAKE_LOOP_SELECTIVE_BYPASS = re.compile(
    r"\[principal_inference\] FAKE_LOOP_SELECTIVE_BYPASS:"
)
RE_VERIFY_GATE = re.compile(
    r"\[verify_gate\] prerequisite=(pass|fail\(\w+\))\s+controlled_verify=(run|skipped)"
)
RE_PHASE_RECORD = re.compile(
    r"\[phase_record\] eval_phase=(\w+)"
    r".*?subtype=(\S+)"
    r".*?principals=\[([^\]]*)\]"
)
RE_COGNITION_CHECK = re.compile(
    r"\[cognition\]|cognition_result="
)


def parse_log_lines(lines: list[str]) -> dict:
    """
    Parse signal lines from CloudWatch/local log file.

    Returns gate fire metrics:
      {
        "principal_gate": {"ADMITTED": N, "RETRYABLE": N, "REJECTED": N, "ESCALATE_CONTRACT_BUG": N},
        "principal_inference": {"FAKE_RETRYABLE": N, "ESCALATE_FAKE_LOOP": N, "SELECTIVE_BYPASS": N},
        "verify_gate": {"pass_run": N, "pass_skipped": N, "fail_run": N, "fail_skipped": N},
        "phase_record_count": N,
        "cognition_check_count": N,
      }
    """
    gate: dict[str, int] = defaultdict(int)
    inference: dict[str, int] = defaultdict(int)
    verify: dict[str, int] = defaultdict(int)
    phase_record_count = 0
    cognition_check_count = 0

    for line in lines:
        # principal_gate admission
        m = RE_PRINCIPAL_GATE.search(line)
        if m:
            gate[m.group(1)] += 1

        # ESCALATE_CONTRACT_BUG (separate from admission — it overrides RETRYABLE to ADMITTED)
        if RE_PRINCIPAL_GATE_ESCALATE.search(line):
            gate["ESCALATE_CONTRACT_BUG"] += 1

        # FAKE_RETRYABLE
        if RE_FAKE_RETRYABLE.search(line):
            inference["FAKE_RETRYABLE"] += 1

        # ESCALATE_FAKE_LOOP
        if RE_FAKE_LOOP_ESCALATE.search(line):
            inference["ESCALATE_FAKE_LOOP"] += 1

        # p207-P9: SELECTIVE_BYPASS
        if RE_FAKE_LOOP_SELECTIVE_BYPASS.search(line):
            inference["SELECTIVE_BYPASS"] += 1

        # verify_gate
        m = RE_VERIFY_GATE.search(line)
        if m:
            prereq = "pass" if m.group(1) == "pass" else "fail"
            cv = m.group(2)
            verify[f"{prereq}_{cv}"] += 1

        # phase_record telemetry
        if RE_PHASE_RECORD.search(line):
            phase_record_count += 1

        # cognition_check
        if RE_COGNITION_CHECK.search(line):
            cognition_check_count += 1

    return {
        "principal_gate": dict(gate),
        "principal_inference": dict(inference),
        "verify_gate": dict(verify),
        "phase_record_count": phase_record_count,
        "cognition_check_count": cognition_check_count,
    }


# ── Log fetching (from CloudWatch) ──────────────────────────────────────────

def fetch_logs(task_id: str) -> list[str]:
    """Fetch logs from CloudWatch for ECS task."""
    import boto3
    logs_client = boto3.client("logs", region_name="us-west-2")
    log_group = "/ecs/jingu-swebench"
    log_stream = f"runner/runner/{task_id}"

    print(f"[cognition] fetching logs: {log_group}/{log_stream}", file=sys.stderr, flush=True)
    lines: list[str] = []
    next_token = None
    while True:
        kwargs = dict(
            logGroupName=log_group,
            logStreamName=log_stream,
            startFromHead=True,
            limit=10000,
        )
        if next_token:
            kwargs["nextToken"] = next_token
        resp = logs_client.get_log_events(**kwargs)
        for ev in resp["events"]:
            lines.append(ev["message"])
        new_token = resp.get("nextForwardToken")
        if new_token == next_token:
            break
        next_token = new_token

    print(f"[cognition] fetched {len(lines)} log lines", file=sys.stderr, flush=True)
    return lines


# ── Per-Instance Summary ─────────────────────────────────────────────────────

def build_instance_summary(
    traj_data: dict[str, list[dict]],
    pi_data: dict[str, list[dict]],
) -> list[dict]:
    """
    Per-instance summary: which phases produced structure, which principals failed.
    """
    all_instances = sorted(set(list(traj_data.keys()) + list(pi_data.keys())))
    summaries = []

    for iid in all_instances:
        records = traj_data.get(iid, [])
        pi_entries = pi_data.get(iid, [])

        phases_with_structure = []
        phases_without_structure = []
        for phase in ALL_PHASES:
            phase_recs = [r for r in records if (r.get("phase") or "").upper() == phase]
            if not phase_recs:
                continue
            has_structure = any(
                r.get("principals") or r.get("evidence_refs")
                for r in phase_recs
            )
            if has_structure:
                phases_with_structure.append(phase)
            else:
                phases_without_structure.append(phase)

        # Principal issues from inference
        missing_required: list[str] = []
        fake_principals: list[str] = []
        for entry in pi_entries:
            diff = entry.get("diff") or {}
            for p in (diff.get("missing_required") or []):
                if p not in missing_required:
                    missing_required.append(p)
            for p in (diff.get("fake") or []):
                if p not in fake_principals:
                    fake_principals.append(p)

        summaries.append({
            "instance_id": iid,
            "phases_entered": len(phases_with_structure) + len(phases_without_structure),
            "phases_structured": len(phases_with_structure),
            "phases_without_structure": phases_without_structure,
            "principal_entries": len(pi_entries),
            "missing_required": missing_required,
            "fake_principals": fake_principals,
        })

    return summaries


# ── Extract from traj files ──────────────────────────────────────────────────

def extract_from_trajs(results_dir: str) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """
    Extract phase_records and principal_inference from traj.json files.

    Returns:
      (traj_data, pi_data) where:
        traj_data = {instance_id: [phase_record_dict, ...]}
        pi_data   = {instance_id: [principal_inference_entry, ...]}
    """
    traj_data: dict[str, list[dict]] = defaultdict(list)
    pi_data: dict[str, list[dict]] = defaultdict(list)

    for instance_id, attempt_id, traj_file in _iter_traj_files(results_dir):
        try:
            traj = json.loads(traj_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"[cognition] WARNING: failed to read {traj_file}: {e}", file=sys.stderr)
            continue

        jingu_body = traj.get("jingu_body") or {}

        # Phase records
        phase_records = jingu_body.get("phase_records") or []
        if isinstance(phase_records, list):
            for pr in phase_records:
                if isinstance(pr, dict):
                    traj_data[instance_id].append(pr)

        # Principal inference
        pi_list = jingu_body.get("principal_inference") or []
        if isinstance(pi_list, list):
            for entry in pi_list:
                if isinstance(entry, dict):
                    pi_data[instance_id].append(entry)

    return dict(traj_data), dict(pi_data)


# ── Human-readable report ────────────────────────────────────────────────────

def print_report(
    phase_coverage: dict,
    principal_metrics: dict,
    gate_metrics: dict | None,
    instance_summaries: list[dict],
    total_instances: int,
) -> None:
    """Print human-readable summary table."""
    print("\n" + "=" * 72)
    print("COGNITION SYSTEM EFFECTIVENESS REPORT")
    print("=" * 72)
    print(f"Total instances: {total_instances}")

    # Phase coverage
    print(f"\n{'─' * 72}")
    print("PHASE COVERAGE")
    print(f"{'Phase':<12} {'Entered':>8} {'Structured':>11} {'Coverage':>9}")
    print(f"{'─' * 12} {'─' * 8} {'─' * 11} {'─' * 9}")
    for phase in ALL_PHASES:
        stats = phase_coverage.get(phase, {})
        entered = stats.get("instances_entered", 0)
        structured = stats.get("instances_with_structure", 0)
        rate = stats.get("coverage_rate", 0.0)
        bar = "#" * int(rate * 20)
        print(f"{phase:<12} {entered:>8} {structured:>11} {rate:>8.1%}  {bar}")

    # Principal metrics
    print(f"\n{'─' * 72}")
    print("PRINCIPAL METRICS")
    print(f"{'Principal':<30} {'Decl':>5} {'Inf':>5} {'Fake':>5} {'FakeRate':>9}")
    print(f"{'─' * 30} {'─' * 5} {'─' * 5} {'─' * 5} {'─' * 9}")
    for principal in ALL_PRINCIPALS:
        m = principal_metrics.get(principal, _empty_principal_metric())
        print(
            f"{principal:<30} {m['declared_count']:>5} {m['inferred_count']:>5}"
            f" {m['fake_count']:>5} {m['fake_rate']:>8.1%}"
        )

    # Gate fire metrics (if available)
    if gate_metrics:
        print(f"\n{'─' * 72}")
        print("GATE FIRE METRICS (from log lines)")

        pg = gate_metrics.get("principal_gate", {})
        if pg:
            total_pg = sum(pg.values())
            print(f"\n  Principal Gate (total fires: {total_pg}):")
            for status in ["ADMITTED", "RETRYABLE", "REJECTED", "ESCALATE_CONTRACT_BUG"]:
                count = pg.get(status, 0)
                rate = count / total_pg if total_pg > 0 else 0.0
                print(f"    {status:<28} {count:>5}  ({rate:.1%})")

        pi = gate_metrics.get("principal_inference", {})
        if pi:
            print(f"\n  Principal Inference:")
            for key, count in sorted(pi.items()):
                print(f"    {key:<28} {count:>5}")

        vg = gate_metrics.get("verify_gate", {})
        if vg:
            total_vg = sum(vg.values())
            print(f"\n  Verify Gate (total fires: {total_vg}):")
            for key in ["pass_run", "pass_skipped", "fail_run", "fail_skipped"]:
                count = vg.get(key, 0)
                rate = count / total_vg if total_vg > 0 else 0.0
                print(f"    {key:<28} {count:>5}  ({rate:.1%})")

        print(f"\n  Phase record lines:    {gate_metrics.get('phase_record_count', 0)}")
        print(f"  Cognition check lines: {gate_metrics.get('cognition_check_count', 0)}")

    # Per-instance summary (top issues)
    problem_instances = [
        s for s in instance_summaries
        if s["missing_required"] or s["fake_principals"] or s["phases_without_structure"]
    ]
    if problem_instances:
        print(f"\n{'─' * 72}")
        print(f"PER-INSTANCE ISSUES ({len(problem_instances)} instances with issues)")
        print(f"{'Instance':<35} {'Phases':>6} {'Struct':>6} {'MissReq':>8} {'Fake':>5}")
        print(f"{'─' * 35} {'─' * 6} {'─' * 6} {'─' * 8} {'─' * 5}")
        for s in problem_instances[:30]:  # cap display at 30
            iid_short = s["instance_id"][-25:] if len(s["instance_id"]) > 25 else s["instance_id"]
            print(
                f"{iid_short:<35} {s['phases_entered']:>6} {s['phases_structured']:>6}"
                f" {len(s['missing_required']):>8} {len(s['fake_principals']):>5}"
            )
            if s["missing_required"]:
                print(f"  {'':35} missing: {', '.join(s['missing_required'])}")
            if s["fake_principals"]:
                print(f"  {'':35} fake: {', '.join(s['fake_principals'])}")

    print("\n" + "=" * 72)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "p207-P10: Cognition System Effectiveness Analysis.\n"
            "\n"
            "Analyzes phase structure coverage, principal declaration/inference metrics,\n"
            "gate fire rates, and per-instance failure summaries from SWE-bench batch runs.\n"
            "\n"
            "Primary data: traj.json files (--results dir).\n"
            "Supplementary: log lines from CloudWatch (--task-id) or local file (--log-file)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--results",
        required=True,
        help="Results dir containing attempt_N/instance_id/instance_id.traj.json files",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Local log file with [phase_record]/[principal_gate]/[verify_gate] lines",
    )
    parser.add_argument(
        "--task-id",
        default=None,
        help="ECS task ID — fetches logs from CloudWatch for gate fire metrics",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Output only JSON (no human-readable table)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write JSON output to this file path (in addition to stdout)",
    )
    args = parser.parse_args()

    # ── Extract from traj files ──────────────────────────────────────────
    print(f"[cognition] Extracting from: {args.results}", file=sys.stderr, flush=True)
    traj_data, pi_data = extract_from_trajs(args.results)
    total_instances = len(set(list(traj_data.keys()) + list(pi_data.keys())))
    print(
        f"[cognition] Found {total_instances} instances, "
        f"{sum(len(v) for v in traj_data.values())} phase_records, "
        f"{sum(len(v) for v in pi_data.values())} principal_inference entries",
        file=sys.stderr, flush=True,
    )

    if total_instances == 0:
        print(
            "[cognition] WARNING: 0 instances found. Possible causes:\n"
            "  - No traj.json files in results dir\n"
            "  - traj.json files missing jingu_body.phase_records / principal_inference\n"
            "  - Wrong directory layout (expected attempt_N/instance_id/ or flat instance_id/)",
            file=sys.stderr,
        )

    # ── Analyze ──────────────────────────────────────────────────────────
    phase_coverage = analyze_phase_coverage(traj_data)
    principal_metrics = analyze_principal_metrics(pi_data)
    instance_summaries = build_instance_summary(traj_data, pi_data)

    # ── Gate fire metrics (optional — from logs) ─────────────────────────
    gate_metrics = None
    if args.task_id:
        log_lines = fetch_logs(args.task_id)
        gate_metrics = parse_log_lines(log_lines)
    elif args.log_file:
        log_lines = Path(args.log_file).read_text().splitlines()
        gate_metrics = parse_log_lines(log_lines)

    # ── Build output ─────────────────────────────────────────────────────
    result = {
        "total_instances": total_instances,
        "phase_coverage": phase_coverage,
        "principal_metrics": principal_metrics,
        "instance_summaries": instance_summaries,
    }
    if gate_metrics:
        result["gate_metrics"] = gate_metrics

    # ── Output ───────────────────────────────────────────────────────────
    if not args.json_only:
        print_report(
            phase_coverage,
            principal_metrics,
            gate_metrics,
            instance_summaries,
            total_instances,
        )

    json_output = json.dumps(result, indent=2, default=str)

    if args.json_only:
        print(json_output)
    else:
        print(f"\n[cognition] JSON output ({len(json_output)} chars):")
        print(json_output)

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json_output)
        print(f"\n[cognition] Written to: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
