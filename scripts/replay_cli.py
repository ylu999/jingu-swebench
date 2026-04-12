#!/usr/bin/env python3
"""replay_cli.py — Jingu replay CLI.

Subcommands:
  list-checkpoints  Show available checkpoint steps for an instance/attempt
  replay            Launch replay from a checkpoint with optional modifications
  compare           Compare original vs replayed trajectory, find divergence point

Usage:
  python scripts/replay_cli.py list-checkpoints --traj-dir <dir> [--attempt N]
  python scripts/replay_cli.py replay --traj-dir <dir> --from-step N [--inject-hint ...]
  python scripts/replay_cli.py compare --original <dir> --replayed <dir>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def cmd_list_checkpoints(args: argparse.Namespace) -> None:
    """List available checkpoints for an instance attempt."""
    from checkpoint import list_checkpoints

    traj_dir = Path(args.traj_dir)
    checkpoints = list_checkpoints(traj_dir, args.attempt)

    if not checkpoints:
        print("No checkpoints found.")
        return

    print(f"{'Step':>6} | {'Phase':<20} | {'Trigger':<20} | {'Path'}")
    print("-" * 80)
    for ck in checkpoints:
        print(
            f"{ck['step_n']:>6} | "
            f"{ck.get('phase', '?'):<20} | "
            f"{ck['trigger']:<20} | "
            f"{ck.get('path', '')}"
        )


def cmd_replay(args: argparse.Namespace) -> None:
    """Launch a replay from a specified checkpoint step."""
    from checkpoint import list_checkpoints
    from replay_engine import replay_from_checkpoint, ReplayModifications

    traj_dir = Path(args.traj_dir)

    # Find the checkpoint file for --from-step
    checkpoints = list_checkpoints(traj_dir, args.attempt)
    ckpt = next((c for c in checkpoints if c["step_n"] == args.from_step), None)
    if not ckpt:
        available = [c["step_n"] for c in checkpoints]
        print(f"No checkpoint at step {args.from_step}. Available: {available}")
        sys.exit(1)

    mods = ReplayModifications(
        inject_hint=args.inject_hint,
        replace_system_prompt=args.replace_system_prompt,
        inject_user_message=args.inject_user_message,
    )

    if args.dry_run:
        print("=== DRY RUN ===")
        print(f"Checkpoint: step {args.from_step}, trigger={ckpt['trigger']}, phase={ckpt.get('phase')}")
        print(f"Modifications: {mods}")
        print("No LLM API calls will be made.")
        return

    # Load config
    config: dict = {}
    if args.config:
        with open(args.config) as f:
            config = json.load(f)

    output_dir = Path(args.output_dir) if args.output_dir else traj_dir / "replays"

    result = replay_from_checkpoint(
        checkpoint_path=Path(ckpt["path"]),
        output_dir=output_dir,
        modifications=mods,
    )

    print("Replay complete:")
    print(f"  From step: {result.checkpoint_step}")
    print(f"  Total steps: {result.total_steps}")
    print(f"  Output: {result.output_dir}")
    print(f"  Success: {result.success}")
    if result.error:
        print(f"  Error: {result.error}")


def cmd_compare(args: argparse.Namespace) -> None:
    """Compare original vs replayed trajectory — find divergence point."""
    import json
    from traj_diff import compare_trajs, format_comparison

    orig_path = Path(args.original)
    repl_path = Path(args.replayed)

    # Support both direct traj.json paths and instance directories
    if orig_path.is_dir():
        candidates = list(orig_path.rglob("*.traj.json"))
        if not candidates:
            print(f"No traj.json found in {orig_path}")
            sys.exit(1)
        orig_path = candidates[0]

    if repl_path.is_dir():
        candidates = list(repl_path.rglob("*.traj.json"))
        if not candidates:
            print(f"No traj.json found in {repl_path}")
            sys.exit(1)
        repl_path = candidates[0]

    with open(orig_path) as f:
        orig = json.load(f)
    with open(repl_path) as f:
        repl = json.load(f)

    result = compare_trajs(orig, repl)
    print(format_comparison(result))

    # Optionally dump raw JSON
    if args.json:
        print("\n--- RAW JSON ---")
        print(json.dumps(result, indent=2, default=str))


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Jingu replay CLI — list checkpoints, launch replays, compare trajectories.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  %(prog)s list-checkpoints --traj-dir results/instance_dir --attempt 1
  %(prog)s replay --traj-dir results/instance_dir --from-step 5 --dry-run
  %(prog)s replay --traj-dir results/instance_dir --from-step 5 --inject-hint "Focus on the test file"
  %(prog)s compare --original results/original --replayed results/replayed
""",
    )
    subparsers = parser.add_subparsers(dest="command")

    # -- list-checkpoints --
    ls_parser = subparsers.add_parser(
        "list-checkpoints",
        help="Show available checkpoint steps for an instance",
    )
    ls_parser.add_argument("--traj-dir", required=True, help="Instance output directory")
    ls_parser.add_argument("--attempt", type=int, default=1, help="Attempt number (default: 1)")

    # -- replay --
    rp_parser = subparsers.add_parser(
        "replay",
        help="Launch replay from a checkpoint with optional modifications",
    )
    rp_parser.add_argument("--traj-dir", required=True, help="Instance output directory")
    rp_parser.add_argument("--from-step", type=int, required=True, help="Checkpoint step to resume from")
    rp_parser.add_argument("--attempt", type=int, default=1, help="Attempt number (default: 1)")
    rp_parser.add_argument("--inject-hint", default=None, help="Hint to inject as user message before resuming")
    rp_parser.add_argument("--replace-system-prompt", default=None, help="Replace the system prompt entirely")
    rp_parser.add_argument("--inject-user-message", default=None, help="Inject a user message before resuming")
    rp_parser.add_argument("--output-dir", default=None, help="Output directory for replay artifacts")
    rp_parser.add_argument("--dry-run", action="store_true", help="Show what would happen without making LLM calls")
    rp_parser.add_argument("--config", default=None, help="Path to config JSON file")

    # -- compare --
    cmp_parser = subparsers.add_parser(
        "compare",
        help="Compare original vs replayed trajectory — find divergence point",
    )
    cmp_parser.add_argument("--original", required=True, help="Path to original traj.json or instance directory")
    cmp_parser.add_argument("--replayed", required=True, help="Path to replayed traj.json or instance directory")
    cmp_parser.add_argument("--json", action="store_true", help="Also dump raw comparison as JSON")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    handlers = {
        "list-checkpoints": cmd_list_checkpoints,
        "replay": cmd_replay,
        "compare": cmd_compare,
    }
    handler = handlers.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
