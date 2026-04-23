"""Aggregate benchmark results from S3 into summary tables.

Usage:
    python scripts/aggregate_results.py                    # all batches
    python scripts/aggregate_results.py --batches ladder-sonnet46-full30 ceiling-opus46-full30
    python scripts/aggregate_results.py --format csv       # CSV output
    python scripts/aggregate_results.py --format markdown  # Markdown table (default)
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path


S3_BUCKET = "jingu-swebench-results"
BENCHMARK_BATCHES = [
    "baseline-trunk-44d1c33",
    "best-config-v1",
    "ladder-sonnet46-modelonly-full30",
    "ladder-sonnet46-full30",
    "ceiling-opus46-full30",
]


def s3_get_json(key: str) -> dict | None:
    """Fetch a JSON file from S3."""
    try:
        result = subprocess.run(
            ["aws", "s3", "cp", f"s3://{S3_BUCKET}/{key}", "-"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    return None


def get_eval_results(batch_name: str) -> dict | None:
    """Get eval results for a batch."""
    # Try standard eval path
    for prefix in [f"eval-{batch_name}", f"eval_{batch_name}"]:
        data = s3_get_json(f"{prefix}/eval_results.json")
        if data:
            return data
    return None


def get_history() -> list[dict]:
    """Get pipeline history."""
    data = s3_get_json("pipeline-results/history.json")
    return data if isinstance(data, list) else []


def aggregate_batches(batch_names: list[str]) -> list[dict]:
    """Aggregate results for specified batches."""
    rows = []
    history = get_history()
    history_map = {h.get("batch_name", ""): h for h in history}

    for batch in batch_names:
        row = {"batch": batch, "resolved": "?", "total": 30, "rate": "?"}

        # Try history first
        if batch in history_map:
            h = history_map[batch]
            resolved = h.get("resolved_count", h.get("resolved", "?"))
            total = h.get("total_count", h.get("total", 30))
            if isinstance(resolved, int) and isinstance(total, int):
                row["resolved"] = resolved
                row["total"] = total
                row["rate"] = f"{resolved/total*100:.1f}%"

        # Try eval results
        eval_data = get_eval_results(batch)
        if eval_data:
            resolved_ids = eval_data.get("resolved_ids", eval_data.get("resolved", []))
            if isinstance(resolved_ids, list):
                row["resolved"] = len(resolved_ids)
                row["resolved_ids"] = resolved_ids
                row["rate"] = f"{len(resolved_ids)/row['total']*100:.1f}%"

        rows.append(row)
    return rows


def format_markdown(rows: list[dict]) -> str:
    """Format as markdown table."""
    lines = ["| Batch | Resolved | Rate |", "|-------|----------|------|"]
    for r in rows:
        lines.append(f"| {r['batch']} | {r['resolved']}/{r['total']} | {r['rate']} |")
    return "\n".join(lines)


def format_csv(rows: list[dict]) -> str:
    """Format as CSV."""
    lines = ["batch,resolved,total,rate"]
    for r in rows:
        lines.append(f"{r['batch']},{r['resolved']},{r['total']},{r['rate']}")
    return "\n".join(lines)


def per_instance_table(rows: list[dict]) -> str:
    """Show per-instance status across batches."""
    # Collect all instance IDs
    all_ids = set()
    batch_resolved = {}
    for r in rows:
        ids = r.get("resolved_ids", [])
        all_ids.update(ids)
        batch_resolved[r["batch"]] = set(ids)

    if not all_ids:
        return "(no per-instance data available)"

    all_ids = sorted(all_ids)
    header = "| Instance | " + " | ".join(r["batch"][:20] for r in rows) + " |"
    sep = "|----------|" + "|".join("---" for _ in rows) + "|"
    lines = [header, sep]
    for iid in all_ids:
        short = iid.replace("django__django-", "")
        cells = []
        for r in rows:
            cells.append("Y" if iid in batch_resolved.get(r["batch"], set()) else ".")
        lines.append(f"| {short} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Aggregate Jingu benchmark results")
    parser.add_argument("--batches", nargs="*", default=None,
                        help="Batch names to aggregate (default: all benchmark batches)")
    parser.add_argument("--format", choices=["markdown", "csv"], default="markdown")
    parser.add_argument("--per-instance", action="store_true",
                        help="Show per-instance status table")
    args = parser.parse_args()

    batches = args.batches or BENCHMARK_BATCHES
    print(f"Aggregating {len(batches)} batches...\n", file=sys.stderr)

    rows = aggregate_batches(batches)

    if args.format == "csv":
        print(format_csv(rows))
    else:
        print(format_markdown(rows))

    if args.per_instance:
        print("\n## Per-Instance Status\n")
        print(per_instance_table(rows))


if __name__ == "__main__":
    main()
