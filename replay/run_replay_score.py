#!/usr/bin/env python3
"""Replay L1 Governance Scorer — CLI entry point.

Reads step_events.jsonl + decisions.jsonl from S3 for a batch, scores governance
metrics for each instance/attempt, and outputs a summary report.

Usage:
    # Score a single instance from S3
    python replay/run_replay_score.py --batch batch-p11-routing-control --instance django__django-10097

    # Score all instances in a batch
    python replay/run_replay_score.py --batch batch-p11-routing-control

    # Score from local directory
    python replay/run_replay_score.py --local-dir /path/to/instance_dir --attempt 1

    # Output JSON instead of human-readable
    python replay/run_replay_score.py --batch batch-p11-routing-control --json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

# Add project root for imports
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from scoring.governance import (
    GovernanceMetrics,
    format_report,
    score_governance,
    score_governance_s3,
)


def discover_instances_s3(batch_name: str, bucket: str = "jingu-swebench-results") -> list[str]:
    """Find all instance IDs in a batch by listing S3 prefixes."""
    import boto3

    s3 = boto3.client("s3", region_name="us-west-2")
    prefix = f"{batch_name}/"
    instances: set[str] = set()

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            p = cp["Prefix"].rstrip("/").split("/")[-1]
            # Instance IDs look like "django__django-NNNNN" or similar
            if "__" in p and p != "attempt_1" and p != "attempt_2":
                instances.add(p)
    return sorted(instances)


def discover_attempts_s3(
    batch_name: str, instance_id: str, bucket: str = "jingu-swebench-results"
) -> list[int]:
    """Find all attempt numbers for an instance."""
    import boto3

    s3 = boto3.client("s3", region_name="us-west-2")
    prefix = f"{batch_name}/{instance_id}/"
    attempts: list[int] = []

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            dirname = cp["Prefix"].rstrip("/").split("/")[-1]
            if dirname.startswith("attempt_"):
                try:
                    attempts.append(int(dirname.split("_")[1]))
                except (ValueError, IndexError):
                    pass
    return sorted(attempts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay L1 Governance Scorer")
    parser.add_argument("--batch", help="S3 batch name to score")
    parser.add_argument("--instance", help="Specific instance ID (default: all in batch)")
    parser.add_argument("--attempt", type=int, default=0, help="Attempt number (0=all)")
    parser.add_argument("--local-dir", help="Local instance directory (instead of S3)")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of text")
    args = parser.parse_args()

    results: list[dict] = []

    if args.local_dir:
        # Local mode
        attempts = [args.attempt] if args.attempt > 0 else [1, 2]
        for att in attempts:
            m = score_governance(args.local_dir, attempt=att)
            if m.total_steps > 0:
                results.append({
                    "instance": Path(args.local_dir).name,
                    "attempt": att,
                    "metrics": asdict(m),
                })
                if not args.json:
                    print(f"\n--- {Path(args.local_dir).name} attempt {att} ---")
                    print(format_report(m))
    elif args.batch:
        # S3 mode
        if args.instance:
            instance_ids = [args.instance]
        else:
            instance_ids = discover_instances_s3(args.batch)
            if not instance_ids:
                print(f"No instances found in batch {args.batch}", file=sys.stderr)
                sys.exit(1)

        for iid in instance_ids:
            if args.attempt > 0:
                attempts = [args.attempt]
            else:
                attempts = discover_attempts_s3(args.batch, iid)
                if not attempts:
                    attempts = [1, 2]

            for att in attempts:
                m = score_governance_s3(args.batch, iid, attempt=att)
                if m.total_steps > 0:
                    results.append({
                        "instance": iid,
                        "attempt": att,
                        "metrics": asdict(m),
                    })
                    if not args.json:
                        print(f"\n--- {iid} attempt {att} ---")
                        print(format_report(m))
    else:
        parser.error("Must provide --batch or --local-dir")

    if args.json:
        print(json.dumps(results, indent=2, default=str))

    # Summary
    if not args.json and len(results) > 0:
        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        total_redirects = sum(r["metrics"]["redirect_count"] for r in results)
        total_retries = sum(r["metrics"]["retry_count"] for r in results)
        total_effective = sum(r["metrics"]["effective_redirect_count"] for r in results)
        total_generic = sum(r["metrics"]["generic_retry_count"] for r in results)
        total_specific = sum(r["metrics"]["specific_retry_count"] for r in results)
        avg_gov = sum(r["metrics"]["governance_score"] for r in results) / len(results)

        print(f"Instances scored: {len(results)}")
        print(f"Redirects total: {total_redirects} (effective: {total_effective})")
        print(f"Retries total: {total_retries} (specific: {total_specific}, generic: {total_generic})")
        print(f"Avg governance score: {avg_gov:.3f}")

        # P1.1 key indicators
        print(f"\nP1.1 Key Indicators:")
        print(f"  redirect_count: {total_redirects}")
        eff_rate = total_effective / total_redirects if total_redirects > 0 else 0
        print(f"  effective_redirect_rate: {eff_rate:.0%}")
        total_all_retries = total_generic + total_specific
        gen_ratio = total_generic / total_all_retries if total_all_retries > 0 else 0
        print(f"  generic_retry_ratio: {gen_ratio:.0%}")


if __name__ == "__main__":
    main()
