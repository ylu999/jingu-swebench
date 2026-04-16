#!/usr/bin/env python3
"""Jingu Effect Attribution Framework.

Classifies each instance into causal categories:
  STRONG_CAUSAL_WIN  — Jingu intervention → patch change → resolved
  WEAK_WIN           — resolved but small patch delta (possibly sampling)
  BEHAVIOR_CHANGE_NO_WIN — Jingu changed trajectory but task still unsolved
  NO_EFFECT          — agent stuck, identical patches across attempts

Usage:
  python scripts/attribution.py --run-a nprg-runA-v3-20260416 --run-b nprg-runB-v3-20260416
  python scripts/attribution.py --run-a batch-A --run-b batch-B --json  # machine-readable
"""

import argparse
import json
import sys

import boto3

S3_BUCKET = "jingu-swebench-results"
REGION = "us-west-2"


def patch_similarity(p1: str, p2: str) -> float:
    """Jaccard similarity on patch lines."""
    if not p1 or not p2:
        return 0.0
    lines1 = set(p1.splitlines())
    lines2 = set(p2.splitlines())
    if not lines1 or not lines2:
        return 0.0
    return len(lines1 & lines2) / len(lines1 | lines2)


def load_attempt(s3, batch: str, iid: str, att: int) -> dict | None:
    key = f"{batch}/attempt_{att}/{iid}/{iid}.traj.json"
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
        traj = json.loads(obj["Body"].read())
        jb = traj.get("jingu_body", {})
        cv = jb.get("controlled_verify", {})
        return {
            "patch": traj.get("info", {}).get("submission", "") or "",
            "patch_len": len(traj.get("info", {}).get("submission", "") or ""),
            "resolved": cv.get("eval_resolved", False),
            "files_written": sorted(jb.get("files_written", [])),
            "failure_type": jb.get("failure_type"),
            "failure_mode": jb.get("failure_mode"),
            "nprg_detected": jb.get("nprg_detected"),
            "nprg_action": jb.get("no_progress_repeat"),
        }
    except Exception:
        return None


def discover_instances(s3, batch: str) -> list[str]:
    """Find all instance IDs in a batch."""
    paginator = s3.get_paginator("list_objects_v2")
    iids = set()
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=f"{batch}/attempt_1/"):
        for obj in page.get("Contents", []):
            parts = obj["Key"].split("/")
            if len(parts) >= 3 and parts[2] and not parts[2].endswith(".json"):
                iids.add(parts[2])
    return sorted(iids)


def classify(b_a1, b_a2, sim_B: float) -> str:
    big_change = sim_B < 0.7
    if not b_a1["resolved"] and b_a2["resolved"]:
        return "STRONG_CAUSAL_WIN" if big_change else "WEAK_WIN"
    if not b_a1["resolved"] and not b_a2["resolved"]:
        return "BEHAVIOR_CHANGE_NO_WIN" if big_change else "NO_EFFECT"
    if b_a1["resolved"] and b_a2["resolved"]:
        return "ALREADY_SOLVED"
    return "REGRESSION"


def main():
    parser = argparse.ArgumentParser(description="Jingu Effect Attribution")
    parser.add_argument("--run-a", required=True, help="Batch name: Run A (baseline, NPRG OFF)")
    parser.add_argument("--run-b", required=True, help="Batch name: Run B (experiment, NPRG ON)")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of table")
    args = parser.parse_args()

    s3 = boto3.client("s3", region_name=REGION)

    # Discover instances from both runs
    iids_a = set(discover_instances(s3, args.run_a))
    iids_b = set(discover_instances(s3, args.run_b))
    instances = sorted(iids_a & iids_b)

    if not instances:
        print("No common instances found.", file=sys.stderr)
        sys.exit(1)

    results = []
    categories = {}

    for iid in instances:
        a_a1 = load_attempt(s3, args.run_a, iid, 1)
        a_a2 = load_attempt(s3, args.run_a, iid, 2)
        b_a1 = load_attempt(s3, args.run_b, iid, 1)
        b_a2 = load_attempt(s3, args.run_b, iid, 2)

        if not all([a_a1, a_a2, b_a1, b_a2]):
            continue

        sim_A = patch_similarity(a_a1["patch"], a_a2["patch"])
        sim_B = patch_similarity(b_a1["patch"], b_a2["patch"])
        sim_cross = patch_similarity(a_a2["patch"], b_a2["patch"])

        cat = classify(b_a1, b_a2, sim_B)
        categories.setdefault(cat, []).append(iid)

        entry = {
            "instance": iid,
            "category": cat,
            "run_a_sim": round(sim_A, 3),
            "run_b_sim": round(sim_B, 3),
            "cross_sim": round(sim_cross, 3),
            "run_a_a1_resolved": a_a1["resolved"],
            "run_a_a2_resolved": a_a2["resolved"],
            "run_b_a1_resolved": b_a1["resolved"],
            "run_b_a2_resolved": b_a2["resolved"],
            "run_b_a2_nprg": b_a2.get("nprg_action"),
            "run_a_patches": f"{a_a1['patch_len']}c / {a_a2['patch_len']}c",
            "run_b_patches": f"{b_a1['patch_len']}c / {b_a2['patch_len']}c",
        }
        results.append(entry)

    if args.json:
        print(json.dumps({"results": results, "summary": {k: len(v) for k, v in categories.items()}}, indent=2))
        return

    # Table output
    print("=" * 90)
    print("JINGU EFFECT ATTRIBUTION")
    print("=" * 90)

    for r in results:
        cat = r["category"]
        marker = {"STRONG_CAUSAL_WIN": "***", "WEAK_WIN": " * ",
                   "BEHAVIOR_CHANGE_NO_WIN": " ~ ", "NO_EFFECT": "   ",
                   "ALREADY_SOLVED": " = ", "REGRESSION": " ! "}.get(cat, "   ")
        print(f"\n{marker} {r['instance']}  [{cat}]")
        print(f"    Run A: sim={r['run_a_sim']:.2f}  resolved={r['run_a_a1_resolved']}/{r['run_a_a2_resolved']}  patches={r['run_a_patches']}")
        print(f"    Run B: sim={r['run_b_sim']:.2f}  resolved={r['run_b_a1_resolved']}/{r['run_b_a2_resolved']}  patches={r['run_b_patches']}")
        print(f"    Cross: sim={r['cross_sim']:.2f}  nprg={r['run_b_a2_nprg']}")

    print(f"\n{'=' * 90}")
    print("SUMMARY")
    print(f"{'=' * 90}")
    total = len(results)
    for cat in ["STRONG_CAUSAL_WIN", "WEAK_WIN", "BEHAVIOR_CHANGE_NO_WIN",
                "NO_EFFECT", "ALREADY_SOLVED", "REGRESSION"]:
        items = categories.get(cat, [])
        if items:
            print(f"  {cat:<30} {len(items)}/{total}  ({len(items)/total:.0%})  {items}")

    wins = len(categories.get("STRONG_CAUSAL_WIN", [])) + len(categories.get("WEAK_WIN", []))
    behavior = wins + len(categories.get("BEHAVIOR_CHANGE_NO_WIN", []))
    print(f"\n  Causal Win Rate:      {wins}/{total} ({wins/total:.0%})")
    print(f"  Behavior Change Rate: {behavior}/{total} ({behavior/total:.0%})")


if __name__ == "__main__":
    main()
