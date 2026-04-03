"""
preflight.py — Runner environment invariant checks.

Runs before any batch to verify all execution invariants hold.
FAIL → exit(1) immediately. PASS → proceed.

P-INV-001: Invariant over Instance — verify once, enforce always.
P-INV-002: Fail Fast on Missing Invariants — crash at entry, not mid-run.
"""

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).parent.parent
GATE_DIR = REPO_ROOT / "jingu-trust-gate"
REQUIRED_PATHS = [
    REPO_ROOT / "scripts",
    REPO_ROOT / "scripts" / "gate_runner.js",
    GATE_DIR,
    GATE_DIR / "dist" / "src",
]
REQUIRED_NODE_PACKAGES = ["jingu-protocol"]


def _run(cmd: str, cwd: Path | None = None) -> tuple[int, str, str]:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def check_filesystem() -> tuple[bool, str]:
    for p in REQUIRED_PATHS:
        if not p.exists():
            return False, f"missing required path: {p}"
    return True, "ok"


def check_node() -> tuple[bool, str]:
    code, out, _ = _run("node --version")
    if code != 0:
        return False, "node not installed or not on PATH"

    # Auto-install node_modules if missing (self-healing for cold start)
    nm = GATE_DIR / "node_modules"
    if not nm.exists():
        print("[preflight] node_modules missing — running npm install...")
        code, _, err = _run("npm install --silent", cwd=GATE_DIR)
        if code != 0:
            return False, f"npm install failed: {err[:200]}"

    # Verify each required package is resolvable
    for pkg in REQUIRED_NODE_PACKAGES:
        code, _, err = _run(f'node -e "require(\'{pkg}\')"', cwd=GATE_DIR)
        if code != 0:
            return False, f"package not resolvable: {pkg} — {err[:200]}"

    return True, f"node {out}"


def check_python() -> tuple[bool, str]:
    code, out, _ = _run("python3 --version")
    if code != 0:
        return False, "python3 not found"
    return True, out


def check_env() -> tuple[bool, str]:
    # HOME may be empty in SSM — gate_runner.js uses os.homedir() fallback,
    # but log a warning so it's visible
    home = os.environ.get("HOME", "")
    if not home:
        print("[preflight] WARNING: HOME env var is empty (SSM session) — JS uses os.homedir() fallback")
    return True, f"HOME={home or '(empty, homedir() fallback active)'}"


def run_preflight(verbose: bool = True) -> None:
    """
    Run all preflight checks. Raises SystemExit(1) on first failure.
    """
    checks = [
        ("filesystem", check_filesystem),
        ("env",        check_env),
        ("node",       check_node),
        ("python",     check_python),
    ]

    if verbose:
        print("[preflight] starting environment invariant checks...")

    for name, fn in checks:
        ok, msg = fn()
        if not ok:
            print(f"[preflight] FAIL [{name}]: {msg}", file=sys.stderr)
            raise SystemExit(1)
        if verbose:
            print(f"[preflight] ok   [{name}]: {msg}")

    if verbose:
        print("[preflight] ALL CHECKS PASSED\n")


if __name__ == "__main__":
    run_preflight()
