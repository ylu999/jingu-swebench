#!/usr/bin/env python3
"""Offline Binding Failure Analyzer.

Extracts phase_records (DESIGN target_files, ANALYZE root_cause) from checkpoint
data and cross-references with actual patch files to classify binding failures.

Data sources:
  1. eval_results.json         -> resolved/unresolved ground truth
  2. strategy_log.jsonl        -> files_written_paths (actual patch files)
  3. checkpoints/step_N.json.gz -> phase_records (DESIGN FILES_TO_MODIFY, etc.)
  4. jingu-predictions.jsonl   -> patch diff content

Failure categories:
  A: design-file binding mismatch (declared target_files != actual patch files)
  B: analyze-root-cause-to-patch mismatch (root_cause points elsewhere)
  C: mechanism-direction mismatch (declared mechanism != actual change)
  D: semantic weakening (patch weakens constraints instead of fixing)
  E: oversized/non-minimal (patch touches too many files or too many lines)
  F: not detectable (binding check would not have caught this)

Usage:
  python scripts/analyze_binding_failures.py --batch ladder-sonnet46-full30
"""

import argparse
import gzip
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import boto3

S3_BUCKET = "jingu-swebench-results"


def get_s3():
    return boto3.client("s3")


def download_json(s3, key):
    """Download and parse a JSON file from S3."""
    resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
    return json.loads(resp["Body"].read())


def download_jsonl(s3, key):
    """Download and parse a JSONL file from S3."""
    resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
    lines = resp["Body"].read().decode().strip().split("\n")
    return [json.loads(line) for line in lines if line.strip()]


def download_gzip_json(s3, key):
    """Download and parse a gzipped JSON file from S3."""
    resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
    raw = gzip.decompress(resp["Body"].read())
    return json.loads(raw)


def list_checkpoints(s3, batch, instance_id, attempt):
    """List checkpoint files for an instance attempt, return sorted by step_n."""
    prefix = f"{batch}/{instance_id}/attempt_{attempt}/checkpoints/"
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
    files = []
    for obj in resp.get("Contents", []):
        key = obj["Key"]
        match = re.search(r"step_(\d+)\.json\.gz", key)
        if match:
            files.append((int(match.group(1)), key))
    return sorted(files, key=lambda x: x[0])


def get_latest_checkpoint(s3, batch, instance_id):
    """Get the latest checkpoint (highest step_n) across all attempts."""
    # Try attempts 1, 2, 3
    best = None
    for attempt in range(1, 4):
        checkpoints = list_checkpoints(s3, batch, instance_id, attempt)
        if checkpoints:
            step_n, key = checkpoints[-1]  # highest step
            if best is None or step_n > best[0]:
                best = (step_n, key, attempt)
    return best


def parse_design_target_files(phase_records):
    """Extract FILES_TO_MODIFY from DESIGN phase records."""
    target_files = []
    design_content = None
    for pr in phase_records:
        if pr.get("phase") == "DESIGN":
            content = pr.get("content", "")
            design_content = content
            # Parse FILES_TO_MODIFY line
            match = re.search(r"FILES_TO_MODIFY:\s*(.+?)(?:\n|$)", content)
            if match:
                files_str = match.group(1).strip()
                # Handle comma-separated or space-separated
                for f in re.split(r"[,\s]+", files_str):
                    f = f.strip()
                    if f and not f.startswith("SCOPE") and not f.startswith("IN "):
                        target_files.append(f)
    return target_files, design_content


def parse_analyze_root_cause(phase_records):
    """Extract root_cause info from ANALYZE phase records."""
    for pr in phase_records:
        if pr.get("phase") == "ANALYZE":
            return pr.get("content", "")
    return None


def parse_scope_boundary(design_content):
    """Extract SCOPE_BOUNDARY from design content."""
    if not design_content:
        return None
    match = re.search(r"SCOPE_BOUNDARY:\s*(.+?)(?:INVARIANTS:|$)", design_content, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


def get_patch_diff(predictions, instance_id):
    """Get the model_patch from predictions."""
    for entry in predictions:
        if entry.get("instance_id") == instance_id:
            return entry.get("model_patch", "")
    return ""


def parse_diff_files(patch_text):
    """Extract file paths from a unified diff."""
    files = set()
    for line in patch_text.split("\n"):
        if line.startswith("--- a/") or line.startswith("+++ b/"):
            path = line.split("/", 1)[1] if "/" in line else ""
            if path and path != "/dev/null":
                files.add(path)
    return sorted(files)


def count_diff_lines(patch_text):
    """Count added/removed lines in a diff."""
    added = 0
    removed = 0
    for line in patch_text.split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def classify_failure(design_targets, actual_files, patch_text, design_content,
                     analyze_content, strategy_entry):
    """Classify a failure into categories A-F.

    Returns (category, explanation).
    """
    categories = []

    # Category A: design-file binding mismatch
    if design_targets and actual_files:
        design_set = set(design_targets)
        actual_set = set(actual_files)
        if design_set != actual_set:
            extra = actual_set - design_set
            missing = design_set - actual_set
            overlap = design_set & actual_set
            details = []
            if extra:
                details.append(f"extra_files={list(extra)}")
            if missing:
                details.append(f"missing_files={list(missing)}")
            details.append(f"overlap={len(overlap)}/{len(design_set)}")
            categories.append(("A", f"design-file mismatch: {'; '.join(details)}"))
    elif not design_targets and actual_files:
        categories.append(("A", "no DESIGN record found but files were written"))
    elif design_targets and not actual_files:
        categories.append(("A", "DESIGN declared targets but no files written"))

    # Category B: analyze-root-cause-to-patch mismatch
    # Check if analyze mentions different files than what was patched
    if analyze_content and actual_files:
        analyze_files_mentioned = set()
        for f in actual_files:
            basename = os.path.basename(f)
            if basename in analyze_content:
                analyze_files_mentioned.add(f)
        if not analyze_files_mentioned and actual_files:
            categories.append(("B", f"analyze root_cause doesn't mention any patched file"))

    # Category C: mechanism-direction mismatch
    # Check if SCOPE_BOUNDARY mentions different approach than actual diff
    scope = parse_scope_boundary(design_content) if design_content else None
    if scope and "OUT OF SCOPE" in scope:
        out_of_scope_section = scope.split("OUT OF SCOPE:")[-1].strip() if "OUT OF SCOPE:" in scope else ""
        # Check if any actual file is in OUT OF SCOPE
        for f in actual_files:
            basename = os.path.basename(f)
            if basename in out_of_scope_section:
                categories.append(("C", f"patched {f} which was declared OUT OF SCOPE"))
                break

    # Category D: semantic weakening
    # Look for patterns: removing assertions, weakening conditions, adding try/except
    weakening_patterns = [
        (r"^\-\s*assert\b", "removed assertion"),
        (r"^\-\s*raise\b", "removed raise"),
        (r"^\+\s*try:", "added try/except (potential swallowing)"),
        (r"^\+\s*pass\s*$", "added bare pass"),
    ]
    for pattern, desc in weakening_patterns:
        if re.search(pattern, patch_text, re.MULTILINE):
            categories.append(("D", f"semantic weakening: {desc}"))
            break

    # Category E: oversized/non-minimal
    added, removed = count_diff_lines(patch_text)
    total_changes = added + removed
    if total_changes > 50:
        categories.append(("E", f"oversized patch: {added} added, {removed} removed = {total_changes} lines"))
    if len(actual_files) > 3:
        categories.append(("E", f"non-minimal: {len(actual_files)} files modified"))

    # Category F: not detectable
    if not categories:
        categories.append(("F", "no binding violation detected — failure is in patch logic/correctness"))

    return categories


def analyze_batch(batch_name):
    """Main analysis: extract data, classify failures, output report."""
    s3 = get_s3()
    eval_batch = f"eval-{batch_name}"

    print(f"[analyze] Loading eval results from {eval_batch}/eval_results.json")
    eval_results = download_json(s3, f"{eval_batch}/eval_results.json")
    unresolved = eval_results["unresolved_ids"]
    resolved = eval_results["resolved_ids"]
    print(f"[analyze] {len(resolved)} resolved, {len(unresolved)} unresolved")

    print(f"[analyze] Loading strategy_log from {batch_name}/strategy_log.jsonl")
    strategy_entries = download_jsonl(s3, f"{batch_name}/strategy_log.jsonl")
    # Index by instance_id, keep latest attempt
    strategy_map = {}
    for e in strategy_entries:
        iid = e["instance_id"]
        if iid not in strategy_map or e["attempt_id"] > strategy_map[iid]["attempt_id"]:
            strategy_map[iid] = e

    print(f"[analyze] Loading predictions from {batch_name}/jingu-predictions.jsonl")
    predictions = download_jsonl(s3, f"{batch_name}/jingu-predictions.jsonl")

    print()
    results = []

    for instance_id in sorted(unresolved):
        print(f"{'='*70}")
        print(f"INSTANCE: {instance_id}")
        print(f"{'='*70}")

        # Strategy data
        strat = strategy_map.get(instance_id, {})
        actual_files = strat.get("files_written_paths", [])
        failure_class = strat.get("failure_class", "unknown")
        print(f"  failure_class: {failure_class}")
        print(f"  actual_files: {actual_files}")

        # Checkpoint data
        ckpt_info = get_latest_checkpoint(s3, batch_name, instance_id)
        phase_records = []
        if ckpt_info:
            step_n, ckpt_key, attempt = ckpt_info
            print(f"  checkpoint: attempt_{attempt}/step_{step_n}")
            ckpt = download_gzip_json(s3, ckpt_key)
            phase_records = ckpt.get("phase_records", [])
            print(f"  phase_records: {len(phase_records)}")
            for pr in phase_records:
                print(f"    - {pr.get('phase')}/{pr.get('subtype')}")
        else:
            print(f"  checkpoint: NONE FOUND")

        # Parse DESIGN target files
        design_targets, design_content = parse_design_target_files(phase_records)
        print(f"  design_targets: {design_targets}")

        # Parse ANALYZE root cause
        analyze_content = parse_analyze_root_cause(phase_records)
        if analyze_content:
            print(f"  analyze_content: {analyze_content[:150]}...")

        # Patch diff
        patch_text = get_patch_diff(predictions, instance_id)
        diff_files = parse_diff_files(patch_text)
        added, removed = count_diff_lines(patch_text)
        print(f"  diff_files: {diff_files}")
        print(f"  diff_size: +{added}/-{removed}")

        # Classify
        categories = classify_failure(
            design_targets, actual_files, patch_text,
            design_content, analyze_content, strat
        )
        print(f"  CLASSIFICATION:")
        for cat, explanation in categories:
            print(f"    [{cat}] {explanation}")

        results.append({
            "instance_id": instance_id,
            "failure_class": failure_class,
            "actual_files": actual_files,
            "design_targets": design_targets,
            "diff_files": diff_files,
            "diff_added": added,
            "diff_removed": removed,
            "categories": categories,
            "design_content": (design_content or "")[:500],
            "analyze_content": (analyze_content or "")[:500],
        })
        print()

    # Also analyze a sample of RESOLVED instances for false-positive check
    print(f"\n{'='*70}")
    print("FALSE POSITIVE CHECK — Sampling resolved instances")
    print(f"{'='*70}\n")

    fp_results = []
    for instance_id in sorted(resolved)[:10]:  # sample 10 resolved
        strat = strategy_map.get(instance_id, {})
        actual_files = strat.get("files_written_paths", [])

        ckpt_info = get_latest_checkpoint(s3, batch_name, instance_id)
        phase_records = []
        if ckpt_info:
            step_n, ckpt_key, attempt = ckpt_info
            ckpt = download_gzip_json(s3, ckpt_key)
            phase_records = ckpt.get("phase_records", [])

        design_targets, design_content = parse_design_target_files(phase_records)
        analyze_content = parse_analyze_root_cause(phase_records)
        patch_text = get_patch_diff(predictions, instance_id)
        diff_files = parse_diff_files(patch_text)
        added, removed = count_diff_lines(patch_text)

        categories = classify_failure(
            design_targets, actual_files, patch_text,
            design_content, analyze_content, strat
        )

        would_reject = any(cat in ("A", "B", "C", "D", "E") for cat, _ in categories)
        print(f"  {instance_id}: design={design_targets} actual={actual_files} "
              f"cats={[c for c,_ in categories]} would_reject={would_reject}")

        fp_results.append({
            "instance_id": instance_id,
            "categories": categories,
            "would_reject": would_reject,
        })

    # Summary table
    print(f"\n{'='*70}")
    print("SUMMARY TABLE")
    print(f"{'='*70}\n")

    cat_counts = {"A": 0, "B": 0, "C": 0, "D": 0, "E": 0, "F": 0}
    for r in results:
        for cat, _ in r["categories"]:
            cat_counts[cat] += 1

    fp_count = sum(1 for r in fp_results if r["would_reject"])

    print(f"{'Category':<12} {'Count':<8} {'Description'}")
    print(f"{'-'*60}")
    cat_descs = {
        "A": "Design-file binding mismatch",
        "B": "Analyze-root-cause-to-patch mismatch",
        "C": "Mechanism-direction mismatch (OUT OF SCOPE violation)",
        "D": "Semantic weakening",
        "E": "Oversized / non-minimal patch",
        "F": "Not detectable by binding check",
    }
    for cat in "ABCDEF":
        print(f"  {cat:<10} {cat_counts[cat]:<8} {cat_descs[cat]}")

    print(f"\nFalse positive check: {fp_count}/{len(fp_results)} resolved instances "
          f"would be flagged by binding checks")

    # Recommendation table
    print(f"\n{'='*70}")
    print("RECOMMENDATION TABLE")
    print(f"{'='*70}\n")

    checks = [
        {
            "name": "design_file_binding",
            "category": "A",
            "cases_caught": cat_counts["A"],
            "fp_risk": f"{fp_count}/{len(fp_results)} resolved flagged",
            "impl_cost": "low (compare sets)",
            "description": "Check files_written subset of design.target_files",
        },
        {
            "name": "mechanism_direction_check",
            "category": "C",
            "cases_caught": cat_counts["C"],
            "fp_risk": "low (only checks OUT OF SCOPE)",
            "impl_cost": "low (string match)",
            "description": "Check patch doesn't touch OUT OF SCOPE files",
        },
        {
            "name": "analyze_root_cause_binding",
            "category": "B",
            "cases_caught": cat_counts["B"],
            "fp_risk": "medium (analyze may not name files)",
            "impl_cost": "medium (NLP-like matching)",
            "description": "Check analyze root_cause mentions patched files",
        },
        {
            "name": "semantic_weakening_detector",
            "category": "D",
            "cases_caught": cat_counts["D"],
            "fp_risk": "high (legitimate removals)",
            "impl_cost": "medium (regex on diff)",
            "description": "Detect removed assertions/raises in patch",
        },
        {
            "name": "patch_size_gate",
            "category": "E",
            "cases_caught": cat_counts["E"],
            "fp_risk": "medium (some issues need large patches)",
            "impl_cost": "trivial",
            "description": "Reject patches > N lines or > M files",
        },
    ]

    print(f"{'Check':<30} {'Caught':<8} {'FP Risk':<35} {'Cost':<12}")
    print(f"{'-'*85}")
    for c in checks:
        print(f"  {c['name']:<28} {c['cases_caught']:<8} {c['fp_risk']:<35} {c['impl_cost']:<12}")

    print(f"\nTotal unresolved: {len(results)}")
    print(f"Category F (not detectable): {cat_counts['F']}")
    detectable = len(results) - cat_counts["F"]
    print(f"Potentially detectable: {detectable}/{len(results)}")

    # Write full results to JSON
    output_path = f"/tmp/binding_analysis_{batch_name}.json"
    with open(output_path, "w") as f:
        json.dump({
            "batch": batch_name,
            "unresolved_count": len(results),
            "resolved_sampled": len(fp_results),
            "category_counts": cat_counts,
            "false_positive_count": fp_count,
            "instances": results,
            "false_positive_check": fp_results,
        }, f, indent=2, default=str)
    print(f"\nFull results written to: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Offline Binding Failure Analyzer")
    parser.add_argument("--batch", required=True, help="Batch name (e.g. ladder-sonnet46-full30)")
    args = parser.parse_args()
    analyze_batch(args.batch)


if __name__ == "__main__":
    main()
