#!/usr/bin/env python3
"""Compare NPRG A/B experiment results.

Usage:
  python scripts/nprg_compare.py --run-a nprg-runA-baseline-20260416 --run-b nprg-runB-experiment-20260416

Metrics (per user spec — NOT resolved rate):
  1. repeated_patch_rate: count(L2_same_files detected) / total attempt-2 instances
  2. phase_shift_rate: count(ADJUST after gate) / count(gate triggered)
  3. patch_novelty_rate: count(attempt2 files != attempt1 files) / total attempt-2 instances
"""

import argparse
import hashlib
import json
import sys

import boto3

S3_BUCKET = "jingu-swebench-results"
REGION = "us-west-2"


def load_traj_pairs(batch: str) -> dict:
    """Load attempt 1 and 2 traj pairs for a batch. Returns {iid: {1: data, 2: data}}."""
    s3 = boto3.client("s3", region_name=REGION)
    paginator = s3.get_paginator("list_objects_v2")
    traj_keys = []
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=f"{batch}/attempt_"):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".traj.json"):
                traj_keys.append(obj["Key"])

    instances = {}
    for key in traj_keys:
        parts = key.split("/")
        attempt = int(parts[1].split("_")[1])
        iid = parts[2]
        if iid.endswith(".traj.json"):
            continue  # skip duplicate naming pattern

        try:
            obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
            traj = json.loads(obj["Body"].read())
            jb = traj.get("jingu_body", {})
            patch = jb.get("patch", "") or ""
            files_written = sorted(jb.get("files_written", []))
            patch_hash = hashlib.md5(patch.encode()).hexdigest()[:8] if patch else "empty"

            if iid not in instances:
                instances[iid] = {}
            instances[iid][attempt] = {
                "patch_hash": patch_hash,
                "files": files_written,
                "failure_type": jb.get("failure_type"),
                "failure_mode": jb.get("failure_mode"),
                "cv_resolved": (jb.get("controlled_verify") or {}).get("eval_resolved"),
                "nprg_detected": jb.get("nprg_detected"),
                "no_progress_repeat": jb.get("no_progress_repeat"),
                "patch_lines": len(patch.splitlines()) if patch else 0,
            }
        except Exception as e:
            print(f"  warn: {key}: {e}", file=sys.stderr)

    return instances


def compute_metrics(instances: dict, label: str) -> dict:
    """Compute the 3 NPRG metrics from traj pairs."""
    total_a2 = 0
    l1_detected = 0
    l2_detected = 0
    nprg_triggered = 0
    nprg_adjust = 0
    files_changed = 0
    resolved_a1 = 0
    resolved_a2 = 0

    details = []

    for iid in sorted(instances):
        a1 = instances[iid].get(1)
        a2 = instances[iid].get(2)

        if a1 and a1.get("cv_resolved"):
            resolved_a1 += 1

        if not a2:
            # Only attempt 1 — resolved on first try or no retry
            details.append(f"  {iid}: a1_only cv={a1.get('cv_resolved') if a1 else '?'}")
            continue

        total_a2 += 1
        if a2.get("cv_resolved"):
            resolved_a2 += 1

        same_hash = (a1["patch_hash"] == a2["patch_hash"] and a1["patch_hash"] != "empty")
        same_files = (a1["files"] == a2["files"] and a1["files"])

        if same_hash:
            l1_detected += 1
        elif same_files:
            l2_detected += 1

        if not same_files and not same_hash:
            files_changed += 1

        nprg_det = a2.get("nprg_detected")
        nprg_act = a2.get("no_progress_repeat")
        if nprg_act:
            nprg_triggered += 1
            if "L2" in str(nprg_act):
                nprg_adjust += 1

        flag = ""
        if same_hash:
            flag = " <<< L1_IDENTICAL"
        elif same_files:
            flag = " <<< L2_SAME_FILES"
        elif a1["files"] and a2["files"]:
            flag = " (files changed)"

        details.append(
            f"  {iid}: "
            f"a1={a1['files']} a2={a2['files']} "
            f"cv1={a1.get('cv_resolved')} cv2={a2.get('cv_resolved')} "
            f"nprg_det={nprg_det} nprg_act={nprg_act}"
            f"{flag}"
        )

    metrics = {
        "label": label,
        "total_instances": len(instances),
        "total_with_attempt2": total_a2,
        "l1_identical_count": l1_detected,
        "l2_same_files_count": l2_detected,
        "repeated_patch_rate": (l1_detected + l2_detected) / total_a2 if total_a2 else 0,
        "patch_novelty_rate": files_changed / total_a2 if total_a2 else 0,
        "nprg_triggered": nprg_triggered,
        "nprg_adjust": nprg_adjust,
        "phase_shift_rate": nprg_adjust / nprg_triggered if nprg_triggered else 0,
        "resolved_a1": resolved_a1,
        "resolved_a2": resolved_a2,
    }
    return metrics, details


def print_comparison(ma: dict, mb: dict, da: list, db: list):
    """Print side-by-side comparison."""
    print("=" * 70)
    print("NPRG A/B EXPERIMENT RESULTS")
    print("=" * 70)

    print(f"\n{'Metric':<35} {'Run A (OFF)':<18} {'Run B (ON)':<18} {'Delta'}")
    print("-" * 70)

    for key in ["total_instances", "total_with_attempt2"]:
        print(f"{key:<35} {ma[key]:<18} {mb[key]:<18}")

    print()
    for key, fmt in [
        ("repeated_patch_rate", ".1%"),
        ("patch_novelty_rate", ".1%"),
        ("phase_shift_rate", ".1%"),
    ]:
        va, vb = ma[key], mb[key]
        delta = vb - va
        sign = "+" if delta > 0 else ""
        print(f"{key:<35} {format(va, fmt):<18} {format(vb, fmt):<18} {sign}{format(delta, fmt)}")

    print()
    for key in ["l1_identical_count", "l2_same_files_count", "nprg_triggered", "nprg_adjust",
                 "resolved_a1", "resolved_a2"]:
        va, vb = ma[key], mb[key]
        delta = vb - va
        sign = "+" if delta > 0 else ""
        print(f"{key:<35} {va:<18} {vb:<18} {sign}{delta}")

    print(f"\n{'=' * 70}")
    print("EXPECTED if gate works:")
    print("  repeated_patch_rate: A > B (gate forces direction change)")
    print("  patch_novelty_rate: A < B (gate forces new files)")
    print("  phase_shift_rate: A = 0, B > 0 (gate triggers ADJUST)")
    print(f"{'=' * 70}")

    print(f"\n--- Run A details ({ma['label']}) ---")
    for d in da:
        print(d)
    print(f"\n--- Run B details ({mb['label']}) ---")
    for d in db:
        print(d)


def main():
    parser = argparse.ArgumentParser(description="Compare NPRG A/B experiment")
    parser.add_argument("--run-a", required=True, help="Batch name for Run A (baseline, NPRG OFF)")
    parser.add_argument("--run-b", required=True, help="Batch name for Run B (experiment, NPRG ON)")
    args = parser.parse_args()

    print(f"Loading Run A: {args.run_a}...")
    inst_a = load_traj_pairs(args.run_a)
    print(f"  {len(inst_a)} instances loaded")

    print(f"Loading Run B: {args.run_b}...")
    inst_b = load_traj_pairs(args.run_b)
    print(f"  {len(inst_b)} instances loaded")

    ma, da = compute_metrics(inst_a, args.run_a)
    mb, db = compute_metrics(inst_b, args.run_b)

    print_comparison(ma, mb, da, db)


if __name__ == "__main__":
    main()
