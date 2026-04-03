"""
smoke_checkpoint.py — phase/checkpoint smoke test monitor

Usage (local, via SSM):
  python3 smoke_checkpoint.py --instance-id i-xxx --log /root/results/smoke-p179.log

Checkpoints:
  A. infra  — instance ready, SSM ready, container ready
  B. agent  — step loop started, first LLM step
  C. progress — first file write detected (inner-verify trigger)
  D. verify — controlled_verify emitted with tests_passed != -1
  E. done   — report saved
"""
import re, sys, time, subprocess

CHECKPOINTS = [
    ("A1", "infra",    r"\[preflight\] ALL CHECKS PASSED"),
    ("A2", "infra",    r"\[jingu\] START"),
    ("A3", "infra",    r"\[inner-verify\] container ready"),
    ("B1", "agent",    r"\[step 1\]"),
    ("B2", "agent",    r"\[step [5-9]\]|\[step [0-9]{2}\]"),
    ("C1", "progress", r"\[inner-verify\] triggering verify at step="),
    ("D1", "verify",   r"tests_passed=[0-9]+"),
    ("D2", "verify",   r"delta=[+-]?[0-9]+"),
    ("E1", "done",     r"report saved"),
]

def check_log(log_path):
    try:
        with open(log_path) as f:
            content = f.read()
        return content
    except:
        return ""

def run_monitor(log_path, timeout_s=900):
    seen = set()
    start = time.time()
    print(f"[smoke-monitor] watching {log_path}")
    print(f"[smoke-monitor] timeout={timeout_s}s")
    print()

    while time.time() - start < timeout_s:
        content = check_log(log_path)
        elapsed = int(time.time() - start)

        for cp_id, phase, pattern in CHECKPOINTS:
            if cp_id not in seen and re.search(pattern, content):
                seen.add(cp_id)
                print(f"[smoke] {elapsed:4d}s  phase={phase:<10}  ✓  {cp_id}: {pattern[:50]}", flush=True)

        if "E1" in seen:
            print(f"\n[smoke] COMPLETE in {elapsed}s — all checkpoints reached")
            return True

        # Check for fatal errors
        if "Traceback" in content and "A1" not in seen:
            print(f"[smoke] FATAL: startup error detected")
            print(content[-500:])
            return False

        time.sleep(5)

    elapsed = int(time.time() - start)
    print(f"\n[smoke] TIMEOUT after {elapsed}s")
    print(f"[smoke] reached: {sorted(seen)}")
    missing = [cp for cp, _, _ in CHECKPOINTS if cp not in seen]
    print(f"[smoke] missing: {missing}")
    return False

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--log", default="/root/results/smoke-p179.log")
    p.add_argument("--timeout", type=int, default=900)
    args = p.parse_args()
    ok = run_monitor(args.log, args.timeout)
    sys.exit(0 if ok else 1)
