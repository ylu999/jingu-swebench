"""
cognition_taxonomy.py — Offline cognition gate taxonomy over historical trajectories.

Runs declaration_extractor + patch_signals + cognition_check against every
traj.json found under a results directory. Produces a taxonomy JSON with
per-category counts and a per-instance detail list.

Usage:
  python3 scripts/cognition_taxonomy.py --results results/ --output results/cognition-taxonomy.json

Output schema:
  {
    "run_date": "<ISO>",
    "total": N,
    "categories": {
      "missing_declaration":         N,  # no FIX_TYPE line found at all
      "unusable_declaration":        N,  # FIX_TYPE present but not in controlled vocabulary
      "pass_consistent":             N,  # declaration present and valid, no contradiction
      "signal_contradiction":        N,  # type vs patch signal mismatch
      "principal_contradiction":     N,  # self-contradicting principals
      "weak_declaration":            N,  # FIX_TYPE valid but principals empty
      "no_patch":                    N   # traj has no submission patch
    },
    "instances": [
      {
        "instance_id":    str,
        "traj_path":      str,
        "category":       str,
        "fix_type":       str | null,
        "principals":     [str],
        "patch_signals":  [str],
        "violations":     [{kind, reason}],
        "patch_lines":    int
      }
    ]
  }
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Import gate modules from same scripts/ dir
sys.path.insert(0, str(Path(__file__).parent))
from declaration_extractor import extract_declaration, extract_last_agent_message
from patch_signals import extract_patch_signals
from cognition_check import check_cognition

# Controlled vocabulary — must match TYPE_PRINCIPAL_POLICY keys in jingu-policy-core
VALID_FIX_TYPES = {
    "root_cause_fix", "workaround_fix", "exploration",
    "test_validation", "environment_fix",
}


def classify(decl: dict, signals: list[str], violations: list[dict], has_patch: bool) -> str:
    """
    Map gate inputs/outputs to a single taxonomy category.

    Priority order (first match wins):
      no_patch               → traj has no submission
      missing_declaration    → no FIX_TYPE line found
      unusable_declaration   → FIX_TYPE present but not in controlled vocabulary
      weak_declaration       → valid type but principals list empty
      signal_contradiction   → type contradicts patch signals
      principal_contradiction → self-contradicting principals
      pass_consistent        → declaration valid, no contradiction found
    """
    if not has_patch:
        return "no_patch"
    if not decl or not decl.get("type"):
        return "missing_declaration"
    if decl["type"] not in VALID_FIX_TYPES:
        return "unusable_declaration"
    if not decl.get("principals"):
        return "weak_declaration"
    if any(v["kind"] == "signal_contradiction" for v in violations):
        return "signal_contradiction"
    if any(v["kind"] == "principal_contradiction" for v in violations):
        return "principal_contradiction"
    return "pass_consistent"


def process_traj(traj_path: Path) -> dict:
    """Run cognition check on one traj.json. Returns instance detail dict."""
    try:
        traj = json.loads(traj_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return {"error": str(e), "traj_path": str(traj_path)}

    instance_id = traj_path.parent.name
    messages = traj.get("messages", [])
    info = traj.get("info", {})
    patch = info.get("submission", "") or ""

    last_msg = extract_last_agent_message(messages)
    decl = extract_declaration(last_msg)
    signals = extract_patch_signals(patch)
    cog = check_cognition(decl, signals)
    category = classify(decl, signals, cog["violations"], bool(patch.strip()))

    patch_lines = len(patch.splitlines()) if patch else 0

    return {
        "instance_id":   instance_id,
        "traj_path":     str(traj_path),
        "category":      category,
        "fix_type":      decl.get("type") if decl else None,
        "principals":    decl.get("principals", []) if decl else [],
        "patch_signals": signals,
        "violations":    cog["violations"],
        "patch_lines":   patch_lines,
    }


def run_taxonomy(results_dir: Path, output_path: Path) -> None:
    traj_files = sorted(results_dir.rglob("*.traj.json"))
    if not traj_files:
        print(f"No traj.json files found under {results_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(traj_files)} trajectories. Processing...", flush=True)

    instances = []
    for traj_path in traj_files:
        result = process_traj(traj_path)
        instances.append(result)
        cat = result.get("category", "error")
        fix_type = result.get("fix_type") or "-"
        signals = result.get("patch_signals", [])
        print(f"  {result.get('instance_id', '?'):40s}  {cat:30s}  type={fix_type}  signals={signals}")

    # Count categories
    category_counts: dict[str, int] = {
        "missing_declaration":     0,
        "unusable_declaration":    0,
        "pass_consistent":         0,
        "signal_contradiction":    0,
        "principal_contradiction": 0,
        "weak_declaration":        0,
        "no_patch":                0,
        "error":                   0,
    }
    for inst in instances:
        cat = inst.get("category", "error")
        category_counts[cat] = category_counts.get(cat, 0) + 1

    total = len(instances)
    output = {
        "run_date":  datetime.now(timezone.utc).isoformat(),
        "total":     total,
        "categories": category_counts,
        "rates": {
            k: round(v / total, 3) if total else 0
            for k, v in category_counts.items()
        },
        "instances": instances,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2))
    print(f"\nTaxonomy written: {output_path}")
    print(f"\n{'='*50}")
    print(f"  TOTAL: {total}")
    print()
    for cat, count in sorted(category_counts.items(), key=lambda x: -x[1]):
        if count == 0:
            continue
        rate = count / total if total else 0
        bar = "█" * int(rate * 30)
        print(f"  {cat:30s}  {count:4d}  ({rate:.0%})  {bar}")
    print(f"{'='*50}")

    # Gate judgment
    print("\n  GATE JUDGMENT:")
    signal_rate    = category_counts.get("signal_contradiction", 0) / total if total else 0
    missing_rate   = category_counts.get("missing_declaration", 0) / total if total else 0
    unusable_rate  = category_counts.get("unusable_declaration", 0) / total if total else 0
    declared_rate  = 1.0 - missing_rate  # any FIX_TYPE found

    if missing_rate > 0.80:
        print("  → TUNE PROMPT: >80% missing declaration — agent not following declaration protocol")
    elif unusable_rate > 0.20:
        print(f"  → FORMAT ISSUE: {unusable_rate:.0%} unusable_declaration — agent declares but uses wrong vocabulary")
    elif declared_rate >= 0.80 and signal_rate >= 0.05:
        print("  → PROMISING: declaration protocol working (≥80% declare) + ≥5% signal_contradiction")
        print("     Proceed to Step 2 (before/after case sampling)")
    elif declared_rate >= 0.80:
        print(f"  → DECLARING OK, LOW CONTRADICTION: {declared_rate:.0%} declare but only {signal_rate:.0%} signal_contradiction")
        print("     Gate may need expanded contradiction rules, or patches are genuinely consistent")
    else:
        print(f"  → PARTIAL: {declared_rate:.0%} declare — prompt injection partially working, keep monitoring")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", default="results/", help="Results dir to scan for traj.json files")
    parser.add_argument("--output", default="results/cognition-taxonomy.json", help="Output JSON path")
    args = parser.parse_args()

    run_taxonomy(
        Path(args.results),
        Path(args.output),
    )
