#!/usr/bin/env python3
"""
Fast local eval: apply patch in Docker container + run FAIL_TO_PASS tests.

This is the FAST FEEDBACK loop (30-60s/instance).
It is NOT ground truth — use sb-cli for final leaderboard evaluation.

Architecture:
  Laptop → SSH → Cloud Desktop → Docker container → git apply + pytest

Usage:
  # Eval predictions file
  python scripts/fast_eval.py \
    --predictions results/run_X/jingu-predictions.jsonl \
    --instance-ids django__django-11039

  # Quick smoke test of one instance
  python scripts/fast_eval.py \
    --predictions results/run_X/jingu-predictions.jsonl \
    --instance-ids django__django-11039 --remote cloud

Output:
  { instance_id: { resolved: bool, output: str, test_cmd: str } }

Principle: Remote compute is an accelerator, not the judge.
           Docker pytest = fast iteration signal, NOT leaderboard ground truth.
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

DATASETS_CACHE: dict = {}

def load_instance(instance_id: str) -> dict:
    if instance_id in DATASETS_CACHE:
        return DATASETS_CACHE[instance_id]
    from datasets import load_dataset
    ds = load_dataset("SWE-bench/SWE-bench_Lite", split="test")
    for inst in ds:
        DATASETS_CACHE[inst["instance_id"]] = dict(inst)
    if instance_id not in DATASETS_CACHE:
        raise ValueError(f"Instance not found: {instance_id}")
    return DATASETS_CACHE[instance_id]


def get_eval_cmd(instance_id: str, instance: dict) -> str:
    """Get the test run command from the harness eval script."""
    try:
        from swebench.harness.test_spec.test_spec import make_test_spec
        spec = make_test_spec(instance)
        for line in spec.eval_script.splitlines():
            if "runtests.py" in line or ("pytest" in line and "test" in line):
                return line.strip()
    except Exception:
        pass
    # Fallback for django
    if "django" in instance_id:
        parts = instance["FAIL_TO_PASS"][0].split(".")
        module = ".".join(parts[:-1]) if len(parts) > 1 else parts[0]
        return f"./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1 {module}"
    return ""


def fast_eval_instance(
    instance_id: str,
    patch: str,
    remote: str = "",
) -> dict:
    """Apply patch in Docker container and run FAIL_TO_PASS tests."""
    instance = load_instance(instance_id)
    test_patch = instance.get("test_patch", "")
    fail_to_pass = instance.get("FAIL_TO_PASS", [])
    eval_cmd = get_eval_cmd(instance_id, instance)

    if not eval_cmd:
        return {"resolved": False, "output": "ERROR: could not determine test command", "instance_id": instance_id}

    # Write patches to temp files (local or remote)
    image = f"sweb.eval.x86_64.{instance_id.replace('__', '__')}:latest"

    # Build the in-container script
    script = f"""set -e
source /opt/miniconda3/etc/profile.d/conda.sh 2>/dev/null || true
conda activate testbed 2>/dev/null || true
cd /testbed

# Apply test patch (adds FAIL_TO_PASS test)
cat > /tmp/test.patch << 'PATCH'
{test_patch}
PATCH
git apply /tmp/test.patch 2>/dev/null || true

# Apply model patch
cat > /tmp/model.patch << 'PATCH'
{patch}
PATCH
git apply /tmp/model.patch

# Run the targeted tests
{eval_cmd}
"""

    # Determine if running local or remote
    if remote:
        # Write script to remote and run
        import base64
        script_b64 = base64.b64encode(script.encode()).decode()
        cmd = [
            "ssh", remote,
            f"echo {script_b64} | base64 -d | docker run --rm -i -w /testbed {image} bash"
        ]
    else:
        cmd = ["docker", "run", "--rm", "-i", "-w", "/testbed", image, "bash"]

    try:
        import subprocess
        result = subprocess.run(
            cmd,
            input=script if not remote else None,
            capture_output=True, text=True, timeout=120,
        )
        output = result.stdout + result.stderr
        # Resolve = exit code 0 (harness convention: non-zero = test failure)
        tests_failed = result.returncode != 0
        resolved = not tests_failed

        return {
            "instance_id": instance_id,
            "resolved": resolved,
            "returncode": result.returncode,
            "test_cmd": eval_cmd,
            "fail_to_pass": fail_to_pass,
            "output_tail": output[-1000:],
        }
    except subprocess.TimeoutExpired:
        return {"instance_id": instance_id, "resolved": False, "output_tail": "TIMEOUT", "test_cmd": eval_cmd}
    except Exception as e:
        return {"instance_id": instance_id, "resolved": False, "output_tail": str(e), "test_cmd": eval_cmd}


def main():
    parser = argparse.ArgumentParser(description="Fast eval: Docker apply+test (NOT ground truth)")
    parser.add_argument("--predictions", required=True)
    parser.add_argument("--instance-ids", nargs="*", default=[])
    parser.add_argument("--remote", default="cloud",
                        help="SSH host for cloud desktop (default: cloud). Empty string = local Docker.")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    # Load predictions
    preds = {}
    for line in Path(args.predictions).read_text().splitlines():
        if line.strip():
            d = json.loads(line)
            preds[d["instance_id"]] = d["model_patch"]

    instance_ids = args.instance_ids or list(preds.keys())
    print(f"[fast-eval] {len(instance_ids)} instances | remote={args.remote or 'local'}")
    print("[fast-eval] NOTE: This is fast feedback, NOT ground truth. Use sb-cli for final eval.\n")

    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = {}

    def _run(iid):
        patch = preds.get(iid, "")
        if not patch:
            return iid, {"instance_id": iid, "resolved": False, "output_tail": "NO_PATCH"}
        return iid, fast_eval_instance(iid, patch, remote=args.remote)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(_run, iid): iid for iid in instance_ids}
        for fut in as_completed(futs):
            iid, r = fut.result()
            results[iid] = r
            status = "✓ resolved" if r["resolved"] else "✗ not resolved"
            print(f"  {iid}: {status}  (rc={r.get('returncode','?')})")
            if not r["resolved"]:
                print(f"    tail: {r.get('output_tail','')[-200:]}")

    resolved = sum(1 for r in results.values() if r["resolved"])
    print(f"\n[fast-eval] {resolved}/{len(results)} resolved (fast signal, NOT leaderboard)")


if __name__ == "__main__":
    main()
