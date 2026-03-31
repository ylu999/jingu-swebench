#!/usr/bin/env python3
"""
Thin wrapper around swebench.harness.run_evaluation.main

Usage:
  python scripts/evaluate.py \
    --predictions results/jingu/predictions.jsonl \
    --run-id my-run-001

This builds Docker images for each instance (cached after first build),
applies the patch, runs the repo's test suite, and writes a JSON report
with resolved_ids / unresolved_ids.
"""
import argparse
import sys


def main():
    parser = argparse.ArgumentParser(description="Run SWE-bench harness evaluation")
    parser.add_argument("--predictions", required=True, help="Path to predictions.jsonl")
    parser.add_argument("--run-id", required=True, help="Unique run identifier")
    parser.add_argument("--dataset", default="SWE-bench/SWE-bench_Lite", help="Dataset name")
    parser.add_argument("--split", default="test", help="Dataset split")
    parser.add_argument("--instance-ids", nargs="*", default=[], help="Specific instance IDs (default: all in predictions)")
    parser.add_argument("--max-workers", type=int, default=4, help="Parallel Docker workers")
    parser.add_argument("--timeout", type=int, default=1800, help="Per-instance timeout in seconds")
    parser.add_argument("--cache-level", default="env",
                        choices=["none", "base", "env", "instance"],
                        help="Docker image cache level")
    parser.add_argument("--clean", action="store_true", help="Remove instance images after run")
    parser.add_argument("--report-dir", default=".", help="Directory to write the JSON report file")
    parser.add_argument("--modal", action="store_true", help="Run on Modal (cloud) instead of local Docker")
    args = parser.parse_args()

    try:
        from swebench.harness.run_evaluation import main as run_eval
    except ImportError:
        print("ERROR: swebench not installed. Run: pip install swebench", file=sys.stderr)
        sys.exit(1)

    report_path = run_eval(
        dataset_name=args.dataset,
        split=args.split,
        instance_ids=args.instance_ids,
        predictions_path=args.predictions,
        max_workers=args.max_workers,
        force_rebuild=False,
        cache_level=args.cache_level,
        clean=args.clean,
        open_file_limit=4096,
        run_id=args.run_id,
        timeout=args.timeout,
        namespace=None,
        rewrite_reports=False,
        modal=args.modal,
        report_dir=args.report_dir,
    )

    if report_path:
        print(f"\nReport written to: {report_path}")
    else:
        print("No instances evaluated.")


if __name__ == "__main__":
    main()
