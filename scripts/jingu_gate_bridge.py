"""
jingu_gate_bridge.py — Python bridge to jingu-trust-gate (TS) via subprocess.

Calls gate_runner.js with patch + trajectory evidence as JSON,
returns a structured GateResult.

B1 stage: post-hoc gate — patch is already generated, we evaluate it
against trajectory evidence extracted from traj.json.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ── Path resolution (env vars for cloud portability) ─────────────────────────

_SCRIPTS_DIR = Path(
    os.environ.get("JINGU_SWEBENCH_SCRIPTS", str(Path(__file__).parent))
)
_GATE_RUNNER = _SCRIPTS_DIR / "gate_runner.js"

# Resolve node executable — prefer mise shim (cloud), fall back to plain 'node' (local)
def _find_node() -> str:
    import shutil
    # Check env override first
    if node_env := os.environ.get("JINGU_NODE_BIN"):
        return node_env
    # Try mise shim (cloud desktop)
    mise_node = Path.home() / ".local/share/mise/shims/node"
    if mise_node.exists():
        return str(mise_node)
    # Fall back to PATH
    return shutil.which("node") or "node"

_NODE_BIN = _find_node()

# NODE_PATH: jingu-trust-gate node_modules (needed on cloud where npm install wasn't run)
# Convention: trust-gate dist lives at $JINGU_TRUST_GATE_DIST, node_modules one level up
_GATE_DIST = os.environ.get("JINGU_TRUST_GATE_DIST") or str(
    Path.home() / "jingu-swebench" / "jingu-trust-gate" / "dist" / "src"
)
_NODE_MODULES_CANDIDATES = []
if _GATE_DIST:
    # e.g. ~/jingu-swebench/jingu-trust-gate/dist/src → ~/jingu-swebench/jingu-trust-gate/node_modules
    candidate = Path(_GATE_DIST).parent.parent / "node_modules"
    if candidate.exists():
        _NODE_MODULES_CANDIDATES.append(str(candidate))

# Env vars passed to gate_runner.js subprocess
_GATE_ENV = {
    **os.environ,
    "JINGU_SWEBENCH_SCRIPTS": str(_SCRIPTS_DIR),
}
if _NODE_MODULES_CANDIDATES:
    existing = os.environ.get("NODE_PATH", "")
    extra = ":".join(_NODE_MODULES_CANDIDATES)
    _GATE_ENV["NODE_PATH"] = f"{extra}:{existing}" if existing else extra


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class GateUnit:
    unit_id: str
    status: str                   # "approved" | "downgraded" | "rejected" | "approved_with_conflict"
    file_path: str
    hunk_header: str
    applied_grades: list[str]
    reason_codes: list[str]
    annotations: list[dict]
    conflict_annotations: list[dict]


@dataclass
class GateExplanation:
    total_units: int
    approved: int
    downgraded: int
    conflicts: int
    rejected: int
    retry_attempts: int
    gate_reason_codes: list[str]


@dataclass
class GateResult:
    ok: bool
    admitted: bool                # True = patch passes gate (approved or downgraded only)
    rejected: bool
    reason_codes: list[str]
    explanation: Optional[GateExplanation]
    admitted_units: list[GateUnit]
    rejected_units: list[GateUnit]
    retry_feedback: Optional[dict]
    error: Optional[str] = None   # set if ok=False (gate runner crashed)

    @property
    def gate_code(self) -> str:
        """Primary reason code for logging."""
        if not self.ok:
            return f"GATE_ERROR"
        if self.admitted:
            if self.explanation and self.explanation.downgraded > 0:
                return "ADMITTED_SPECULATIVE"
            return "ADMITTED"
        codes = self.reason_codes
        return codes[0] if codes else "REJECTED"

    @property
    def retry_hint(self) -> str:
        """Human-readable retry hint from gate feedback."""
        if not self.retry_feedback:
            return ""
        return self.retry_feedback.get("summary", "")


# ── Support pool builder ──────────────────────────────────────────────────────

def build_support_pool(
    patch_text: str,
    traj_path: Optional[Path] = None,
    exit_status: Optional[str] = None,
    apply_success: Optional[bool] = None,
    apply_error: Optional[str] = None,
    jingu_body: Optional[dict] = None,
) -> list[dict]:
    """
    Build the support pool for the gate from available evidence.

    At B1, we use:
    - exit_status from traj.json (was the agent forced to stop?)
    - task_description (always present — ensures task intent in pool)
    - apply_result (if we tried to apply the patch locally)
    - test_output (if available in traj)
    - jingu_body (structured agent behavior summary, if available)
    """
    pool: list[dict] = []
    sup_id = 0

    def _sid() -> str:
        nonlocal sup_id
        sup_id += 1
        return f"sup-{sup_id:03d}"

    # Load traj if provided
    traj: dict = {}
    if traj_path and Path(traj_path).exists():
        try:
            traj = json.loads(Path(traj_path).read_text())
        except (json.JSONDecodeError, OSError):
            pass

    # 1. exit_status support ref
    traj_exit = exit_status or traj.get("info", {}).get("exit_status", "")
    if traj_exit:
        pool.append({
            "id": _sid(),
            "sourceType": "exit_status",
            "sourceId": "traj_exit_status",
            "attributes": {
                "status": traj_exit,
                "submitted": traj_exit == "submitted",
            },
        })

    # 2. apply_result (if caller ran git apply)
    if apply_success is not None:
        pool.append({
            "id": _sid(),
            "sourceType": "apply_result",
            "sourceId": "git_apply_result",
            "attributes": {
                "success": apply_success,
                "error": apply_error or "",
            },
        })

    # 3. task_description (from traj info or fallback empty)
    problem_stmt = ""
    if traj:
        # mini-SWE-agent stores problem_statement in info or in first user message
        problem_stmt = traj.get("info", {}).get("problem_statement", "")
        if not problem_stmt:
            for msg in traj.get("messages", []):
                if msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, str) and len(content) > 20:
                        problem_stmt = content[:500]
                        break
    if problem_stmt:
        pool.append({
            "id": _sid(),
            "sourceType": "task_description",
            "sourceId": "problem_statement",
            "attributes": {
                "excerpt": problem_stmt[:300],
            },
        })

    # 4. test_output — look for test execution results in traj tool outputs
    if traj:
        for msg in reversed(traj.get("messages", [])):
            if msg.get("role") != "tool":
                continue
            content = str(msg.get("content", ""))
            # Look for pytest-style output
            if "PASSED" in content or "FAILED" in content or "passed" in content:
                passed = "FAILED" not in content and "failed" not in content.lower()
                pool.append({
                    "id": _sid(),
                    "sourceType": "test_output",
                    "sourceId": "traj_test_output",
                    "attributes": {
                        "passed": passed,
                        "excerpt": content[:300],
                    },
                })
                break  # one test_output ref is enough

    # 5. jingu_body — structured agent behavior summary (B1+)
    # Also read from traj.json if it was written back there
    body = jingu_body
    if body is None and traj:
        body = traj.get("jingu_body")
    if body and body.get("schema_version") == "jingu-body-v0":
        pool.append({
            "id": _sid(),
            "sourceType": "jingu_body",
            "sourceId": "agent_behavior_summary",
            "attributes": {
                "schema_version": body["schema_version"],
                "exit_status": body.get("exit_status", ""),
                "files_written": body.get("files_written", []),
                "test_ran": body.get("test_results", {}).get("ran_tests", False),
                "test_passed": body.get("test_results", {}).get("last_passed"),
                "patch_files_changed": body.get("patch_summary", {}).get("files_changed", 0),
                "patch_hunks": body.get("patch_summary", {}).get("hunks", 0),
            },
        })

    return pool


# ── Gate runner ───────────────────────────────────────────────────────────────

def run_patch_gate(
    patch_text: str,
    support_pool: Optional[list[dict]] = None,
    proposal_id: str = "patch-proposal",
    options: Optional[dict] = None,
    timeout_s: float = 30.0,
) -> GateResult:
    """
    Run jingu-trust-gate B1 on a patch.

    Args:
        patch_text:    unified diff string
        support_pool:  list of SupportRef dicts (from build_support_pool())
        proposal_id:   identifier for this admission attempt
        options:       Layer 3 gate params override (require_trajectory, max_files_changed)
        timeout_s:     subprocess timeout

    Returns:
        GateResult
    """
    if support_pool is None:
        support_pool = []

    payload = {
        "patch_text": patch_text,
        "support_pool": support_pool,
        "proposal_id": proposal_id,
        "options": options or {},
    }

    try:
        proc = subprocess.run(
            [_NODE_BIN, str(_GATE_RUNNER)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=_GATE_ENV,
        )
    except subprocess.TimeoutExpired:
        return GateResult(
            ok=False, admitted=False, rejected=True,
            reason_codes=["GATE_TIMEOUT"],
            explanation=None,
            admitted_units=[], rejected_units=[],
            retry_feedback=None,
            error=f"gate_runner.js timed out after {timeout_s}s",
        )
    except FileNotFoundError:
        return GateResult(
            ok=False, admitted=False, rejected=True,
            reason_codes=["GATE_NODE_NOT_FOUND"],
            explanation=None,
            admitted_units=[], rejected_units=[],
            retry_feedback=None,
            error=f"node executable not found at '{_NODE_BIN}' — ensure Node.js is installed",
        )

    stdout = proc.stdout.strip()
    if proc.returncode != 0 and not stdout:
        return GateResult(
            ok=False, admitted=False, rejected=True,
            reason_codes=["GATE_RUNNER_CRASH"],
            explanation=None,
            admitted_units=[], rejected_units=[],
            retry_feedback=None,
            error=f"gate_runner.js exited {proc.returncode}: {proc.stderr[:500]}",
        )

    try:
        raw = json.loads(stdout)
    except json.JSONDecodeError as e:
        return GateResult(
            ok=False, admitted=False, rejected=True,
            reason_codes=["GATE_OUTPUT_PARSE_ERROR"],
            explanation=None,
            admitted_units=[], rejected_units=[],
            retry_feedback=None,
            error=f"gate output parse error: {e} — stdout: {stdout[:300]}",
        )

    if not raw.get("ok"):
        err = raw.get("error", "unknown gate error")
        # Distinguish validation errors (expected) from internal crashes
        if "missing or invalid patch_text" in err:
            code = "EMPTY_PATCH"
        elif "gate.admit error" in err or "unhandled error" in err:
            code = "GATE_INTERNAL_ERROR"
        else:
            code = "GATE_ERROR"
        return GateResult(
            ok=False, admitted=False, rejected=True,
            reason_codes=[code],
            explanation=None,
            admitted_units=[], rejected_units=[],
            retry_feedback=None,
            error=err,
        )

    # Parse explanation
    exp_raw = raw.get("explanation", {})
    explanation = GateExplanation(
        total_units=exp_raw.get("totalUnits", 0),
        approved=exp_raw.get("approved", 0),
        downgraded=exp_raw.get("downgraded", 0),
        conflicts=exp_raw.get("conflicts", 0),
        rejected=exp_raw.get("rejected", 0),
        retry_attempts=exp_raw.get("retryAttempts", 1),
        gate_reason_codes=exp_raw.get("gateReasonCodes", []),
    ) if exp_raw else None

    def _parse_unit(u: dict) -> GateUnit:
        return GateUnit(
            unit_id=u.get("unit_id", ""),
            status=u.get("status", ""),
            file_path=u.get("file_path", ""),
            hunk_header=u.get("hunk_header", ""),
            applied_grades=u.get("applied_grades", []),
            reason_codes=u.get("reason_codes", []),
            annotations=u.get("annotations", []),
            conflict_annotations=u.get("conflict_annotations", []),
        )

    return GateResult(
        ok=True,
        admitted=raw.get("admitted", False),
        rejected=raw.get("rejected", True),
        reason_codes=raw.get("reason_codes", []),
        explanation=explanation,
        admitted_units=[_parse_unit(u) for u in raw.get("admitted_units", [])],
        rejected_units=[_parse_unit(u) for u in raw.get("rejected_units", [])],
        retry_feedback=raw.get("retry_feedback"),
    )


# ── Convenience: evaluate patch from traj.json ────────────────────────────────

def evaluate_patch_from_traj(
    patch_text: str,
    traj_path: Optional[Path],
    exit_status: Optional[str] = None,
    proposal_id: str = "patch-proposal",
    options: Optional[dict] = None,
    jingu_body: Optional[dict] = None,
) -> GateResult:
    """
    High-level entry point for run_with_jingu_gate.py.
    Builds support pool from traj.json + jingu_body, then runs gate.
    """
    pool = build_support_pool(
        patch_text=patch_text,
        traj_path=traj_path,
        exit_status=exit_status,
        jingu_body=jingu_body,
    )
    return run_patch_gate(
        patch_text=patch_text,
        support_pool=pool,
        proposal_id=proposal_id,
        options=options,
    )


# ── Smoke test (python scripts/jingu_gate_bridge.py) ─────────────────────────

if __name__ == "__main__":
    sample_patch = """\
diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py
--- a/django/db/models/fields/__init__.py
+++ b/django/db/models/fields/__init__.py
@@ -1,4 +1,5 @@
 from django.db import models
+from django.utils.functional import cached_property

 class Field:
     pass
"""
    pool = build_support_pool(
        patch_text=sample_patch,
        exit_status="submitted",
    )
    print(f"Support pool: {len(pool)} refs")
    for s in pool:
        print(f"  {s['sourceType']}: {s['sourceId']} {s.get('attributes', {})}")

    result = run_patch_gate(sample_patch, pool, proposal_id="smoke-test")
    print(f"\nGate result:")
    print(f"  ok={result.ok} admitted={result.admitted} code={result.gate_code}")
    print(f"  reason_codes={result.reason_codes}")
    if result.explanation:
        e = result.explanation
        print(f"  units: total={e.total_units} approved={e.approved} "
              f"downgraded={e.downgraded} rejected={e.rejected}")
    if result.error:
        print(f"  error: {result.error}")
