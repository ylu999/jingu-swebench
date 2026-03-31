#!/usr/bin/env python3
"""
Compare A/B/C experiment groups and print a summary table.

Usage:
  python scripts/compare_groups.py
"""
import json
import re
from pathlib import Path
from statistics import median

GROUPS = [
    ("A", "attempts=1, no stagger",  "results/mini-swe-baseline/jingu-predictions.jsonl", "/tmp/baseline-parallel.log"),
    ("B", "attempts=1, stagger=20",  "results/group-b/jingu-predictions.jsonl",           "/tmp/group-b.log"),
    ("C", "attempts=3, stagger=20",  "results/group-c/jingu-predictions.jsonl",           "/tmp/group-c.log"),
]

HARNESS_REPORTS = {
    "A": "mini-swe-agent+jingu.baseline-mini-swe-v1.json",
    "B": "mini-swe-agent+jingu.group-b.json",
    "C": "mini-swe-agent+jingu.group-c.json",
}

ALL_INSTANCES = [
    "django__django-11039",
    "django__django-11001",
    "django__django-11019",
    "django__django-11049",
    "django__django-11099",
]
TOTAL = len(ALL_INSTANCES)

def load_predictions(path: str) -> dict[str, dict]:
    p = Path(path)
    if not p.exists():
        return {}
    result = {}
    for line in p.read_text().splitlines():
        if line.strip():
            d = json.loads(line)
            result[d["instance_id"]] = d
    return result

def load_resolved(report_path: str) -> set[str]:
    p = Path(report_path)
    if not p.exists():
        return set()
    data = json.loads(p.read_text())
    return set(data.get("resolved_ids", []))

def parse_log(log_path: str) -> dict:
    p = Path(log_path)
    if not p.exists():
        return {}
    text = p.read_text()
    sandbox_times = [float(t) for t in re.findall(r"Sandbox .+ created in ([\d.]+)s", text)]
    timeout_count = text.count("Runtime did not start within") + text.count("TimeoutError")
    best_attempts = [int(m) for m in re.findall(r"best_attempt=(\d+)", text)]
    scores = [float(s) for s in re.findall(r"score=([\d.]+)", text)]
    return {
        "sandbox_times": sandbox_times,
        "timeout_count": timeout_count,
        "best_attempts": best_attempts,
        "scores": scores,
    }

def fmt(val, fallback="-"):
    return f"{val:.1f}" if isinstance(val, float) else (str(val) if val is not None else fallback)

def main():
    rows = []
    for gid, label, pred_path, log_path in GROUPS:
        preds = load_predictions(pred_path)
        resolved = load_resolved(HARNESS_REPORTS.get(gid, ""))
        log = parse_log(log_path)

        accepted = len(preds)
        patch_line_counts = [len(preds[iid]["model_patch"].splitlines()) for iid in preds]
        avg_lines = sum(patch_line_counts) / len(patch_line_counts) if patch_line_counts else None

        st = log.get("sandbox_times", [])
        scores = log.get("scores", [])
        best_attempts = log.get("best_attempts", [])

        rows.append({
            "id": gid,
            "label": label,
            "accepted": f"{accepted}/{TOTAL}",
            "resolved": f"{len(resolved)}/{TOTAL}" if resolved else "-",
            "sb_min": min(st) if st else None,
            "sb_med": median(st) if st else None,
            "sb_max": max(st) if st else None,
            "timeouts": log.get("timeout_count", 0),
            "avg_lines": avg_lines,
            "avg_score": sum(scores)/len(scores) if scores else None,
            "best_attempt_dist": str(sorted(best_attempts)) if best_attempts else "-",
        })

    # Print table
    cols = [
        ("Group",       "id",               8),
        ("Config",      "label",            24),
        ("Accepted",    "accepted",         9),
        ("Resolved",    "resolved",         9),
        ("SB min",      "sb_min",           7),
        ("SB med",      "sb_med",           7),
        ("SB max",      "sb_max",           7),
        ("Timeouts",    "timeouts",         9),
        ("Avg lines",   "avg_lines",        10),
        ("Avg score",   "avg_score",        10),
        ("Best attempt","best_attempt_dist",20),
    ]

    header = "  ".join(f"{name:<{w}}" for name, _, w in cols)
    sep    = "  ".join("-" * w for _, _, w in cols)
    print("\n" + header)
    print(sep)
    for row in rows:
        line = "  ".join(f"{fmt(row[key]):<{w}}" for _, key, w in cols)
        print(line)
    print()

    # Per-instance breakdown
    print("=== Per-instance patch lines ===\n")
    print(f"{'Instance':<35}", end="")
    for gid, _, _, _ in GROUPS:
        print(f"{'Group '+gid:<15}", end="")
    print()
    print("-" * (35 + 15 * len(GROUPS)))
    for iid in ALL_INSTANCES:
        print(f"{iid:<35}", end="")
        for _, _, pred_path, _ in GROUPS:
            preds = load_predictions(pred_path)
            if iid in preds:
                lines = len(preds[iid]["model_patch"].splitlines())
                print(f"{lines:<15}", end="")
            else:
                print(f"{'(none)':<15}", end="")
        print()
    print()

if __name__ == "__main__":
    main()
