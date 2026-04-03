"""
aggregate_strategies.py — p178: offline aggregation of strategy JSONL log.

Reads strategy_log.jsonl, groups by bucket key (failure_class × enforced_violations),
computes hint win rates, writes strategy_table.json.

Usage:
  python3 scripts/aggregate_strategies.py \
    --log /root/results/strategy_log.jsonl \
    --out strategy_table.json

strategy_table.json format:
  {
    "<bucket_key>": {
      "<hint_text>": {
        "win_rate": 0.73,
        "sample_count": 11,
        "solved": 8,
        "total": 11
      }
    }
  }

Minimum sample threshold before a bucket is trusted (MIN_SAMPLES = 3).
Below this, the table entry exists but ε-greedy should treat it as cold-start.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from strategy_logger import load_strategy_log, make_bucket_key

# Minimum samples before we trust the win rate for exploitation
MIN_SAMPLES = 3

# p178.1: primary reward = next_attempt_admitted (retry-level effectiveness)
# Falls back to legacy outcome field for entries written before p178.1
SOLVED_OUTCOMES = {"solved"}


def aggregate(log_path: str | Path, out_path: str | Path) -> dict:
    """
    Read JSONL log, aggregate per-bucket hint win rates, write strategy_table.json.
    Returns the table dict.
    """
    entries = load_strategy_log(log_path)

    # bucket_key → hint_text → {solved: int, total: int}
    counts: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: {"solved": 0, "total": 0}))

    for entry in entries:
        key = make_bucket_key(entry.failure_class, entry.enforced_violation_codes)
        hint = entry.hint_used.strip()
        if not hint:
            hint = "(no hint)"
        counts[key][hint]["total"] += 1
        # p178.1: primary reward = next_attempt_admitted (retry-level effectiveness)
        # next_attempt_admitted=True means the hint helped attempt N+1 get admitted
        if entry.next_attempt_admitted:
            counts[key][hint]["solved"] += 1

    # Build output table with win_rate + sample_count
    table: dict[str, dict[str, dict]] = {}
    for bucket_key, hints in counts.items():
        table[bucket_key] = {}
        for hint_text, stats in hints.items():
            total = stats["total"]
            solved = stats["solved"]
            table[bucket_key][hint_text] = {
                "win_rate": round(solved / total, 4) if total > 0 else 0.0,
                "sample_count": total,
                "solved": solved,
                "total": total,
                "trusted": total >= MIN_SAMPLES,
            }

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(table, indent=2, ensure_ascii=False))

    # Summary
    total_entries = len(entries)
    total_buckets = len(table)
    trusted_buckets = sum(
        1 for hints in table.values()
        if any(h["trusted"] for h in hints.values())
    )
    print(f"[aggregate] entries={total_entries}  buckets={total_buckets}  trusted={trusted_buckets}")
    print(f"[aggregate] written → {out_path}")
    return table


def main() -> None:
    global MIN_SAMPLES
    parser = argparse.ArgumentParser(description="Aggregate strategy log to win-rate table")
    parser.add_argument("--log", required=True, help="Path to strategy_log.jsonl")
    parser.add_argument("--out", required=True, help="Path to output strategy_table.json")
    parser.add_argument("--min-samples", type=int, default=MIN_SAMPLES,
                        help=f"Minimum samples for trusted bucket (default: {MIN_SAMPLES})")
    args = parser.parse_args()

    MIN_SAMPLES = args.min_samples

    table = aggregate(args.log, args.out)

    # Print summary table
    print("\nStrategy table summary:")
    for bucket_key, hints in sorted(table.items()):
        print(f"\n  [{bucket_key}]")
        for hint_text, stats in sorted(hints.items(), key=lambda x: -x[1]["win_rate"]):
            trusted_mark = "✓" if stats["trusted"] else "·"
            print(f"    {trusted_mark} win={stats['win_rate']:.2f}  n={stats['sample_count']}  "
                  f"hint={hint_text[:80]!r}")


if __name__ == "__main__":
    main()
