#!/usr/bin/env python3
"""
Single-command experiment runner: launches A/B/C groups, waits, then outputs summary.

Usage:
  python scripts/run_experiment_suite.py
  python scripts/run_experiment_suite.py --groups A B   # run subset
  python scripts/run_experiment_suite.py --instances django__django-11039 django__django-11099

Then go do something else. Come back and read results/summary.md.
"""
import argparse
import subprocess
import time
from datetime import datetime
from pathlib import Path

BASE_INSTANCES = [
    "django__django-11039",
    "django__django-11001",
    "django__django-11019",
    "django__django-11049",
    "django__django-11099",
]

GROUPS = {
    "A": {
        "label": "attempts=1, no stagger",
        "output": "results/mini-swe-baseline",
        "extra_args": ["--max-attempts", "1", "--workers", "4"],
        "timeout": 900,
    },
    "B": {
        "label": "attempts=1, stagger=20",
        "output": "results/group-b",
        "extra_args": ["--max-attempts", "1", "--workers", "4", "--stagger", "20"],
        "timeout": 900,
    },
    "C": {
        "label": "attempts=3, stagger=20",
        "output": "results/group-c",
        "extra_args": ["--max-attempts", "3", "--workers", "4", "--stagger", "20"],
        "timeout": 2400,
    },
}


def launch(gid: str, instances: list[str]) -> tuple:
    cfg = GROUPS[gid]
    log_path = Path(f"/tmp/group-{gid.lower()}.log")
    cmd = (
        ["python", "scripts/run_with_jingu_gate.py"]
        + ["--instance-ids"] + instances
        + ["--output", cfg["output"]]
        + cfg["extra_args"]
    )
    print(f"[launch] Group {gid} ({cfg['label']})  log={log_path}")
    with open(log_path, "w") as f:
        proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT)
    return proc, log_path, cfg["timeout"]


def wait_all(running: dict) -> dict:
    """Wait for all processes. Returns {gid: 'done'|'timeout'}."""
    status = {gid: "running" for gid in running}
    start = {gid: time.time() for gid in running}

    while any(s == "running" for s in status.values()):
        for gid, (proc, _, timeout) in running.items():
            if status[gid] != "running":
                continue
            if proc.poll() is not None:
                status[gid] = "done"
                print(f"[done]    Group {gid}")
            elif time.time() - start[gid] > timeout:
                proc.kill()
                status[gid] = "timeout"
                print(f"[timeout] Group {gid} (killed after {timeout}s)")
        still = sum(1 for s in status.values() if s == "running")
        if still:
            print(f"[waiting] {still} group(s) still running...", flush=True)
            time.sleep(15)

    return status


def write_summary(group_ids: list[str], statuses: dict):
    path = Path("results/summary.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Experiment Summary\n\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"]
    lines.append("## Status\n")
    for gid in group_ids:
        cfg = GROUPS[gid]
        s = statuses.get(gid, "skipped")
        lines.append(f"- Group {gid} ({cfg['label']}): **{s}**")
    lines.append("\n## Results\n```")
    result = subprocess.run(["python", "scripts/compare_groups.py"], capture_output=True, text=True)
    lines.append(result.stdout)
    lines.append("```\n")
    path.write_text("\n".join(lines))
    print(f"[summary] written → {path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--groups", nargs="+", default=list(GROUPS.keys()),
                        choices=list(GROUPS.keys()), help="Which groups to run (default: A B C)")
    parser.add_argument("--instances", nargs="+", default=BASE_INSTANCES,
                        help="Instance IDs to run")
    args = parser.parse_args()

    t0 = time.time()
    running = {}
    for gid in args.groups:
        proc, log_path, timeout = launch(gid, args.instances)
        running[gid] = (proc, log_path, timeout)

    print(f"\nAll {len(running)} group(s) launched. Waiting...\n")
    statuses = wait_all(running)

    print("\n--- Results ---")
    subprocess.run(["python", "scripts/compare_groups.py"])

    write_summary(args.groups, statuses)

    elapsed = int(time.time() - t0)
    print(f"\nDone in {elapsed//60}m {elapsed%60}s. See results/summary.md")


if __name__ == "__main__":
    main()
