#!/usr/bin/env python3
"""prompt_regression.py — A/B replay + prompt regression testing framework.

p235: Run two prompt variants from the same checkpoint and compare outcomes.
Maintain a golden traj set for automated prompt regression testing.

Subcommands:
  ab            Run A/B replay on a single checkpoint (two variants)
  suite         Run regression suite against golden trajs
  init-golden   Create golden entries from existing traj directories

Usage:
  python scripts/prompt_regression.py ab --checkpoint <path> --variant-b "v2-hint" --dry-run
  python scripts/prompt_regression.py suite --prompt-change "new-scope" --dry-run
  python scripts/prompt_regression.py init-golden --traj-dir results/ --instance-ids id1,id2
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

# Imports from sibling modules (in scripts/)
from replay_engine import ReplayModifications, ReplayResult, replay_from_checkpoint
from traj_diff import compare_trajs


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PromptVariant:
    """A named prompt variant with replay modifications."""
    name: str                          # human-readable label e.g. "baseline", "v2-explicit-scope"
    modifications: ReplayModifications  # from replay_engine


@dataclass
class VariantResult:
    """Result of running a single variant."""
    test_pass: bool
    steps_used: int
    cost_usd: float
    submitted: bool
    divergence_from_original: dict | None  # DivergencePoint as dict


@dataclass
class RegressionResult:
    """Result of A/B comparison on a single instance."""
    instance_id: str
    checkpoint_step: int
    variants: dict[str, VariantResult]  # variant_name -> result
    winner: str | None                  # variant with better outcome, None if tie


# ---------------------------------------------------------------------------
# Winner determination
# ---------------------------------------------------------------------------

def _determine_winner(variants: dict[str, VariantResult]) -> str | None:
    """Determine which variant wins based on outcome hierarchy.

    Priority:
      1. test_pass > no_pass
      2. If both pass: fewer steps wins
      3. If tied: lower cost wins
      4. If still tied: None
    """
    names = list(variants.keys())
    if len(names) != 2:
        return None

    a_name, b_name = names
    a, b = variants[a_name], variants[b_name]

    # Rule 1: test_pass wins
    if a.test_pass and not b.test_pass:
        return a_name
    if b.test_pass and not a.test_pass:
        return b_name

    # Rule 2: fewer steps (only meaningful if both pass or both fail)
    if a.steps_used < b.steps_used:
        return a_name
    if b.steps_used < a.steps_used:
        return b_name

    # Rule 3: lower cost
    if a.cost_usd < b.cost_usd:
        return a_name
    if b.cost_usd < a.cost_usd:
        return b_name

    # Tie
    return None


# ---------------------------------------------------------------------------
# Core: run_ab_replay
# ---------------------------------------------------------------------------

def run_ab_replay(
    checkpoint_path: Path,
    variant_a: PromptVariant,
    variant_b: PromptVariant,
    config: dict,
    output_dir: Path,
) -> RegressionResult:
    """Run two variants from the same checkpoint and compare results.

    Runs variant_a first, then variant_b. Each gets its own output subdirectory.
    Both results are compared against each other using traj_diff.compare_trajs().

    Args:
        checkpoint_path: Path to the checkpoint .json.gz file.
        variant_a: First prompt variant (typically "baseline").
        variant_b: Second prompt variant (the treatment).
        config: Agent config dict.
        output_dir: Root output directory for this A/B run.

    Returns:
        RegressionResult with both variant outcomes and the winner.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- Load checkpoint metadata for instance_id + step ---
    from checkpoint import load_checkpoint
    ckpt = load_checkpoint(checkpoint_path)
    instance_id = ckpt.instance_id if ckpt else "unknown"
    ckpt_step = ckpt.step_n if ckpt else 0

    variants_results: dict[str, VariantResult] = {}

    for variant in [variant_a, variant_b]:
        variant_dir = output_dir / variant.name
        print(f"\n{'='*60}")
        print(f"  Running variant: {variant.name}")
        print(f"{'='*60}\n", flush=True)

        result = replay_from_checkpoint(
            checkpoint_path=checkpoint_path,
            output_dir=variant_dir,
            modifications=variant.modifications,
        )

        # Determine test_pass from result
        # The replay produces a patch; we check if it was submitted successfully
        test_pass = result.success and bool(result.patch)
        submitted = bool(result.patch)

        # Compare this variant's traj against the original checkpoint's traj
        divergence: dict | None = None
        if result.traj_path:
            try:
                # Look for original traj in the checkpoint's parent directory
                ckpt_parent = checkpoint_path.parent
                orig_traj_candidates = list(ckpt_parent.rglob("*.traj.json"))
                if orig_traj_candidates:
                    with open(orig_traj_candidates[0]) as f:
                        orig_traj = json.load(f)
                    with open(result.traj_path) as f:
                        repl_traj = json.load(f)
                    comparison = compare_trajs(orig_traj, repl_traj)
                    divergence = comparison.get("divergence")
            except Exception as e:
                print(f"  [warn] divergence comparison failed: {e}", flush=True)

        cost_usd = result.cost.get("total_usd", 0.0) if result.cost else 0.0

        variants_results[variant.name] = VariantResult(
            test_pass=test_pass,
            steps_used=result.total_steps,
            cost_usd=cost_usd,
            submitted=submitted,
            divergence_from_original=divergence,
        )

    # --- Compare the two variant trajs against each other ---
    winner = _determine_winner(variants_results)

    regression = RegressionResult(
        instance_id=instance_id,
        checkpoint_step=ckpt_step,
        variants=variants_results,
        winner=winner,
    )

    # Save result
    result_path = output_dir / "ab_result.json"
    with open(result_path, "w") as f:
        json.dump(asdict(regression), f, indent=2, default=str)
    print(f"\n  A/B result saved to {result_path}")

    # Print summary
    _print_ab_summary(regression)

    return regression


def _print_ab_summary(result: RegressionResult) -> None:
    """Print a human-readable summary of the A/B comparison."""
    print(f"\n{'='*60}")
    print(f"  A/B RESULT: {result.instance_id} (step {result.checkpoint_step})")
    print(f"{'='*60}")

    for name, vr in result.variants.items():
        status = "PASS" if vr.test_pass else "FAIL"
        sub = "yes" if vr.submitted else "no"
        print(f"  {name:20s}  {status}  steps={vr.steps_used}  cost=${vr.cost_usd:.4f}  submitted={sub}")

    if result.winner:
        print(f"\n  WINNER: {result.winner}")
    else:
        print(f"\n  WINNER: (tie)")
    print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# Core: run_regression_suite
# ---------------------------------------------------------------------------

def run_regression_suite(
    golden_dir: Path,
    prompt_change: PromptVariant,
    config: dict,
    output_dir: Path,
) -> list[RegressionResult]:
    """Run A/B regression tests against golden trajectory set.

    For each golden entry with a valid checkpoint, runs an A/B comparison:
    baseline (no modifications) vs the prompt_change variant.

    Args:
        golden_dir: Directory containing manifest.json and checkpoint files.
        prompt_change: The treatment variant to test against baseline.
        config: Agent config dict.
        output_dir: Root output directory for suite results.

    Returns:
        List of RegressionResult for each golden instance.
    """
    manifest_path = golden_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: manifest not found at {manifest_path}")
        return []

    with open(manifest_path) as f:
        manifest = json.load(f)

    print(f"Regression suite: {len(manifest)} golden entries")
    print(f"Treatment variant: {prompt_change.name}\n")

    results: list[RegressionResult] = []
    baseline = PromptVariant(name="baseline", modifications=ReplayModifications())

    for entry in manifest:
        instance_id = entry["instance_id"]
        ckpt_step = entry.get("checkpoint_step")

        # Find checkpoint file
        ckpt_path = _find_checkpoint(golden_dir, instance_id, ckpt_step)
        if not ckpt_path:
            print(f"  SKIP {instance_id}: no checkpoint found")
            continue

        print(f"\n  Running: {instance_id}")
        instance_output = output_dir / instance_id

        result = run_ab_replay(
            checkpoint_path=ckpt_path,
            variant_a=baseline,
            variant_b=prompt_change,
            config=config,
            output_dir=instance_output,
        )
        results.append(result)

    # --- Aggregate summary ---
    if results:
        _print_suite_summary(results, prompt_change.name)

    # Save suite results
    suite_path = output_dir / "suite_results.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(suite_path, "w") as f:
        json.dump([asdict(r) for r in results], f, indent=2, default=str)
    print(f"\nSuite results saved to {suite_path}")

    return results


def _find_checkpoint(
    golden_dir: Path,
    instance_id: str,
    checkpoint_step: int | None,
) -> Path | None:
    """Find a checkpoint file for a golden instance.

    Searches golden_dir/<instance_id>/checkpoints/ for checkpoint files.
    If checkpoint_step is specified, finds that specific step.
    Otherwise, returns the latest checkpoint.
    """
    instance_dir = golden_dir / instance_id / "checkpoints"
    if not instance_dir.exists():
        # Also try flat structure
        instance_dir = golden_dir / instance_id
        if not instance_dir.exists():
            return None

    # Find checkpoint files (.json.gz)
    candidates = sorted(instance_dir.glob("checkpoint_*.json.gz"))
    if not candidates:
        candidates = sorted(instance_dir.glob("*.json.gz"))
    if not candidates:
        return None

    if checkpoint_step is not None:
        # Find specific step
        for c in candidates:
            if f"step_{checkpoint_step}" in c.name or f"_{checkpoint_step}" in c.name:
                return c

    # Return the latest (last sorted)
    return candidates[-1] if candidates else None


def _print_suite_summary(results: list[RegressionResult], treatment_name: str) -> None:
    """Print aggregated suite results."""
    total = len(results)
    baseline_wins = sum(1 for r in results if r.winner == "baseline")
    treatment_wins = sum(1 for r in results if r.winner == treatment_name)
    ties = sum(1 for r in results if r.winner is None)

    # Pass rate delta
    baseline_passes = sum(
        1 for r in results
        if r.variants.get("baseline", VariantResult(False, 0, 0, False, None)).test_pass
    )
    treatment_passes = sum(
        1 for r in results
        if r.variants.get(treatment_name, VariantResult(False, 0, 0, False, None)).test_pass
    )

    # Cost delta
    baseline_cost = sum(
        r.variants.get("baseline", VariantResult(False, 0, 0, False, None)).cost_usd
        for r in results
    )
    treatment_cost = sum(
        r.variants.get(treatment_name, VariantResult(False, 0, 0, False, None)).cost_usd
        for r in results
    )

    # Regression count (baseline passed but treatment didn't)
    regressions = sum(
        1 for r in results
        if r.variants.get("baseline", VariantResult(False, 0, 0, False, None)).test_pass
        and not r.variants.get(treatment_name, VariantResult(False, 0, 0, False, None)).test_pass
    )

    print(f"\n{'='*60}")
    print(f"  SUITE SUMMARY ({total} instances)")
    print(f"{'='*60}")
    print(f"  Wins:  baseline={baseline_wins}  {treatment_name}={treatment_wins}  tie={ties}")
    print(f"  Pass:  baseline={baseline_passes}/{total}  {treatment_name}={treatment_passes}/{total}")
    print(f"  Cost:  baseline=${baseline_cost:.4f}  {treatment_name}=${treatment_cost:.4f}  "
          f"delta=${treatment_cost - baseline_cost:+.4f}")
    print(f"  Regressions: {regressions}")
    if regressions > 0:
        print(f"  WARNING: {regressions} instance(s) regressed!")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# init-golden: create golden entries from existing traj directories
# ---------------------------------------------------------------------------

def cmd_init_golden(args: argparse.Namespace) -> None:
    """Create golden entries from existing traj directories."""
    traj_dir = Path(args.traj_dir)
    golden_dir = Path(args.golden_dir)
    instance_ids = [iid.strip() for iid in args.instance_ids.split(",")]

    golden_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = golden_dir / "manifest.json"

    # Load existing manifest if present
    existing: list[dict] = []
    if manifest_path.exists():
        with open(manifest_path) as f:
            existing = json.load(f)
    existing_ids = {e["instance_id"] for e in existing}

    added = 0
    for iid in instance_ids:
        if iid in existing_ids:
            print(f"  SKIP {iid}: already in manifest")
            continue

        # Find traj directory for this instance
        candidates = list(traj_dir.rglob(f"{iid}/*.traj.json"))
        if not candidates:
            candidates = list(traj_dir.rglob(f"*{iid}*/*.traj.json"))
        if not candidates:
            print(f"  SKIP {iid}: no traj found in {traj_dir}")
            continue

        # Find checkpoint step (latest checkpoint if available)
        ckpt_step = None
        instance_path = candidates[0].parent
        ckpt_files = sorted(instance_path.glob("checkpoints/checkpoint_*.json.gz"))
        if ckpt_files:
            # Extract step number from filename
            import re
            m = re.search(r"step_(\d+)", ckpt_files[-1].name)
            if m:
                ckpt_step = int(m.group(1))

        entry = {
            "instance_id": iid,
            "description": f"Auto-added from {traj_dir.name}",
            "checkpoint_step": ckpt_step,
            "expected_outcome": "resolved",
            "notes": f"Source: {candidates[0].parent}",
        }
        existing.append(entry)
        added += 1
        print(f"  ADD {iid}: checkpoint_step={ckpt_step}")

    # Write manifest
    with open(manifest_path, "w") as f:
        json.dump(existing, f, indent=4)

    print(f"\nManifest: {len(existing)} entries ({added} new) -> {manifest_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="A/B replay + prompt regression testing framework.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  %(prog)s ab --checkpoint ckpt.json.gz --variant-b "v2-hint" --inject-hint-b "Focus on tests" --dry-run
  %(prog)s suite --prompt-change "new-scope" --inject-hint "Be more precise" --dry-run
  %(prog)s init-golden --traj-dir results/batch-p10 --instance-ids django__django-10097,django__django-10554
""",
    )
    subparsers = parser.add_subparsers(dest="command")

    # -- ab: A/B replay on single checkpoint --
    ab_parser = subparsers.add_parser(
        "ab",
        help="Run A/B replay on a single checkpoint (two prompt variants)",
    )
    ab_parser.add_argument("--checkpoint", required=True, help="Path to checkpoint .json.gz file")
    ab_parser.add_argument("--variant-a", default="baseline", help="Name for variant A (default: baseline)")
    ab_parser.add_argument("--variant-b", required=True, help="Name for variant B (the treatment)")
    ab_parser.add_argument("--inject-hint-a", default=None, help="Hint to inject for variant A")
    ab_parser.add_argument("--inject-hint-b", default=None, help="Hint to inject for variant B")
    ab_parser.add_argument("--config", default=None, help="Path to config JSON file")
    ab_parser.add_argument("--output-dir", default=None, help="Output directory (default: <checkpoint_dir>/ab_<timestamp>)")
    ab_parser.add_argument("--dry-run", action="store_true", help="Show plan without making API calls")

    # -- suite: regression suite against golden trajs --
    suite_parser = subparsers.add_parser(
        "suite",
        help="Run regression suite against golden trajectory set",
    )
    suite_parser.add_argument("--golden-dir", default="golden_trajs", help="Golden trajs directory (default: golden_trajs/)")
    suite_parser.add_argument("--prompt-change", required=True, help="Name for the treatment variant")
    suite_parser.add_argument("--inject-hint", default=None, help="Hint to inject for the treatment variant")
    suite_parser.add_argument("--config", default=None, help="Path to config JSON file")
    suite_parser.add_argument("--output-dir", default=None, help="Output directory (default: regression_<timestamp>)")
    suite_parser.add_argument("--dry-run", action="store_true", help="Show plan without making API calls")

    # -- init-golden: create golden entries --
    init_parser = subparsers.add_parser(
        "init-golden",
        help="Create golden entries from existing traj directories",
    )
    init_parser.add_argument("--traj-dir", required=True, help="Directory containing instance traj outputs")
    init_parser.add_argument("--instance-ids", required=True, help="Comma-separated instance IDs to add")
    init_parser.add_argument("--golden-dir", default="golden_trajs", help="Golden trajs directory (default: golden_trajs/)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # --- Dispatch ---
    if args.command == "ab":
        _handle_ab(args)
    elif args.command == "suite":
        _handle_suite(args)
    elif args.command == "init-golden":
        cmd_init_golden(args)
    else:
        parser.print_help()
        sys.exit(1)


def _handle_ab(args: argparse.Namespace) -> None:
    """Handle the 'ab' subcommand."""
    checkpoint_path = Path(args.checkpoint)

    variant_a = PromptVariant(
        name=args.variant_a,
        modifications=ReplayModifications(inject_hint=args.inject_hint_a),
    )
    variant_b = PromptVariant(
        name=args.variant_b,
        modifications=ReplayModifications(inject_hint=args.inject_hint_b),
    )

    if args.dry_run:
        print("=== DRY RUN (ab) ===")
        print(f"Checkpoint:  {checkpoint_path}")
        print(f"Variant A:   {variant_a.name}  hint={args.inject_hint_a or '(none)'}")
        print(f"Variant B:   {variant_b.name}  hint={args.inject_hint_b or '(none)'}")
        print(f"Config:      {args.config or '(default)'}")
        print(f"Output dir:  {args.output_dir or '(auto)'}")
        print("\nNo LLM API calls will be made.")
        return

    config: dict = {}
    if args.config:
        with open(args.config) as f:
            config = json.load(f)

    output_dir = Path(args.output_dir) if args.output_dir else (
        checkpoint_path.parent / f"ab_{int(time.time())}"
    )

    run_ab_replay(
        checkpoint_path=checkpoint_path,
        variant_a=variant_a,
        variant_b=variant_b,
        config=config,
        output_dir=output_dir,
    )


def _handle_suite(args: argparse.Namespace) -> None:
    """Handle the 'suite' subcommand."""
    golden_dir = Path(args.golden_dir)

    prompt_change = PromptVariant(
        name=args.prompt_change,
        modifications=ReplayModifications(inject_hint=args.inject_hint),
    )

    if args.dry_run:
        manifest_path = golden_dir / "manifest.json"
        entries: list[dict] = []
        if manifest_path.exists():
            with open(manifest_path) as f:
                entries = json.load(f)

        print("=== DRY RUN (suite) ===")
        print(f"Golden dir:     {golden_dir}")
        print(f"Manifest:       {len(entries)} entries")
        print(f"Treatment:      {prompt_change.name}  hint={args.inject_hint or '(none)'}")
        print(f"Config:         {args.config or '(default)'}")
        print(f"Output dir:     {args.output_dir or '(auto)'}")
        print()
        for entry in entries:
            ckpt = _find_checkpoint(golden_dir, entry["instance_id"], entry.get("checkpoint_step"))
            status = "READY" if ckpt else "NO CHECKPOINT"
            print(f"  {entry['instance_id']:40s}  {status}")
        print("\nNo LLM API calls will be made.")
        return

    config: dict = {}
    if args.config:
        with open(args.config) as f:
            config = json.load(f)

    output_dir = Path(args.output_dir) if args.output_dir else (
        Path(f"regression_{int(time.time())}")
    )

    run_regression_suite(
        golden_dir=golden_dir,
        prompt_change=prompt_change,
        config=config,
        output_dir=output_dir,
    )


if __name__ == "__main__":
    main()
