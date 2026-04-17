"""
analyze_principal_metrics.py — p196: Principal System Validation

Extracts ExtractedRecord objects from traj.json files (via jingu_body["principal_inference"]),
computes 7 metrics, generates 4 reports, and detects anomalies.

Usage:
  python scripts/analyze_principal_metrics.py --results results/p196-smoke --output results/p196-smoke
  python scripts/analyze_principal_metrics.py --results results/p196-smoke --mock

Constraint: DO NOT tune thresholds or add rules after seeing data (methodology-antipatterns.md rule 2).
"""

from __future__ import annotations

import csv
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


# ── ExtractedRecord (fixed schema — do not modify) ────────────────────────────

@dataclass
class ExtractedRecord:
    instance_id: str
    attempt_id: int
    subtype: str
    present: list[str]           # inferred present
    absent: list[str]            # inferred absent
    missing_required: list[str]
    missing_expected: list[str]
    fake: list[str]
    declared: list[str]          # what agent declared
    verdict: str                 # "pass" | "reject" | "warn"
    signals: dict                # per-principal signals from p195
    explanation: dict            # per-principal explanations from p195
    rule_triggered: list[str]    # which rules fired


# SST: derive from canonical source
from canonical_symbols import ALL_PRINCIPALS  # type: ignore[assignment]


# ── Extractor ─────────────────────────────────────────────────────────────────

def _verdict_from_diff(diff: dict) -> str:
    """
    Derive verdict from diff fields (deterministic, always present in principal_inference).

    Priority:
      fake non-empty       → reject
      missing_required non-empty → reject
      missing_expected non-empty → warn
      otherwise            → pass
    """
    if diff.get("fake"):
        return "reject"
    if diff.get("missing_required"):
        return "reject"
    if diff.get("missing_expected"):
        return "warn"
    return "pass"


def _extract_signals_and_explanation(details: dict) -> tuple[dict, dict]:
    """
    Extract per-principal signals and explanation from details dict.

    details schema (from run_with_jingu_gate.py p195 telemetry):
      { principal: { "score": float, "signals": list[str], "explanation": str } }
    """
    signals: dict = {}
    explanation: dict = {}
    for principal, result in (details or {}).items():
        if isinstance(result, dict):
            signals[principal] = result.get("signals", [])
            explanation[principal] = result.get("explanation", "")
    return signals, explanation


def _extract_rules_triggered(details: dict, diff: dict) -> list[str]:
    """
    Derive which inference rules actually fired (score >= threshold).

    A rule fires when its principal appears in inferred.present.
    Rule IDs are derived from principal names (1:1 mapping from p195 registry).
    """
    # principals in present = rules that fired (score >= threshold)
    # We reconstruct from details: if score >= 0.7 (default threshold), it fired
    triggered = []
    for principal, result in (details or {}).items():
        if isinstance(result, dict):
            score = result.get("score", 0.0)
            # Use 0.7 as default threshold (matches p195 InferenceRule default)
            if score >= 0.7:
                triggered.append(f"rule_{principal}")
    return triggered


def _iter_traj_files(results_dir: str) -> Iterator[tuple[str, int, Path]]:
    """
    Walk results_dir for traj.json files.

    Supports two directory layouts:
      1. results/attempt_N/instance_id/instance_id.traj.json  (standard)
      2. results/instance_id/instance_id.traj.json            (flat)
    """
    base = Path(results_dir)
    if not base.exists():
        return

    # Layout 1: attempt_N subdirs
    for attempt_dir in sorted(base.glob("attempt_*")):
        if not attempt_dir.is_dir():
            continue
        # Parse attempt number from dir name
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

    # Layout 2: flat — instance dirs directly under results_dir
    # (only if no attempt_N dirs found above)
    found_attempt_dirs = list(base.glob("attempt_*"))
    if not found_attempt_dirs:
        for instance_dir in sorted(base.iterdir()):
            if not instance_dir.is_dir() or instance_dir.name.startswith("."):
                continue
            instance_id = instance_dir.name
            traj_file = instance_dir / f"{instance_id}.traj.json"
            if traj_file.exists():
                yield instance_id, 1, traj_file


def extract_records(results_dir: str) -> list[ExtractedRecord]:
    """
    Walk results_dir and extract ExtractedRecord from every traj.json that has
    jingu_body["principal_inference"].

    Invariant: only extracts from real traj data (p196 constraint: no synthetic extraction).
    One record per (instance_id, attempt_id, phase_record_index).
    """
    records: list[ExtractedRecord] = []

    for instance_id, attempt_id, traj_file in _iter_traj_files(results_dir):
        try:
            traj = json.loads(traj_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        jingu_body = traj.get("jingu_body") or {}
        pi_list = jingu_body.get("principal_inference")
        if not pi_list or not isinstance(pi_list, list):
            # No principal_inference in this traj — skip (pre-p195 run)
            continue

        for pi_entry in pi_list:
            if not isinstance(pi_entry, dict):
                continue

            subtype = pi_entry.get("subtype", "")
            declared = list(pi_entry.get("declared") or [])
            inferred = pi_entry.get("inferred") or {}
            present = list(inferred.get("present") or [])
            absent = list(inferred.get("absent") or [])
            details = pi_entry.get("details") or {}
            diff = pi_entry.get("diff") or {}

            missing_required = list(diff.get("missing_required") or [])
            missing_expected = list(diff.get("missing_expected") or [])
            fake = list(diff.get("fake") or [])

            verdict = _verdict_from_diff(diff)
            signals, explanation = _extract_signals_and_explanation(details)
            rule_triggered = _extract_rules_triggered(details, diff)

            records.append(ExtractedRecord(
                instance_id=instance_id,
                attempt_id=attempt_id,
                subtype=subtype,
                present=present,
                absent=absent,
                missing_required=missing_required,
                missing_expected=missing_expected,
                fake=fake,
                declared=declared,
                verdict=verdict,
                signals=signals,
                explanation=explanation,
                rule_triggered=rule_triggered,
            ))

    return records


# ── 7 Metrics ──────────────────────────────────────────────────────────────────

def compute_metrics(records: list[ExtractedRecord]) -> dict:
    """
    Compute all 7 metrics from ExtractedRecord list.

    Metric definitions (pre-registered, do NOT change after seeing data):
      1. presence_rate per principal  = present count / total
      2. entropy per principal        = binary entropy H(p)
      3. missing_required_rate        = records with any missing_required / total
      4. fake_rate                    = total fake / total declared
      5. missing_expected_rate per principal = missing count / (present+missing) count
      6. reject_rate                  = verdict=="reject" / total
      7. subtype_stats                = per-subtype breakdown
    """
    total = len(records)
    if total == 0:
        return {}

    # ── Metric 1: presence_rate per principal ─────────────────────────────────
    presence_rate: dict[str, float] = {}
    for p in ALL_PRINCIPALS:
        count = sum(1 for r in records if p in r.present)
        presence_rate[p] = count / total

    # ── Metric 2: entropy per principal (binary H(p) = -p log2 p - (1-p) log2(1-p)) ──
    entropy: dict[str, float] = {}
    for p, rate in presence_rate.items():
        if rate <= 0.0 or rate >= 1.0:
            entropy[p] = 0.0
        else:
            entropy[p] = -rate * math.log2(rate) - (1.0 - rate) * math.log2(1.0 - rate)

    # ── Metric 3: missing_required_rate ──────────────────────────────────────
    missing_required_rate = sum(1 for r in records if r.missing_required) / total

    # ── Metric 4: fake_rate = total_fake / total_declared ────────────────────
    total_fake = sum(len(r.fake) for r in records)
    total_declared = sum(len(r.declared) for r in records)
    fake_rate = total_fake / total_declared if total_declared > 0 else 0.0

    # ── Metric 5: missing_expected_rate per principal ─────────────────────────
    # = records where p in missing_expected / records where p was expected (present OR missing_expected)
    missing_expected_rate: dict[str, float] = {}
    for p in ALL_PRINCIPALS:
        expected_count = sum(
            1 for r in records if p in r.present or p in r.missing_expected
        )
        missing_count = sum(1 for r in records if p in r.missing_expected)
        missing_expected_rate[p] = (
            missing_count / expected_count if expected_count > 0 else 0.0
        )

    # ── Metric 6: reject_rate ─────────────────────────────────────────────────
    reject_rate = sum(1 for r in records if r.verdict == "reject") / total

    # ── Metric 7: subtype_stats ───────────────────────────────────────────────
    subtypes = set(r.subtype for r in records)
    subtype_stats: dict[str, dict] = {}
    for subtype in sorted(subtypes):
        sub = [r for r in records if r.subtype == subtype]
        n = len(sub)
        sub_declared = max(sum(len(r.declared) for r in sub), 1)
        subtype_stats[subtype] = {
            "count": n,
            "missing_required_rate": sum(1 for r in sub if r.missing_required) / n,
            "fake_rate": sum(len(r.fake) for r in sub) / sub_declared,
            "reject_rate": sum(1 for r in sub if r.verdict == "reject") / n,
        }

    return {
        "total_records": total,
        "presence_rate": presence_rate,
        "entropy": entropy,
        "missing_required_rate": missing_required_rate,
        "fake_rate": fake_rate,
        "missing_expected_rate": missing_expected_rate,
        "reject_rate": reject_rate,
        "subtype_stats": subtype_stats,
    }


# ── 5 Anomaly Detectors ────────────────────────────────────────────────────────

def detect_anomalies(metrics: dict) -> list[dict]:
    """
    Detect 5 anomaly types (fixed — do NOT add types after seeing data).

    1. NO_SIGNAL_LOW   — presence_rate < 5%
    2. NO_SIGNAL_HIGH  — presence_rate > 95%
    3. GATE_NO_EFFECT  — missing_required_rate=0 AND reject_rate=0
    4. NO_FAKE_DETECTED — fake_rate=0
    5. RULE_NEVER_FIRES — any rule with trigger_rate=0 (from rule_activity in reports)
       (Note: rule_activity is computed in generate_reports; anomaly 5 is post-generate.
        This function detects from metrics; anomaly 5 requires rule_triggers passed in.)
    """
    if not metrics:
        return []

    anomalies: list[dict] = []

    # Anomaly 1+2: NO_SIGNAL_LOW / NO_SIGNAL_HIGH — presence_rate near 0 or 1
    for p, rate in metrics.get("presence_rate", {}).items():
        if rate < 0.05:
            anomalies.append({
                "type": "NO_SIGNAL_LOW",
                "principal": p,
                "value": round(rate, 4),
                "description": f"presence_rate={rate:.3f} < 5% — principal never inferred present",
            })
        elif rate > 0.95:
            anomalies.append({
                "type": "NO_SIGNAL_HIGH",
                "principal": p,
                "value": round(rate, 4),
                "description": f"presence_rate={rate:.3f} > 95% — principal always inferred present (no discrimination)",
            })

    # Anomaly 3: GATE_NO_EFFECT
    mrr = metrics.get("missing_required_rate", 0.0)
    rr = metrics.get("reject_rate", 0.0)
    if mrr == 0.0 and rr == 0.0:
        anomalies.append({
            "type": "GATE_NO_EFFECT",
            "missing_required_rate": mrr,
            "reject_rate": rr,
            "description": "gate has no behavioral effect — missing_required=0 AND reject=0",
        })

    # Anomaly 4: NO_FAKE_DETECTED
    fake_rate = metrics.get("fake_rate", 0.0)
    if fake_rate == 0.0:
        anomalies.append({
            "type": "NO_FAKE_DETECTED",
            "value": 0.0,
            "description": "fake_rate=0 — inference never detected a fake principal declaration",
        })

    # Anomaly 5: OVER_STRICT
    if mrr > 0.5:
        anomalies.append({
            "type": "OVER_STRICT",
            "missing_required_rate": round(mrr, 4),
            "description": f"missing_required_rate={mrr:.3f} > 50% — gate too strict, blocks majority of attempts",
        })

    return anomalies


def detect_rule_anomalies(records: list[ExtractedRecord], metrics: dict) -> list[dict]:
    """
    Detect RULE_NEVER_FIRES anomalies from rule_triggered data.
    Separate from detect_anomalies() because it needs records, not just metrics.
    """
    if not records:
        return []

    total = len(records)
    rule_triggers: dict[str, int] = {}
    for r in records:
        for rule in r.rule_triggered:
            rule_triggers[rule] = rule_triggers.get(rule, 0) + 1

    anomalies: list[dict] = []
    for rule, count in rule_triggers.items():
        rate = count / total
        if rate == 0.0:
            anomalies.append({
                "type": "RULE_NEVER_FIRES",
                "rule_id": rule,
                "trigger_count": count,
                "trigger_rate": 0.0,
                "description": f"rule {rule} never fired — may be misconfigured or subtype mismatch",
            })
    return anomalies


# ── 4 Reports ─────────────────────────────────────────────────────────────────

def generate_reports(
    records: list[ExtractedRecord],
    metrics: dict,
    output_dir: str,
) -> None:
    """
    Generate 4 report files in output_dir/reports/.

    Files:
      principal_summary.csv  — per-principal presence_rate, entropy, missing_expected_rate
      subtype_summary.csv    — per-subtype count, missing_required_rate, fake_rate, reject_rate
      top_issues.txt         — top missing_expected principals + overall stats
      rule_activity.csv      — per-rule trigger_count and trigger_rate
    """
    if not metrics:
        return

    reports_dir = os.path.join(output_dir, "reports")
    os.makedirs(reports_dir, exist_ok=True)

    total = metrics.get("total_records", len(records))
    if total == 0:
        total = 1  # avoid division by zero in report rendering

    # ── Report 1: principal_summary.csv ───────────────────────────────────────
    with open(os.path.join(reports_dir, "principal_summary.csv"), "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "principal", "presence_rate", "entropy", "missing_expected_rate"
        ])
        writer.writeheader()
        for p in ALL_PRINCIPALS:
            writer.writerow({
                "principal": p,
                "presence_rate": round(metrics["presence_rate"].get(p, 0.0), 4),
                "entropy": round(metrics["entropy"].get(p, 0.0), 4),
                "missing_expected_rate": round(
                    metrics["missing_expected_rate"].get(p, 0.0), 4
                ),
            })

    # ── Report 2: subtype_summary.csv ─────────────────────────────────────────
    with open(os.path.join(reports_dir, "subtype_summary.csv"), "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "subtype", "count", "missing_required_rate", "fake_rate", "reject_rate"
        ])
        writer.writeheader()
        for subtype, stats in sorted(metrics.get("subtype_stats", {}).items()):
            writer.writerow({
                "subtype": subtype,
                "count": stats["count"],
                "missing_required_rate": round(stats["missing_required_rate"], 4),
                "fake_rate": round(stats["fake_rate"], 4),
                "reject_rate": round(stats["reject_rate"], 4),
            })

    # ── Report 3: top_issues.txt ──────────────────────────────────────────────
    me_sorted = sorted(
        metrics["missing_expected_rate"].items(),
        key=lambda x: x[1],
        reverse=True,
    )
    with open(os.path.join(reports_dir, "top_issues.txt"), "w") as f:
        f.write("Top missing_expected principals:\n")
        for i, (p, rate) in enumerate(me_sorted[:5], 1):
            f.write(f"  {i}. {p}: {rate:.3f}\n")
        f.write(f"\nOverall stats:\n")
        f.write(f"  fake_rate:             {metrics['fake_rate']:.4f}\n")
        f.write(f"  reject_rate:           {metrics['reject_rate']:.4f}\n")
        f.write(f"  missing_required_rate: {metrics['missing_required_rate']:.4f}\n")
        f.write(f"  total_records:         {metrics['total_records']}\n")

    # ── Report 4: rule_activity.csv ───────────────────────────────────────────
    rule_triggers: dict[str, int] = {}
    for r in records:
        for rule in r.rule_triggered:
            rule_triggers[rule] = rule_triggers.get(rule, 0) + 1

    n = len(records) if records else 1
    with open(os.path.join(reports_dir, "rule_activity.csv"), "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["rule_id", "trigger_count", "trigger_rate"])
        writer.writeheader()
        for rule, count in sorted(
            rule_triggers.items(), key=lambda x: x[1], reverse=True
        ):
            writer.writerow({
                "rule_id": rule,
                "trigger_count": count,
                "trigger_rate": round(count / n, 4),
            })
        if not rule_triggers:
            # Write empty placeholder so file always exists
            writer.writerow({
                "rule_id": "(no rules triggered)",
                "trigger_count": 0,
                "trigger_rate": 0.0,
            })


# ── Mock data generator ────────────────────────────────────────────────────────

def generate_mock_records(n: int = 20) -> list[ExtractedRecord]:
    """
    Generate synthetic records for extractor validation (used when no real data exists).
    Pattern: realistic distribution — some fakes, some missing, some pass.
    """
    import random
    random.seed(42)  # deterministic for test reproducibility

    subtypes = ["analysis.root_cause", "execution.code_patch", "judge.verification"]
    # SST: derive principal-subtype mapping from contract_registry
    try:
        from contract_registry import get_contract_by_subtype
        principals_by_subtype = {
            st: list(get_contract_by_subtype(st).required_principals)
            for st in ["analysis.root_cause", "execution.code_patch", "judge.verification"]
        }
    except Exception:
        principals_by_subtype = {st: [] for st in ["analysis.root_cause", "execution.code_patch", "judge.verification"]}

    records = []
    for i in range(n):
        subtype = subtypes[i % len(subtypes)]
        phase_principals = principals_by_subtype[subtype]

        # Simulate inference: 60% present, 40% absent
        present = [p for p in phase_principals if random.random() > 0.4]
        absent = [p for p in phase_principals if p not in present]

        # Simulate declared: agent declares some subset, sometimes wrong
        # 20% chance of fake (declare something not inferred)
        declared = list(present)
        if random.random() < 0.2 and absent:
            fake_principal = random.choice(absent)
            declared.append(fake_principal)
            fake = [fake_principal]
        else:
            fake = []

        # 25% chance of missing_required
        if random.random() < 0.25 and phase_principals:
            required = phase_principals[0]
            missing_required = [required] if required not in declared else []
        else:
            missing_required = []

        # missing_expected
        expected = phase_principals[1:2] if len(phase_principals) > 1 else []
        missing_expected = [p for p in expected if p not in declared]

        # verdict from diff
        diff = {"fake": fake, "missing_required": missing_required, "missing_expected": missing_expected}
        verdict = _verdict_from_diff(diff)

        # signals
        signals = {
            p: ["has_evidence_refs", "has_causal_language"] if p in present else ["no_evidence_refs"]
            for p in phase_principals
        }
        explanation = {
            p: "Mock inferred" if p in present else "Mock absent"
            for p in phase_principals
        }
        rule_triggered = [f"rule_{p}" for p in present]

        records.append(ExtractedRecord(
            instance_id=f"django__django-{11000 + i}",
            attempt_id=1,
            subtype=subtype,
            present=present,
            absent=absent,
            missing_required=missing_required,
            missing_expected=missing_expected,
            fake=fake,
            declared=declared,
            verdict=verdict,
            signals=signals,
            explanation=explanation,
            rule_triggered=rule_triggered,
        ))

    return records


# ── Pre-registered thresholds (DO NOT CHANGE after seeing data) ───────────────
# Registered before batch run per task-p196.md Step 2.

_THRESHOLDS = {
    "fake_rate_hard_fail":             0.0,   # fake_rate=0 means inference not working
    "missing_required_rate_hard_fail": 0.0,   # missing_required_rate=0 means gate has no effect
    "reject_rate_hard_fail":           0.0,   # reject_rate=0 means gate has no effect
}


def check_thresholds(metrics: dict) -> list[dict]:
    """
    Check pre-registered thresholds. Returns threshold failures.
    MUST NOT be called with modified thresholds.
    """
    if not metrics:
        return []

    failures = []
    if metrics.get("fake_rate", 0.0) <= _THRESHOLDS["fake_rate_hard_fail"]:
        failures.append({
            "metric": "fake_rate",
            "actual": metrics["fake_rate"],
            "threshold": _THRESHOLDS["fake_rate_hard_fail"],
            "status": "HARD_FAIL",
            "reason": "fake_rate=0 means inference is not detecting fake principal declarations",
        })
    if metrics.get("missing_required_rate", 0.0) <= _THRESHOLDS["missing_required_rate_hard_fail"]:
        failures.append({
            "metric": "missing_required_rate",
            "actual": metrics["missing_required_rate"],
            "threshold": _THRESHOLDS["missing_required_rate_hard_fail"],
            "status": "HARD_FAIL",
            "reason": "missing_required_rate=0 means required principal enforcement never triggered",
        })
    if metrics.get("reject_rate", 0.0) <= _THRESHOLDS["reject_rate_hard_fail"]:
        failures.append({
            "metric": "reject_rate",
            "actual": metrics["reject_rate"],
            "threshold": _THRESHOLDS["reject_rate_hard_fail"],
            "status": "HARD_FAIL",
            "reason": "reject_rate=0 means gate has no behavioral effect",
        })
    return failures


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "p196: Principal System Validation — extract metrics from SWE-bench traj results.\n"
            "\n"
            "Reads jingu_body['principal_inference'] from traj.json files under --results dir.\n"
            "Computes 7 metrics, generates 4 report files, detects anomalies.\n"
            "\n"
            "Output structure:\n"
            "  <output>/raw/p196_records.jsonl\n"
            "  <output>/reports/principal_summary.csv\n"
            "  <output>/reports/subtype_summary.csv\n"
            "  <output>/reports/top_issues.txt\n"
            "  <output>/reports/rule_activity.csv\n"
            "  <output>/reports/anomalies.json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--results",
        required=False,
        default=None,
        help="Results dir containing attempt_N/instance_id/instance_id.traj.json files",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output dir for reports (default: same as --results)",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use synthetic mock records instead of real traj data (for extractor validation)",
    )
    args = parser.parse_args()

    if args.mock:
        print("[p196] Using mock data for extractor validation (no real traj available)")
        records = generate_mock_records(n=20)
        output_dir = args.output or args.results or "results/p196-validation"
    elif args.results:
        print(f"[p196] Extracting records from: {args.results}")
        records = extract_records(args.results)
        output_dir = args.output or args.results
    else:
        parser.error("Either --results or --mock is required")
        return

    print(f"[p196] Extracted {len(records)} records")

    if not records:
        print("[p196] WARNING: 0 records extracted. Possible causes:")
        print("         - No traj.json files found in results dir")
        print("         - Traj files do not have jingu_body.principal_inference")
        print("           (runs must be from p195+ pipeline — commit 8500a47 or later)")
        print("       Use --mock to validate extractor logic without real data.")

    # Compute metrics
    metrics = compute_metrics(records)
    if metrics:
        print(f"[p196] Metrics computed: {metrics['total_records']} records")
    else:
        print("[p196] No metrics (0 records)")
        metrics = {
            "total_records": 0,
            "presence_rate": {p: 0.0 for p in ALL_PRINCIPALS},
            "entropy": {p: 0.0 for p in ALL_PRINCIPALS},
            "missing_required_rate": 0.0,
            "fake_rate": 0.0,
            "missing_expected_rate": {p: 0.0 for p in ALL_PRINCIPALS},
            "reject_rate": 0.0,
            "subtype_stats": {},
        }

    # Detect anomalies
    anomalies = detect_anomalies(metrics)
    anomalies += detect_rule_anomalies(records, metrics)

    # Check thresholds
    threshold_failures = check_thresholds(metrics)

    # Generate reports
    os.makedirs(output_dir, exist_ok=True)
    generate_reports(records, metrics, output_dir)

    # Save raw records
    raw_dir = os.path.join(output_dir, "raw")
    os.makedirs(raw_dir, exist_ok=True)
    with open(os.path.join(raw_dir, "p196_records.jsonl"), "w") as f:
        for r in records:
            f.write(json.dumps(r.__dict__) + "\n")

    # Save anomalies
    reports_dir = os.path.join(output_dir, "reports")
    os.makedirs(reports_dir, exist_ok=True)
    all_anomalies = anomalies + [
        {**tf, "category": "threshold_failure"}
        for tf in threshold_failures
    ]
    with open(os.path.join(reports_dir, "anomalies.json"), "w") as f:
        json.dump(all_anomalies, f, indent=2)

    # Summary output
    print(f"\n[p196] Results written to: {output_dir}")
    print(f"[p196] Anomalies detected: {len(all_anomalies)}")
    print(f"\n[p196] Metrics summary:")
    print(json.dumps(metrics, indent=2, default=str))
    if all_anomalies:
        print(f"\n[p196] Anomalies ({len(all_anomalies)}):")
        print(json.dumps(all_anomalies, indent=2))

    if threshold_failures:
        print(f"\n[p196] THRESHOLD FAILURES ({len(threshold_failures)}) — system not producing signal:")
        for tf in threshold_failures:
            print(f"  HARD_FAIL: {tf['metric']}={tf['actual']} (threshold: >{tf['threshold']})")
            print(f"    reason: {tf['reason']}")


if __name__ == "__main__":
    main()
