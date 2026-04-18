#!/usr/bin/env python3
"""
replay_gate.py — Trajectory replay harness for gate verification.

Reads historical traj data (checkpoints, predictions, step_events) and replays
gate decisions deterministically. Answers: "would this gate have changed the
outcome for this specific trajectory?"

Three replay modes:
  1. gate_replay: Run P2/P3 gates on historical ANALYZE record + patch
  2. route_replay: Check if gate verdict would change routing decision
  3. prompt_delta: Compare prompt with/without new gate constraints

Usage:
  python scripts/replay_gate.py gate \
    --batch p2-scope-smoke-r2 \
    --instances django__django-11477 django__django-11400 django__django-11265

  python scripts/replay_gate.py gate --local-dir /tmp/p2-smoke-r2/11477/attempt_1

  python scripts/replay_gate.py table \
    --batch p2-scope-smoke-r2
"""

import argparse
import gzip
import json
import os
import re
import sys
import tempfile

# Add scripts/ to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from phase_record import PhaseRecord
from analysis_gate import evaluate_analysis


# ── Traj loading ─────────────────────────────────────────────────────────────

def _load_checkpoint(checkpoint_path: str) -> dict:
    """Load a gzipped checkpoint JSON."""
    with gzip.open(checkpoint_path, "rt") as f:
        return json.load(f)


def _latest_checkpoint(attempt_dir: str) -> dict | None:
    """Find and load the latest checkpoint in an attempt directory."""
    cp_dir = os.path.join(attempt_dir, "checkpoints")
    if not os.path.isdir(cp_dir):
        return None
    files = [f for f in os.listdir(cp_dir) if f.endswith(".json.gz")]
    if not files:
        return None
    # Sort by step number
    files.sort(key=lambda f: int(re.search(r"step_(\d+)", f).group(1)))
    return _load_checkpoint(os.path.join(cp_dir, files[-1]))


def _extract_patch_files_from_prediction(predictions_path: str, instance_id: str) -> list[str]:
    """Extract patch file paths from predictions JSONL."""
    if not os.path.isfile(predictions_path):
        return []
    with open(predictions_path) as f:
        for line in f:
            d = json.loads(line.strip())
            if d.get("instance_id") == instance_id:
                patch = d.get("model_patch", "")
                # Extract from diff headers: +++ b/path/to/file.py
                files = []
                for pline in patch.split("\n"):
                    if pline.startswith("+++ b/"):
                        files.append(pline[6:].strip())
                return list(dict.fromkeys(files))  # dedupe preserving order
    return []


def _extract_patch_files_from_step_events(attempt_dir: str) -> list[str]:
    """Fallback: find files from step_events patch_non_empty steps."""
    se_path = os.path.join(attempt_dir, "step_events.jsonl")
    if not os.path.isfile(se_path):
        return []
    # We can't get exact file names from step_events alone,
    # but we can confirm patch existed
    return []


def _norm_file(f: str) -> str:
    """Normalize file path (strip /testbed/, a/, b/ prefixes)."""
    f = f.strip()
    for prefix in ("/testbed/", "a/", "b/"):
        if f.startswith(prefix):
            f = f[len(prefix):]
    return f


# ── PhaseRecord reconstruction ───────────────────────────────────────────────

def _reconstruct_phase_record(pr_dict: dict) -> PhaseRecord:
    """Reconstruct a PhaseRecord from a checkpoint's phase_records entry."""
    return PhaseRecord(
        phase=pr_dict.get("phase", ""),
        subtype=pr_dict.get("subtype", ""),
        principals=pr_dict.get("principals", []),
        claims=pr_dict.get("claims", []),
        evidence_refs=pr_dict.get("evidence_refs", []),
        from_steps=pr_dict.get("from_steps", []),
        content=pr_dict.get("content", pr_dict.get("content_preview", "")),
        root_cause=pr_dict.get("root_cause", ""),
        causal_chain=pr_dict.get("causal_chain", ""),
        invariant_capture=pr_dict.get("invariant_capture", {}),
        plan=pr_dict.get("plan", ""),
        alternative_hypotheses=pr_dict.get("alternative_hypotheses", []),
        repair_strategy_type=pr_dict.get("repair_strategy_type", ""),
        root_cause_location_files=pr_dict.get("root_cause_location_files", []),
        mechanism_path=pr_dict.get("mechanism_path", []),
        rejected_nearby_files=pr_dict.get("rejected_nearby_files", []),
        observations=pr_dict.get("observations", []),
        options=pr_dict.get("options", []),
        chosen=pr_dict.get("chosen", ""),
        rationale=pr_dict.get("rationale", ""),
        files_to_modify=pr_dict.get("files_to_modify", []),
        scope_boundary=pr_dict.get("scope_boundary", ""),
        invariants=pr_dict.get("invariants", []),
        design_comparison=pr_dict.get("design_comparison", {}),
        patch_description=pr_dict.get("patch_description", ""),
        files_modified=pr_dict.get("files_modified", []),
        test_results=pr_dict.get("test_results", {}),
        success_criteria_met=pr_dict.get("success_criteria_met", []),
        residual_risks=pr_dict.get("residual_risks", []),
    )


# ── Gate replay ──────────────────────────────────────────────────────────────

def replay_gates(
    checkpoint: dict,
    patch_files: list[str],
    instance_id: str = "",
) -> dict:
    """Replay P2 + P3 gates on a historical checkpoint.

    Returns a verdict dict with all gate scores and decisions.
    """
    cp_state = checkpoint.get("cp_state", {})
    phase_records = checkpoint.get("phase_records", [])

    # Find ANALYZE record
    analyze_pr_dict = None
    for pr in phase_records:
        if pr.get("phase") == "ANALYZE":
            analyze_pr_dict = pr
            break

    result = {
        "instance_id": instance_id,
        "has_analyze_record": analyze_pr_dict is not None,
        "patch_files": patch_files,
    }

    if not analyze_pr_dict:
        result["error"] = "no ANALYZE phase record found"
        return result

    # Reconstruct PhaseRecord and run analysis gate
    pr = _reconstruct_phase_record(analyze_pr_dict)

    # If root_cause_location_files empty, apply fallback extraction (same as declaration_extractor)
    if not pr.root_cause_location_files and pr.root_cause:
        file_patterns = re.findall(
            r'(?:/testbed/)?([a-zA-Z_][\w/]*\.(?:py|js|ts|go|rs|java|c|cpp|h|rb))\b',
            pr.root_cause,
        )
        pr.root_cause_location_files = list(dict.fromkeys(file_patterns))[:5]

    verdict = evaluate_analysis(pr)

    result["analysis_gate"] = {
        "passed": verdict.passed,
        "failed_rules": verdict.failed_rules,
        "scores": {k: v for k, v in verdict.scores.items() if not k.endswith("_note")},
        "notes": {k: v for k, v in verdict.scores.items() if k.endswith("_note")},
    }

    # P2: Scope consistency check
    analyze_files = pr.root_cause_location_files
    analyze_norm = {_norm_file(f) for f in analyze_files}
    patch_norm = {_norm_file(f) for f in patch_files}

    if analyze_norm and patch_norm:
        overlap = analyze_norm & patch_norm
        result["p2_scope"] = {
            "analyze_files": sorted(analyze_norm),
            "patch_files": sorted(patch_norm),
            "overlap": sorted(overlap),
            "drift": len(overlap) == 0,
            "verdict": "SCOPE_DRIFT" if len(overlap) == 0 else "CONSISTENT",
        }
    else:
        result["p2_scope"] = {
            "analyze_files": sorted(analyze_norm),
            "patch_files": sorted(patch_norm),
            "drift": False,
            "verdict": "NOT_APPLICABLE" if not analyze_norm else "NO_PATCH",
        }

    # P3: Scope justification
    result["p3_justification"] = {
        "root_cause_location_files": analyze_files,
        "mechanism_path": pr.mechanism_path,
        "rejected_nearby_files": pr.rejected_nearby_files,
        "score": verdict.scores.get("scope_justification", None),
        "single_file": len(analyze_files) == 1,
        "has_mechanism": len(pr.mechanism_path) >= 2,
        "has_rejected": any(
            isinstance(r, dict) and len((r.get("reason") or "").strip()) > 5
            for r in pr.rejected_nearby_files
        ),
    }

    # Route decision: what would happen
    route_changes = []
    if result["p2_scope"].get("drift"):
        route_changes.append("P2: RETRYABLE → route to DECIDE (scope drift)")
    if verdict.scores.get("scope_justification", 1.0) < 0.5:
        route_changes.append("P3: would RETRYABLE if hard (insufficient scope justification)")
    result["route_delta"] = route_changes if route_changes else ["no routing change"]

    return result


# ── S3 download helper ───────────────────────────────────────────────────────

def _download_from_s3(batch_name: str, instance_id: str, attempt: int = 1) -> str | None:
    """Download traj data from S3 to a temp directory. Returns local path."""
    import subprocess
    s3_prefix = f"s3://jingu-swebench-results/{batch_name}/{instance_id}/attempt_{attempt}/"
    local_dir = os.path.join(tempfile.gettempdir(), "replay", batch_name, instance_id, f"attempt_{attempt}")
    os.makedirs(local_dir, exist_ok=True)

    # Download checkpoints
    r = subprocess.run(
        ["aws", "s3", "cp", s3_prefix, local_dir, "--recursive"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"  [warn] S3 download failed for {instance_id}: {r.stderr.strip()}")
        return None
    return local_dir


def _download_predictions(batch_name: str) -> str | None:
    """Download predictions JSONL from S3."""
    import subprocess
    s3_path = f"s3://jingu-swebench-results/{batch_name}/jingu-predictions.jsonl"
    local_path = os.path.join(tempfile.gettempdir(), "replay", batch_name, "predictions.jsonl")
    os.makedirs(os.path.dirname(local_path), exist_ok=True)

    r = subprocess.run(
        ["aws", "s3", "cp", s3_path, local_path],
        capture_output=True, text=True,
    )
    return local_path if r.returncode == 0 else None


# ── CLI ──────────────────────────────────────────────────────────────────────

def cmd_gate(args):
    """Replay gates on specific instances."""
    results = []

    if args.local_dir:
        # Single local directory
        cp = _latest_checkpoint(args.local_dir)
        if not cp:
            print(f"No checkpoint found in {args.local_dir}")
            return
        instance_id = cp.get("instance_id", os.path.basename(os.path.dirname(args.local_dir)))
        # Try to find patch files from predictions
        pred_path = os.path.join(os.path.dirname(os.path.dirname(args.local_dir)), "predictions.jsonl")
        patch_files = _extract_patch_files_from_prediction(pred_path, instance_id)
        result = replay_gates(cp, patch_files, instance_id)
        results.append(result)
    else:
        # Download from S3
        batch = args.batch
        instances = args.instances
        pred_path = _download_predictions(batch)

        for inst in instances:
            print(f"  downloading {inst}...", end="", flush=True)
            local_dir = _download_from_s3(batch, inst, attempt=args.attempt)
            if not local_dir:
                print(" FAILED")
                continue
            cp = _latest_checkpoint(local_dir)
            if not cp:
                print(" no checkpoint")
                continue
            patch_files = _extract_patch_files_from_prediction(pred_path, inst) if pred_path else []
            result = replay_gates(cp, patch_files, inst)
            results.append(result)
            print(" done")

    # Print results
    print("\n" + "=" * 80)
    print("GATE REPLAY RESULTS")
    print("=" * 80)

    for r in results:
        inst = r["instance_id"]
        print(f"\n{'─' * 60}")
        print(f"Instance: {inst}")

        if "error" in r:
            print(f"  ERROR: {r['error']}")
            continue

        # Analysis gate
        ag = r["analysis_gate"]
        print(f"  Analysis gate: {'PASS' if ag['passed'] else 'FAIL'}")
        if ag["failed_rules"]:
            print(f"    Failed: {ag['failed_rules']}")
        for k, v in sorted(ag["scores"].items()):
            if isinstance(v, float):
                marker = "✓" if v >= 0.5 else "✗"
                print(f"    {marker} {k}: {v:.1f}")

        # P2
        p2 = r["p2_scope"]
        print(f"  P2 scope: {p2['verdict']}")
        print(f"    analyze_files: {p2['analyze_files']}")
        print(f"    patch_files:   {p2['patch_files']}")
        if p2.get("overlap"):
            print(f"    overlap:       {p2['overlap']}")

        # P3
        p3 = r["p3_justification"]
        print(f"  P3 justification: score={p3['score']}")
        print(f"    single_file: {p3['single_file']}")
        print(f"    mechanism_path: {p3['mechanism_path'] or '(empty)'}")
        print(f"    rejected_nearby: {len(p3['rejected_nearby_files'])} entries")

        # Route delta
        print(f"  Route delta: {' | '.join(r['route_delta'])}")

    # Summary table
    if len(results) > 1:
        print(f"\n{'=' * 80}")
        print("SUMMARY TABLE")
        print(f"{'Instance':<40} {'P2':<15} {'P3 score':<10} {'Route delta'}")
        print(f"{'─' * 40} {'─' * 15} {'─' * 10} {'─' * 30}")
        for r in results:
            inst = r["instance_id"].replace("django__django-", "")
            p2 = r.get("p2_scope", {}).get("verdict", "?")
            p3 = r.get("p3_justification", {}).get("score", "?")
            p3_str = f"{p3:.1f}" if isinstance(p3, float) else str(p3)
            delta = " | ".join(r.get("route_delta", ["?"]))
            print(f"  {inst:<38} {p2:<15} {p3_str:<10} {delta}")


def cmd_table(args):
    """Generate a full batch replay table."""
    batch = args.batch
    pred_path = _download_predictions(batch)

    # List all instances in the batch
    import subprocess
    r = subprocess.run(
        ["aws", "s3", "ls", f"s3://jingu-swebench-results/{batch}/django__django-"],
        capture_output=True, text=True,
    )
    instances = []
    for line in r.stdout.strip().split("\n"):
        line = line.strip()
        if line.startswith("PRE "):
            inst = line[4:].rstrip("/")
            instances.append(inst)

    if not instances:
        print(f"No instances found in batch {batch}")
        return

    print(f"Found {len(instances)} instances in {batch}")
    results = []
    for inst in instances:
        print(f"  {inst}...", end="", flush=True)
        local_dir = _download_from_s3(batch, inst, attempt=args.attempt)
        if not local_dir:
            print(" FAILED")
            continue
        cp = _latest_checkpoint(local_dir)
        if not cp:
            print(" no checkpoint")
            continue
        patch_files = _extract_patch_files_from_prediction(pred_path, inst) if pred_path else []
        result = replay_gates(cp, patch_files, inst)
        results.append(result)
        print(" done")

    # Print table
    cmd_gate_print_table(results)


def cmd_gate_print_table(results):
    """Print the summary table for gate replay results."""
    print(f"\n{'=' * 100}")
    print(f"{'Instance':<30} {'P2':<12} {'P3':<6} {'CG':<5} {'CC':<5} {'RST':<5} {'SJ':<5} {'Route delta'}")
    print(f"{'─' * 30} {'─' * 12} {'─' * 6} {'─' * 5} {'─' * 5} {'─' * 5} {'─' * 5} {'─' * 30}")
    for r in results:
        inst = r["instance_id"].replace("django__django-", "")
        p2 = r.get("p2_scope", {}).get("verdict", "?")
        scores = r.get("analysis_gate", {}).get("scores", {})
        p3 = scores.get("scope_justification", "?")
        cg = scores.get("code_grounding", "?")
        cc = scores.get("causal_chain", "?")
        rst = scores.get("repair_strategy_type", "?")
        sj = scores.get("scope_justification", "?")

        def _fmt(v):
            return f"{v:.1f}" if isinstance(v, float) else str(v)[:4]

        delta = " | ".join(r.get("route_delta", ["none"]))
        print(f"  {inst:<28} {p2:<12} {_fmt(p3):<6} {_fmt(cg):<5} {_fmt(cc):<5} {_fmt(rst):<5} {_fmt(sj):<5} {delta}")


def main():
    parser = argparse.ArgumentParser(description="Trajectory replay harness for gate verification")
    sub = parser.add_subparsers(dest="command")

    # gate: replay gates on specific instances
    p_gate = sub.add_parser("gate", help="Replay P2/P3 gates on historical trajectories")
    p_gate.add_argument("--batch", help="S3 batch name")
    p_gate.add_argument("--instances", nargs="+", help="Instance IDs to replay")
    p_gate.add_argument("--local-dir", help="Local attempt directory (alternative to S3)")
    p_gate.add_argument("--attempt", type=int, default=1, help="Attempt number (default: 1)")

    # table: full batch replay table
    p_table = sub.add_parser("table", help="Generate full batch gate replay table")
    p_table.add_argument("--batch", required=True, help="S3 batch name")
    p_table.add_argument("--attempt", type=int, default=1, help="Attempt number (default: 1)")

    args = parser.parse_args()

    if args.command == "gate":
        cmd_gate(args)
    elif args.command == "table":
        cmd_table(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
