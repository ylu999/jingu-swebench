"""jingu_process_instance — drop-in replacement for minisweagent's process_instance().

Investigation result (2026-04-11, p225-03):
  minisweagent.run.benchmarks.swebench.process_instance() has a fixed 4-parameter
  signature: (instance, output_dir, config, progress_manager). It does NOT accept
  agent_class=, agent_factory=, or any similar injection parameter.

  ProgressTrackingAgent is hardcoded at line 161 of swebench.py:
      agent = ProgressTrackingAgent(model, env, progress_manager=..., instance_id=..., **config.get("agent", {}))

  DefaultAgent (parent of ProgressTrackingAgent) has no __slots__ or __init_subclass__
  restrictions — subclassing is fully supported.

  Therefore: jingu_process_instance() mirrors process_instance() core logic but accepts
  an agent_class parameter, allowing JinguProgressTrackingAgent (or any other subclass)
  to be injected without monkey-patching.

Integration path:
  run_with_jingu_gate.py's run_agent() currently calls process_instance() and works
  around the lack of agent_class= by monkey-patching DefaultAgent.run and
  ProgressTrackingAgent.step via ScopedPatch. jingu_process_instance() provides a
  cleaner alternative: pass the agent class directly, no monkey-patching needed for
  agent instantiation.

p225-08:
  JinguDefaultAgent overrides run() to call on_attempt_end() immediately after
  super().run() returns — while the container is still alive. This eliminates the
  second ScopedPatch on DefaultAgent.run from run_with_jingu_gate.py.

  Container lifecycle: DefaultAgent.run() does NOT close the env. The env (Docker
  container) goes out of scope after jingu_process_instance()'s try block returns.
  Therefore on_attempt_end() called from JinguDefaultAgent.run() runs before env cleanup.

p225-09:
  JinguAgent.on_attempt_start() contains prompt assembly logic (moved from run_agent()).
  JinguAgent.run_attempt() contains the full attempt execution (moved from run_agent()).
  run_agent() in run_with_jingu_gate.py is now a 3-line compatibility wrapper that
  delegates to JinguAgent.run_attempt() and returns the original 4-tuple interface.
"""

import json
import logging
import re
import subprocess
import traceback
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

from minisweagent.models import get_model
from minisweagent.run.benchmarks.swebench import (
    ProgressTrackingAgent,
    get_sb_environment,
    remove_from_preds_file,
    update_preds_file,
)
from minisweagent.run.benchmarks.utils.batch_progress import RunBatchProgressManager
from minisweagent.utils.log import logger
from canonical_symbols import ALL_PHASES


def check_direction_change(
    prev_files: set[str], curr_files: set[str], failure_type: str,
) -> dict:
    """Check whether agent changed direction after a wrong_direction failure.

    Returns a dict with:
        direction_changed: bool — True if at least one new file was added
        new_files: set[str] — files in curr but not in prev
        overlap: set[str] — files in both
        should_reject: bool — True if direction_changed is False and failure was wrong_direction
    """
    is_wrong_direction = failure_type in ("wrong_direction", "wrong_direction+p216")
    new_files = curr_files - prev_files
    overlap = curr_files & prev_files
    direction_changed = len(new_files) > 0
    return {
        "direction_changed": direction_changed,
        "new_files": new_files,
        "overlap": overlap,
        "should_reject": is_wrong_direction and not direction_changed,
    }


def judge_near_miss_patch(
    prev_files: set[str],
    curr_files: set[str],
    patch: str,
    max_lines: int = 30,
) -> dict:
    """Near-miss scope gate: hard reject patches that violate repair constraints.

    Returns dict with:
        pass: bool — True if patch is within scope
        reject_reason: str | None — reason code if rejected
        metrics: dict — observable metrics for telemetry
    """
    new_files = curr_files - prev_files
    introduced_new_file = len(new_files) > 0 and len(prev_files) > 0

    # Count lines changed (additions + deletions in diff)
    lines_added = 0
    lines_removed = 0
    for line in (patch or "").splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            lines_added += 1
        elif line.startswith("-") and not line.startswith("---"):
            lines_removed += 1
    total_lines = lines_added + lines_removed

    # Heuristic: detect constraint weakening
    # Look for removed lines that contain guard patterns
    constraint_weakened = False
    guard_patterns = ("raise ", "assert ", "if not ", "ValidationError", "ValueError", "TypeError")
    for line in (patch or "").splitlines():
        if line.startswith("-") and not line.startswith("---"):
            stripped = line[1:].strip()
            if any(p in stripped for p in guard_patterns):
                # Check if a corresponding replacement exists (modify vs remove)
                # Simple heuristic: pure removal of guards = weakening
                constraint_weakened = True

    metrics = {
        "files_touched": len(curr_files),
        "total_lines_changed": total_lines,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "introduced_new_file": introduced_new_file,
        "new_files": sorted(new_files) if new_files else [],
        "constraint_weakened": constraint_weakened,
    }

    if introduced_new_file:
        return {"pass": False, "reject_reason": "near_miss_new_file", "metrics": metrics}

    if total_lines > max_lines:
        return {"pass": False, "reject_reason": "near_miss_patch_too_large", "metrics": metrics}

    # constraint_weakened is a soft signal for now (warn, not reject)
    # because the heuristic has false positives (legitimate guard modifications)
    return {"pass": True, "reject_reason": None, "metrics": metrics}


def build_recovery_escalation_prompt(banned_files: set[str], violation_count: int) -> str:
    """Build an escalated recovery prompt when file-ban violations accumulate.

    Instead of just saying "don't write this file", provides the structured
    direction search protocol to guide the agent toward alternatives.
    """
    banned_list = ", ".join(sorted(banned_files))
    return (
        f"⚠️ DIRECTION SEARCH REQUIRED (violation #{violation_count}) ⚠️\n\n"
        f"You have written to BANNED file(s) {violation_count} time(s).\n"
        f"BANNED FILES: {banned_list}\n\n"
        "You MUST follow the direction search protocol:\n"
        "1. STOP modifying the banned files immediately.\n"
        "2. Think about what OTHER code paths could cause this bug.\n"
        "3. Generate at least 2 alternative hypotheses:\n"
        "   For each: (a) root cause, (b) candidate files (NOT banned), (c) evidence\n"
        "4. Select the most promising hypothesis and modify THOSE files instead.\n\n"
        "The bug might be in: a different model method, a manager, a queryset operation,\n"
        "a signal handler, a middleware, or a utility function that the banned file calls.\n"
        "Look at the CALLERS of the banned file, or look at PARALLEL implementations.\n\n"
        f"DO NOT touch: {banned_list}\n"
        "DO: read the traceback, find alternative code paths, modify a different file."
    )


def derive_candidate_files(
    instance: dict,
    cv_result: dict | None = None,
    verify_history: list | None = None,
) -> list[str]:
    """Derive candidate source files from test failures, problem statement, and stack traces.

    Returns a list of candidate file paths (relative to repo root) that the agent
    should focus on. Used by:
    - wrong_direction retry: suggest concrete files when overlap=0.0
    - near-miss finisher: narrow scope to relevant files

    Signal sources (in priority order):
    1. Stack traces in test stdout (File "..." references)
    2. Module paths from f2p_failing_names (dotted test paths → source modules)
    3. File paths mentioned in problem_statement
    """
    import re
    candidates: dict[str, int] = {}  # path → relevance score

    # Source 1: Stack traces from verify_history stdout
    if verify_history:
        for vh_entry in verify_history:
            stdout = vh_entry.get("stdout", "") or ""
            # Match Python traceback file references: File "/testbed/django/utils/html.py", line 42
            for m in re.finditer(r'File "/testbed/([^"]+\.py)"', stdout):
                fpath = m.group(1)
                # Skip test files — we want source files
                if "/tests/" not in fpath and "/test_" not in fpath.split("/")[-1]:
                    candidates[fpath] = candidates.get(fpath, 0) + 3

    # Source 2: f2p_failing_names from CV → infer source modules
    if cv_result:
        for test_name in cv_result.get("f2p_failing_names", []) or []:
            # Extract dotted module path: "template_tests.filter_tests.test_title.TitleTests"
            # → source might be django/template/defaultfilters.py
            parts = test_name.split(".")
            if len(parts) >= 2:
                # Try to map test module to source module
                # e.g., "auth_tests.test_forms" → "django/contrib/auth/forms.py"
                # This is a heuristic — not all mappings are correct
                test_mod = parts[0]  # e.g., "model_forms" or "auth_tests"
                # Strip "_tests" / "_test" suffix to guess source module
                source_mod = re.sub(r'_tests?$', '', test_mod)
                if source_mod != test_mod:
                    candidates[f"(module:{source_mod})"] = candidates.get(f"(module:{source_mod})", 0) + 1

        # Also check stdout from CV for tracebacks
        for key in ("stdout", "stderr"):
            text = cv_result.get(key, "") or ""
            for m in re.finditer(r'File "/testbed/([^"]+\.py)"', text):
                fpath = m.group(1)
                if "/tests/" not in fpath and "/test_" not in fpath.split("/")[-1]:
                    candidates[fpath] = candidates.get(fpath, 0) + 3

    # Source 3: File paths in problem statement
    problem = instance.get("problem_statement", "")
    for m in re.finditer(r'(?:django|lib)/[\w/]+\.py', problem):
        candidates[m.group(0)] = candidates.get(m.group(0), 0) + 2
    # Also match "in <module>" patterns
    for m in re.finditer(r'(?:File ".*?/)(django/[\w/]+\.py)', problem):
        candidates[m.group(1)] = candidates.get(m.group(1), 0) + 3

    # Sort by score descending, take top 5
    ranked = sorted(candidates.items(), key=lambda x: -x[1])
    return [path for path, _score in ranked[:5]]


def build_near_miss_finisher_prompt(
    f2p_passed: int,
    f2p_failed: int,
    f2p_failing_names: list[str],
    files_written: list[str],
    candidate_files: list[str] | None = None,
) -> str:
    """Build a targeted near-miss finisher prompt.

    This is injected when failure_type=near_miss AND f2p_ratio > 0.9.
    Much more specific than the general residual_gap_repair protocol.
    """
    total = f2p_passed + f2p_failed
    pct = f2p_passed / total * 100 if total > 0 else 0

    parts = [
        "=== NEAR-MISS FINISHER — SURGICAL FIX MODE ===\n"
        f"You are {f2p_passed}/{total} ({pct:.0f}%) — only {f2p_failed} test(s) remain.\n"
        "Your fix direction is CORRECT. Do NOT restart or redesign."
    ]

    if f2p_failing_names:
        parts.append(
            "\nFAILING TESTS (fix ONLY these):\n"
            + "\n".join(f"  - {name}" for name in f2p_failing_names[:10])
        )

    if files_written:
        parts.append(
            f"\nSCOPE LOCK: You may ONLY modify: {', '.join(files_written)}"
        )

    parts.append(
        "\nHARD CONSTRAINTS:\n"
        "1. Do NOT change ANY code that makes the other tests pass — ZERO regression tolerated\n"
        "2. Do NOT introduce new files\n"
        "3. Do NOT broaden, relax, or bypass existing validation/checks\n"
        "4. Make the SMALLEST possible change to fix the remaining test(s)\n"
        "5. Prefer ADDING a condition/branch over REWRITING existing logic\n"
        "6. Read the failing test source code FIRST to understand what it expects"
    )

    parts.append("=== END NEAR-MISS FINISHER ===")
    return "\n\n".join(parts)


def validate_direction_search_record(
    record: dict, banned_files: set[str],
) -> dict:
    """Validate a structured direction-search hypothesis record (WDRG v0.2).

    The agent must submit this record BEFORE any write in A2 wrong_direction.
    Hard validation rules:
      1. alternative_hypotheses must have >= 2 entries
      2. Each hypothesis must have root_cause, candidate_files, evidence (non-empty)
      3. chosen_hypothesis must NOT point to any banned file
      4. why_not_previous must be non-empty
      5. candidate_files across chosen hypothesis must not be a subset of banned_files

    Returns:
        {"admitted": bool, "failures": list[str]}
    """
    failures = []

    # Rule 1: at least 2 hypotheses
    hypotheses = record.get("alternative_hypotheses", [])
    if not isinstance(hypotheses, list) or len(hypotheses) < 2:
        failures.append(
            f"Need >= 2 alternative_hypotheses, got {len(hypotheses) if isinstance(hypotheses, list) else 0}"
        )

    # Rule 2: each hypothesis has required fields
    for i, hyp in enumerate(hypotheses if isinstance(hypotheses, list) else []):
        if not isinstance(hyp, dict):
            failures.append(f"hypothesis[{i}] is not a dict")
            continue
        for field in ("root_cause", "candidate_files", "evidence"):
            val = hyp.get(field)
            if not val:
                failures.append(f"hypothesis[{i}].{field} is empty")
            elif field == "candidate_files" and isinstance(val, list) and len(val) == 0:
                failures.append(f"hypothesis[{i}].candidate_files is empty list")

    # Rule 3: why_not_previous must exist and be non-empty
    why_not = record.get("why_not_previous", "")
    if not why_not or (isinstance(why_not, str) and len(why_not.strip()) < 10):
        failures.append("why_not_previous is missing or too short (need >= 10 chars)")

    # Rule 4: chosen_hypothesis candidate_files must not overlap with banned files
    chosen_idx = record.get("chosen_hypothesis_index")
    if chosen_idx is not None and isinstance(hypotheses, list):
        if isinstance(chosen_idx, int) and 0 <= chosen_idx < len(hypotheses):
            chosen = hypotheses[chosen_idx]
            if isinstance(chosen, dict):
                cand = chosen.get("candidate_files", [])
                if isinstance(cand, list):
                    cand_set = set(cand)
                    banned_hit = cand_set & banned_files
                    if banned_hit:
                        failures.append(
                            f"chosen hypothesis candidate_files overlap with banned: {sorted(banned_hit)}"
                        )
                    # Also check: candidate files must not be subset of banned
                    if cand_set and cand_set <= banned_files:
                        failures.append(
                            "chosen hypothesis candidate_files are ALL banned files"
                        )
        else:
            failures.append(f"chosen_hypothesis_index={chosen_idx} out of range")
    elif chosen_idx is None:
        failures.append("chosen_hypothesis_index is missing")

    # Rule 5: chosen reason must be non-empty
    chosen_reason = record.get("chosen_reason", "")
    if not chosen_reason or (isinstance(chosen_reason, str) and len(chosen_reason.strip()) < 10):
        failures.append("chosen_reason is missing or too short (need >= 10 chars)")

    return {"admitted": len(failures) == 0, "failures": failures}


# JSON schema for direction-search record (used by structured_extract)
DIRECTION_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "why_not_previous": {
            "type": "string",
            "description": "Explain why the previous hypothesis was wrong. What evidence disproved it?",
        },
        "alternative_hypotheses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "root_cause": {
                        "type": "string",
                        "description": "What is actually causing the bug?",
                    },
                    "candidate_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Which file(s) would you modify? Must be DIFFERENT from banned files.",
                    },
                    "evidence": {
                        "type": "string",
                        "description": "What code/behavior supports this hypothesis?",
                    },
                },
                "required": ["root_cause", "candidate_files", "evidence"],
            },
            "description": "At least 2 alternative hypotheses for the bug's root cause.",
        },
        "chosen_hypothesis_index": {
            "type": "integer",
            "description": "0-based index of the chosen hypothesis from alternative_hypotheses.",
        },
        "chosen_reason": {
            "type": "string",
            "description": "Why this hypothesis is more likely than the other(s).",
        },
    },
    "required": [
        "why_not_previous",
        "alternative_hypotheses",
        "chosen_hypothesis_index",
        "chosen_reason",
    ],
}


def build_pre_write_guard_prompt(banned_files: set[str], reject_failures: list[str] | None = None) -> str:
    """Build the pre-write guard message for WDRG v0.2.

    Injected when agent tries to write before submitting an admitted hypothesis record.
    If reject_failures is provided, the previous record submission was rejected.
    """
    banned_list = ", ".join(sorted(banned_files))
    parts = [
        "⛔ WRITE BLOCKED — Direction Search Record Required ⛔\n",
        f"You are in attempt 2 after a wrong_direction failure.",
        f"BANNED FILES (from attempt 1): {banned_list}\n",
        "Before you can write ANY code, you MUST first analyze the problem",
        "and provide a structured direction-search record.\n",
    ]
    if reject_failures:
        parts.append("Your previous record was REJECTED for these reasons:")
        for f in reject_failures:
            parts.append(f"  - {f}")
        parts.append("")

    parts.extend([
        "REQUIRED: Think through the problem, then the system will extract your",
        "hypothesis record. You need to clearly state in your response:",
        "  1. WHY your previous approach was wrong (what evidence disproved it?)",
        "  2. AT LEAST 2 alternative hypotheses, each with:",
        "     - root_cause: what is actually causing the bug",
        "     - candidate_files: which files to modify (NOT banned files)",
        "     - evidence: what supports this hypothesis",
        "  3. WHICH hypothesis you choose and WHY\n",
        "The system will automatically extract and validate your reasoning.",
        "Once validated, your writes will be unblocked.",
        f"\nDO NOT attempt to modify: {banned_list}",
        "DO: analyze the codebase, form hypotheses about alternative root causes.",
    ])
    return "\n".join(parts)


def _parse_fail_to_pass(instance: dict) -> list[str]:
    """Parse FAIL_TO_PASS from instance dict, handling both list and JSON-string formats."""
    raw = instance.get("FAIL_TO_PASS", [])
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return []


def _extract_approach_summary(jingu_body: dict | None, patch: str, fp: dict) -> str:
    """Extract a short summary of the approach direction from this attempt.

    Uses: files changed + root cause from phase records (if available).
    This is a deterministic extraction — no LLM call.
    """
    parts = []
    # Files changed
    files = fp.get("files", []) if fp else []
    if files:
        parts.append(f"files={','.join(sorted(files))}")

    # Root cause from ANALYZE phase record
    if jingu_body:
        phase_recs = jingu_body.get("phase_records", [])
        analyze_rec = next((r for r in phase_recs if r.get("phase") == "ANALYZE"), None)
        if analyze_rec and analyze_rec.get("root_cause"):
            rc = analyze_rec["root_cause"][:100]
            parts.append(f"root_cause={rc}")

    return " | ".join(parts) if parts else ""


# Type alias for agent classes that are compatible with process_instance flow.
# Must accept (model, env, *, progress_manager, instance_id, **agent_config).
AgentClass = type

# PR1: bundle activation proof — module-level so run_report.json can access it
_bundle_activation_proof: dict = {"bundle_loaded": "not_yet_attempted"}

# ---------------------------------------------------------------------------
# AttemptResult / AttemptOutcome — return types for JinguAgent.run_attempt()
# ---------------------------------------------------------------------------

@dataclass
class AttemptResult:
    """Holds all outputs from a single attempt execution.

    Produced by JinguAgent.run_attempt(); consumed by run_agent() compatibility
    wrapper which extracts the 4-tuple (patch, exit_status, jingu_body, monitor).
    """

    patch: str | None
    exit_status: str | None
    jingu_body: dict | None
    monitor: Any  # StepMonitorState


@dataclass
class AttemptOutcome:
    """Wraps AttemptResult with attempt metadata.

    Produced by JinguAgent.run_attempt().
    """

    attempt: int
    result: AttemptResult


@dataclass
class InstanceResult:
    """Final result for a full instance run (all attempts).

    Produced by JinguAgent.run(); consumed by run_with_jingu() thin wrapper
    which calls .to_dict() to maintain backward-compatible dict interface.
    """

    instance_id: str
    accepted: bool
    patch: str
    attempts: int
    best_attempt: Optional[int] = None
    score: Optional[float] = None
    gate_code: Optional[str] = None
    gate_reason_codes: list = field(default_factory=list)
    admission_reason: Optional[str] = None
    elapsed_s: float = 0.0
    model_usage: dict = field(default_factory=dict)
    attempts_log: list = field(default_factory=list)
    attempt_delta: Optional[dict] = None
    # Semantic rootcause layer (from failure_classifier.classify_failure_layer)
    failure_layer: Optional[str] = None
    failure_record: Optional[dict] = None
    # Rejection-only fields
    status: Optional[str] = None
    failure_type: Optional[str] = None
    reason: Optional[str] = None

    def to_dict(self) -> dict:
        """Return dict compatible with run_with_jingu() caller expectations."""
        d: dict[str, Any] = {"instance_id": self.instance_id}
        if self.status == "rejected":
            # Onboarding rejection path
            d["status"] = "rejected"
            d["failure_type"] = self.failure_type
            d["reason"] = self.reason
            d["patch"] = ""
            d["accepted"] = False
            return d
        if not self.accepted:
            d["accepted"] = False
            d["patch"] = ""
            d["attempts"] = self.attempts
            d["elapsed_s"] = self.elapsed_s
            d["model_usage"] = self.model_usage
            d["attempts_log"] = self.attempts_log
            d["attempt_delta"] = self.attempt_delta
            d["failure_layer"] = self.failure_layer
            d["failure_record"] = self.failure_record
            return d
        d["accepted"] = True
        d["patch"] = self.patch
        d["attempts"] = self.attempts
        d["best_attempt"] = self.best_attempt
        d["score"] = self.score
        d["gate_code"] = self.gate_code
        d["gate_reason_codes"] = self.gate_reason_codes
        d["admission_reason"] = self.admission_reason
        d["elapsed_s"] = self.elapsed_s
        d["model_usage"] = self.model_usage
        d["attempts_log"] = self.attempts_log
        d["attempt_delta"] = self.attempt_delta
        return d


def jingu_process_instance(
    instance: dict,
    output_dir: Path,
    config: dict,
    progress_manager: RunBatchProgressManager,
    *,
    agent_class: AgentClass = ProgressTrackingAgent,
    agent_kwargs: dict[str, Any] | None = None,
) -> None:
    """Process a single SWE-bench instance with injectable agent class.

    This is a mirror of minisweagent's process_instance() (swebench.py:136-191)
    with one key difference: the agent class is a parameter, not hardcoded.

    Args:
        instance: SWE-bench instance dict (must have 'instance_id', 'problem_statement').
        output_dir: Root output directory. Instance artifacts go to output_dir/instance_id/.
        config: Agent config dict (model, agent, environment sections).
        progress_manager: RunBatchProgressManager for status updates.
        agent_class: Agent class to instantiate. Must accept the same constructor
            signature as ProgressTrackingAgent: (model, env, *, progress_manager,
            instance_id, **config.get("agent", {})). Defaults to ProgressTrackingAgent.
        agent_kwargs: Extra keyword arguments passed to agent_class constructor
            (merged with config.get("agent", {})). Optional.
    """
    instance_id = instance["instance_id"]
    instance_dir = output_dir / instance_id

    # Clean up any leftover state from previous runs (same as process_instance)
    remove_from_preds_file(output_dir / "preds.json", instance_id)
    (instance_dir / f"{instance_id}.traj.json").unlink(missing_ok=True)

    model = get_model(config=config.get("model", {}))
    task = instance["problem_statement"]

    progress_manager.on_instance_start(instance_id)
    progress_manager.update_instance_status(instance_id, "Pulling/starting environment")

    agent = None
    exit_status = None
    result = None
    extra_info: dict[str, Any] = {}

    try:
        env = get_sb_environment(config, instance)

        # --- KEY DIFFERENCE: agent_class instead of hardcoded ProgressTrackingAgent ---
        merged_agent_config = dict(config.get("agent", {}))
        if agent_kwargs:
            merged_agent_config.update(agent_kwargs)

        agent = agent_class(
            model,
            env,
            progress_manager=progress_manager,
            instance_id=instance_id,
            **merged_agent_config,
        )
        info = agent.run(task)
        exit_status = info.get("exit_status")
        result = info.get("submission")

    except Exception as e:
        logger.error(f"Error processing instance {instance_id}: {e}", exc_info=True)
        exit_status, result = type(e).__name__, ""
        extra_info = {"traceback": traceback.format_exc(), "exception_str": str(e)}

    finally:
        if agent is not None:
            traj_path = instance_dir / f"{instance_id}.traj.json"
            agent.save(
                traj_path,
                {
                    "info": {
                        "exit_status": exit_status,
                        "submission": result,
                        **extra_info,
                    },
                    "instance_id": instance_id,
                },
            )
            logger.info(f"Saved trajectory to '{traj_path}'")

        update_preds_file(
            output_dir / "preds.json", instance_id, model.config.model_name, result
        )
        progress_manager.on_instance_end(instance_id, exit_status)


# ---------------------------------------------------------------------------
# StepDecision — return type for JinguAgent.on_step_end()
# ---------------------------------------------------------------------------

@dataclass
class StepDecision:
    """Decision returned by JinguAgent.on_step_end() to control agent flow.

    action:
        "continue" — proceed normally (default).
        "redirect" — inject *message* into agent conversation and continue.
        "stop"     — raise StopExecution with *reason*.
    target_phase: optional phase hint when redirecting (e.g. "EXECUTE").
    reason: short machine-readable reason (used for StopExecution / logging).
    message: user-role message injected when action="redirect".
    """

    action: Literal["continue", "redirect", "stop"]
    target_phase: str | None = None
    reason: str = ""
    message: str = ""


# ---------------------------------------------------------------------------
# JinguAgent — orchestration skeleton (hooks + attempt/run lifecycle)
# ---------------------------------------------------------------------------

class JinguAgent:
    """Orchestrates governance hooks around a minisweagent agent.

    Lifecycle:
        run()  →  for each attempt:
            on_attempt_start()  →  run_attempt()  →  on_attempt_end()
        Inside run_attempt(), the agent calls step() repeatedly;
        JinguProgressTrackingAgent delegates to on_step_start / on_step_end.
    """

    def __init__(
        self,
        instance: dict,
        output_dir: Path,
        governance: Any,
        *,
        mode: str = "jingu",
        max_attempts: int = 3,
    ):
        self._instance = instance
        self._output_dir = output_dir
        self._governance = governance
        self._mode = mode
        self._max_attempts = max_attempts
        self._cp_state_holder: list[Any] = []
        self._state: Any | None = None  # StepMonitorState, populated in run()
        self._step_emitter: Any | None = None  # StepEventEmitter, per-attempt
        self._step_start_ts: float = 0.0  # ms timestamp set in on_step_start
        self._decision_logger: Any | None = None  # DecisionLogger, per-attempt (p230)
        self._prompt_sections: list[dict] = []  # p231: prompt sections from p229 snapshot
        self._prev_phase_records_count: int = 0  # p231: track phase_records length for checkpoint trigger
        # File-ban enforcement for wrong_direction compliance
        self._file_ban_active: bool = False
        self._file_ban_files: set[str] = set()
        self._file_ban_violations: int = 0
        self._file_ban_max_violations: int = 2  # escalate to stop after N violations
        # WDRG v0.2: direction-search contract — pre-write guard
        self._direction_search_required: bool = False  # True when A2 wrong_direction
        self._direction_search_admitted: bool = False   # True after hypothesis record passes validation
        self._direction_search_attempts: int = 0        # how many extraction attempts
        self._direction_search_record: dict | None = None  # last extracted record
        self._direction_search_last_failures: list[str] = []  # last validation failures
        self._admitted_target_files: set[str] = set()  # v0.3: allowed write set from admitted hypothesis
        self._consecutive_scope_violations: int = 0  # v0.3: consecutive out-of-scope writes
        self._scope_violation_limit: int = 3  # v0.3b: hard stop after this many consecutive
        # Cross-attempt P2P regression names for sentinel priority
        self._prev_p2p_regression_names: list[str] = []

    # -- step-level hooks (called by JinguProgressTrackingAgent.step) --------

    def on_step_start(self, agent_self: Any, step_n: int) -> None:
        """Called before each agent step.

        Runs observation (Section 1) and detects container readiness.
        Stores observation results on self for use by on_step_end().
        """
        import time as _time_mod
        from step_sections import _step_observe

        # p228: record step start time for duration calculation
        self._step_start_ts = _time_mod.time() * 1000

        # Wire monitor state onto agent instance for _step_observe dedup
        if self._state is not None:
            agent_self._jingu_monitor_state = self._state

        text, snippet, env_error = _step_observe(
            agent_self, step_n=step_n, mode=self._mode
        )
        # Stash for on_step_end
        self._last_observe_result = (text, snippet, env_error)

        # P0.4: QJ ack detection — check if agent acknowledged corrective QJ
        if (self._state is not None
                and self._state.quick_judge_history
                and text):
            _last_qj = self._state.quick_judge_history[-1]
            if (_last_qj.get("acknowledged") is None
                    and _last_qj.get("signal_kind") == "corrective"):
                try:
                    from quick_judge import detect_acknowledged
                    from types import SimpleNamespace
                    _qj_ns = SimpleNamespace(**_last_qj)
                    _ack = detect_acknowledged(_qj_ns, text, [])
                    _last_qj["acknowledged"] = _ack
                    if not _ack:
                        self._state._qj_corrective_ignored = True
                        print(f"    [qj-ack] corrective QJ IGNORED by agent", flush=True)
                    else:
                        self._state._qj_corrective_ignored = False
                        print(f"    [qj-ack] corrective QJ acknowledged by agent", flush=True)
                except Exception as _ack_exc:
                    print(f"    [qj-ack] detection error (non-fatal): {_ack_exc}", flush=True)

        # Accumulate text per phase for PhaseRecord extraction (p221)
        if self._state is not None and text:
            _cp = (
                self._cp_state_holder[0]
                if self._cp_state_holder
                else self._state.cp_state
            )
            _phase = str(_cp.phase).upper()
            self._state._phase_accumulated_text[_phase] = (
                self._state._phase_accumulated_text.get(_phase, "") + "\n" + text
            )

        # Container readiness detection
        cid = getattr(getattr(agent_self, "env", None), "container_id", None)
        if cid and self._state is not None and self._state.container_id is None:
            self.on_container_ready(cid)

    def on_step_end(self, agent_self: Any, step_n: int) -> StepDecision:  # noqa: ARG002
        """Called after each agent step.

        Runs sections 2-6 (verify, cp_update, structure, phase_inject, mat_gate).
        Returns a StepDecision to control agent flow.
        """
        from step_sections import (
            _step_verify_if_needed,
            _step_cp_update_and_verdict,
            _step_check_structure,
            _step_inject_phase,
            _check_materialization_gate,
        )
        from step_monitor_state import StopExecution

        # Retrieve observation results from on_step_start
        text, snippet, env_error = getattr(
            self, "_last_observe_result", ("", "", False)
        )

        if self._state is None:
            return StepDecision(action="continue")

        cp_holder = self._cp_state_holder if self._cp_state_holder else None

        # Section 2: verify + quick judge
        patch_non_empty = _step_verify_if_needed(
            agent_self, state=self._state, verify_debounce_s=5.0,
            cp_state_holder=cp_holder,
        )

        # E1: inject quick judge message if pending (system-originated signal)
        _qj_msg = getattr(self._state, '_pending_quick_judge_message', '')
        if _qj_msg:
            agent_self.messages.append({
                "role": "user",
                "content": _qj_msg,
            })
            self._state._pending_quick_judge_message = ""

        # Section 3: cp update + verdict
        try:
            _step_cp_update_and_verdict(
                agent_self,
                state=self._state,
                cp_state_holder=cp_holder,
                env_error_detected=env_error,
                step_patch_non_empty=patch_non_empty,
                latest_assistant_text=text,
            )
        except StopExecution:
            return StepDecision(
                action="stop",
                reason=getattr(self._state.early_stop_verdict, "reason", "no_signal"),
            )

        # Section 5: phase inject (before structure check, matching _monitored_step order)
        _step_inject_phase(agent_self, cp_state_holder=cp_holder, state=self._state)

        # Section 4: structure check
        _step_check_structure(
            agent_self,
            cp_state_holder=cp_holder,
            state=self._state,
            latest_assistant_text=text,
        )

        # Section 6: materialization gate
        _check_materialization_gate(
            agent_self,
            cp_state_holder=cp_holder,
            state=self._state,
            patch_non_empty=patch_non_empty,
        )

        # WDRG v0.2: direction-search pre-write guard + file-ban enforcement
        # When direction_search_required and NOT admitted: block ALL writes and attempt extraction.
        # When admitted: fall back to file-ban enforcement (block only banned file writes).
        if self._file_ban_active and not self._state.early_stop_verdict:
            try:
                import subprocess as _sp_fb
                _fb_cid = self._state.container_id
                if _fb_cid:
                    _fb_base = self._state.instance.get("base_commit", "HEAD")
                    _fb_diff = _sp_fb.run(
                        ["docker", "exec", "-w", "/testbed", _fb_cid,
                         "git", "diff", "--name-only", _fb_base],
                        capture_output=True, text=True, timeout=10,
                    )
                    _fb_changed = set(_fb_diff.stdout.strip().split("\n")) if _fb_diff.stdout.strip() else set()

                    if self._direction_search_required and not self._direction_search_admitted:
                        # Pre-write guard: ANY write is blocked until hypothesis admitted
                        if _fb_changed:
                            self._file_ban_violations += 1
                            # Revert the write by resetting to base commit
                            _sp_fb.run(
                                ["docker", "exec", "-w", "/testbed", _fb_cid,
                                 "git", "checkout", "--", "."],
                                capture_output=True, text=True, timeout=10,
                            )
                            _guard_msg = build_pre_write_guard_prompt(
                                self._file_ban_files,
                                self._direction_search_last_failures or None,
                            )
                            print(f"    [wdrg-v02] WRITE BLOCKED #{self._file_ban_violations}: "
                                  f"hypothesis not admitted, reverted {sorted(_fb_changed)}", flush=True)
                            if not self._state.pending_redirect_hint:
                                self._state.pending_redirect_hint = _guard_msg
                        else:
                            # No write yet — try to extract hypothesis from agent conversation
                            # Only attempt extraction every 3 steps to avoid excessive LLM calls
                            _max_extract_attempts = 5
                            if (step_n >= 3 and step_n % 3 == 0
                                    and self._direction_search_attempts < _max_extract_attempts):
                                self._try_extract_direction_search(agent_self, step_n)
                            # Fallback: if extraction exhausted, auto-admit with file-ban only
                            if (self._direction_search_attempts >= _max_extract_attempts
                                    and not self._direction_search_admitted):
                                self._direction_search_admitted = True
                                print(f"    [wdrg-v02] AUTO-ADMIT: extraction failed {_max_extract_attempts}x, "
                                      f"falling back to file-ban enforcement only", flush=True)
                    else:
                        # v0.3: Post-admission scope-binding enforcement
                        # If admitted_target_files is set, enforce write scope.
                        # If empty (auto-admit fallback), fall back to banned-file-only check.
                        if self._admitted_target_files and _fb_changed:
                            # Check if ALL changed files are in the admitted scope
                            # Use basename matching: admitted "django/db/models/query.py"
                            # should match the same path in git diff output
                            _out_of_scope = _fb_changed - self._admitted_target_files
                            # Also check: out-of-scope files that are also banned = double violation
                            _banned_out = _out_of_scope & self._file_ban_files
                            if _out_of_scope:
                                self._file_ban_violations += 1
                                self._consecutive_scope_violations += 1
                                # Revert ONLY the out-of-scope files (not all files)
                                for _oos_file in sorted(_out_of_scope):
                                    _sp_fb.run(
                                        ["docker", "exec", "-w", "/testbed", _fb_cid,
                                         "git", "checkout", "--", _oos_file],
                                        capture_output=True, text=True, timeout=10,
                                    )
                                _in_scope = _fb_changed & self._admitted_target_files
                                print(f"    [wdrg-v03] SCOPE VIOLATION #{self._file_ban_violations} "
                                      f"(consecutive={self._consecutive_scope_violations}): "
                                      f"reverted {sorted(_out_of_scope)}, "
                                      f"kept {sorted(_in_scope)}", flush=True)
                                if not self._state.pending_redirect_hint:
                                    self._state.pending_redirect_hint = (
                                        f"⚠️ SCOPE VIOLATION: You wrote to files outside your admitted direction.\n"
                                        f"  Reverted: {', '.join(sorted(_out_of_scope))}\n"
                                        f"  Your admitted write scope: {', '.join(sorted(self._admitted_target_files))}\n"
                                        f"  Allowed files kept: {', '.join(sorted(_in_scope)) or '(none)'}\n"
                                        f"  Consecutive violations: {self._consecutive_scope_violations}/{self._scope_violation_limit}\n"
                                        f"Focus on your admitted files ONLY. Do NOT modify files outside the scope."
                                    )
                                # v0.3b: hard stop after consecutive limit
                                if self._consecutive_scope_violations >= self._scope_violation_limit:
                                    print(f"    [wdrg-v03] HARD STOP: {self._scope_violation_limit} consecutive "
                                          f"scope violations — agent cannot follow admitted direction", flush=True)
                                    from control.reasoning_state import VerdictStop
                                    self._state.early_stop_verdict = VerdictStop(
                                        reason="wdrg_scope_violation_limit",
                                    )
                            else:
                                # All writes within scope — reset consecutive counter
                                self._consecutive_scope_violations = 0
                        elif _fb_changed:
                            # Auto-admit fallback: no admitted_target_files, just banned-file check
                            _banned_hit = _fb_changed & self._file_ban_files
                            if _banned_hit:
                                self._file_ban_violations += 1
                                _ban_msg = build_recovery_escalation_prompt(
                                    self._file_ban_files, self._file_ban_violations,
                                )
                                print(f"    [file-ban] VIOLATION #{self._file_ban_violations}: "
                                      f"agent wrote to banned file(s) {sorted(_banned_hit)}", flush=True)
                                if not self._state.pending_redirect_hint:
                                    self._state.pending_redirect_hint = _ban_msg
            except Exception as _fb_exc:
                print(f"    [file-ban] error (non-fatal): {_fb_exc}", flush=True)

        # Determine step decision
        _decision: StepDecision
        if self._state.early_stop_verdict:
            _decision = StepDecision(
                action="stop",
                reason=getattr(self._state.early_stop_verdict, "reason", "no_signal"),
            )
        elif self._state.pending_redirect_hint:
            hint = self._state.pending_redirect_hint
            self._state.pending_redirect_hint = ""
            _decision = StepDecision(action="redirect", message=hint)
        else:
            _decision = StepDecision(action="continue")

        # p228: emit step event (never crashes the run)
        try:
            self._emit_step_event(agent_self, step_n, env_error, patch_non_empty, _decision)
        except Exception:
            pass

        # p231: checkpoint at key decision points (phase_advance, gate_stop, gate_redirect, materialization_gate)
        try:
            self._maybe_save_checkpoint(agent_self, step_n, _decision)
        except Exception:
            pass

        return _decision

    def _try_extract_direction_search(self, agent_self: Any, step_n: int) -> None:
        """Attempt to extract a direction-search hypothesis record from agent conversation.

        Uses structured_extract (separate LLM call) to parse the agent's reasoning
        into a validated hypothesis record. If validation passes, sets
        _direction_search_admitted=True and unblocks writes.

        Called periodically (every 3 steps) when direction_search_required and agent
        hasn't written anything yet (assumed to be in analysis/reasoning phase).
        """
        self._direction_search_attempts += 1
        try:
            # Get the agent's conversation history (last N messages for context)
            messages = getattr(agent_self, "messages", [])
            if not messages or len(messages) < 2:
                print(f"    [wdrg-v02] extraction skip: insufficient messages", flush=True)
                return

            # Use the agent's model for structured extraction
            model = getattr(agent_self, "model", None)
            if model is None or not hasattr(model, "structured_extract"):
                print(f"    [wdrg-v02] extraction skip: model has no structured_extract", flush=True)
                return

            banned_list = ", ".join(sorted(self._file_ban_files))

            # Build accumulated_text from last N assistant messages
            recent_texts = []
            for msg in messages[-10:]:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    recent_texts.append(f"[{role}]: {content}")
            accumulated_text = "\n\n".join(recent_texts)
            if len(accumulated_text) < 50:
                print(f"    [wdrg-v02] extraction skip: accumulated_text too short ({len(accumulated_text)})", flush=True)
                return

            phase_hint = (
                f"The agent is trying to fix a bug after a wrong direction. "
                f"Banned files (from attempt 1): {banned_list}. "
                f"Extract the agent's reasoning into the required structured format."
            )

            # Call structured_extract with correct API signature
            record = model.structured_extract(
                accumulated_text,
                "DIRECTION_SEARCH",
                DIRECTION_SEARCH_SCHEMA,
                phase_hint=phase_hint,
                max_tokens=2048,
            )

            if not isinstance(record, dict):
                print(f"    [wdrg-v02] extraction returned non-dict: {type(record)}", flush=True)
                return

            self._direction_search_record = record

            # Validate the extracted record
            result = validate_direction_search_record(record, self._file_ban_files)
            self._direction_search_last_failures = result["failures"]

            if result["admitted"]:
                self._direction_search_admitted = True
                chosen_idx = record.get("chosen_hypothesis_index", 0)
                hypotheses = record.get("alternative_hypotheses", [])
                chosen = hypotheses[chosen_idx] if 0 <= chosen_idx < len(hypotheses) else {}
                chosen_files = chosen.get("candidate_files", [])
                # v0.3: bind execution to admitted write scope
                self._admitted_target_files = set(chosen_files)
                print(f"    [wdrg-v02] ✓ HYPOTHESIS ADMITTED (attempt {self._direction_search_attempts}): "
                      f"chosen_files={chosen_files}", flush=True)
                # Inject admission confirmation with scope binding notice
                if not self._state.pending_redirect_hint:
                    self._state.pending_redirect_hint = (
                        f"✅ Direction search record ADMITTED. "
                        f"You may now write code.\n"
                        f"ALLOWED WRITE SCOPE: {', '.join(sorted(self._admitted_target_files))}\n"
                        f"⚠️ Writes to files OUTSIDE this scope will be automatically reverted.\n"
                        f"Reason: {record.get('chosen_reason', '')[:200]}"
                    )
            else:
                print(f"    [wdrg-v02] ✗ hypothesis REJECTED (attempt {self._direction_search_attempts}): "
                      f"{result['failures']}", flush=True)
                # Inject rejection feedback
                if not self._state.pending_redirect_hint:
                    self._state.pending_redirect_hint = build_pre_write_guard_prompt(
                        self._file_ban_files, result["failures"],
                    )

        except Exception as _ext_exc:
            print(f"    [wdrg-v02] extraction error (non-fatal): {_ext_exc}", flush=True)

    def _emit_step_event(
        self,
        agent_self: Any,
        step_n: int,
        env_error: bool,
        patch_non_empty: bool,
        decision: StepDecision,
    ) -> None:
        """Build and emit a StepEvent. Called from on_step_end. Never raises to caller."""
        import time as _time_mod
        if self._step_emitter is None:
            return

        from step_event_emitter import StepEvent, extract_tool_usage

        # cp_state snapshot
        cp = self._cp_state_holder[0] if self._cp_state_holder else None
        cp_snapshot = {
            "phase": str(getattr(cp, "phase", None)),
            "step": getattr(cp, "phase_steps", 0),
            "no_progress_steps": getattr(cp, "no_progress_steps", 0),
            "patch_first_write": getattr(cp, "patch_first_write", False),
            "phase_records_count": len(getattr(self._state, "phase_records", [])) if self._state else len(getattr(cp, "phase_records", [])),
        } if cp else None

        # gate verdict from decision
        gate_verdict: str | None = None
        gate_reason: str | None = None
        if decision.action == "stop":
            gate_verdict = "stop"
            gate_reason = decision.reason
        elif decision.action == "redirect":
            gate_verdict = "redirect"
            gate_reason = decision.reason or "redirect"

        # tool usage extraction
        msgs = getattr(agent_self, "messages", [])
        tool_calls_count, files_read, files_written = extract_tool_usage(msgs, step_n)

        now_ms = _time_mod.time() * 1000
        event = StepEvent(
            step_n=step_n,
            timestamp_ms=now_ms,
            phase=str(getattr(cp, "phase", None)) if cp else None,
            gate_verdict=gate_verdict,
            gate_reason=gate_reason,
            cp_state_snapshot=cp_snapshot,
            tool_calls_count=tool_calls_count,
            files_read=files_read,
            files_written=files_written,
            step_duration_ms=now_ms - self._step_start_ts if self._step_start_ts else 0.0,
            patch_non_empty=patch_non_empty,
            env_error=env_error,
        )
        self._step_emitter.emit(event)

    def _maybe_save_checkpoint(
        self, agent_self: Any, step_n: int, decision: StepDecision
    ) -> None:
        """Save a checkpoint if this step is a key decision point (p231).

        Triggers:
        - phase_advance: phase_records count increased since last step
        - gate_stop: decision.action == "stop"
        - gate_redirect: decision.action == "redirect"
        - materialization_gate: detected via decision logger's last event type

        Never raises — all operations wrapped in try/except.
        """
        if self._state is None:
            return

        # Determine trigger type
        trigger: str | None = None
        current_pr_count = len(self._state.phase_records)
        if current_pr_count > self._prev_phase_records_count:
            trigger = "phase_advance"
            self._prev_phase_records_count = current_pr_count
        elif decision.action == "stop":
            trigger = "gate_stop"
        elif decision.action == "redirect":
            trigger = "gate_redirect"

        # Also check for materialization_gate via cp state
        if (
            trigger is None
            and self._state._execute_entry_step >= 0
            and not self._state._execute_write_seen
        ):
            # materialization gate may have fired this step — check decision logger's
            # last event. We detect it by checking if mat gate emitted a decision.
            # Simpler proxy: look at messages for mat-gate injection.
            msgs = getattr(agent_self, "messages", [])
            if msgs and isinstance(msgs[-1], dict):
                last_content = msgs[-1].get("content", "")
                if isinstance(last_content, str) and "[mat-gate]" in last_content.lower():
                    trigger = "materialization_gate"

        if trigger is None:
            return

        from checkpoint import Checkpoint, save_checkpoint

        instance_id = self._instance.get("instance_id", "unknown")
        attempt = self._state.attempt

        # Build cp_state dict
        cp_state_dict = (
            self._state.to_checkpoint_dict()
            if hasattr(self._state, "to_checkpoint_dict")
            else {}
        )

        # Phase records
        phase_records = []
        for pr in (self._state.phase_records or []):
            if isinstance(pr, dict):
                phase_records.append(pr)
            elif hasattr(pr, "__dict__"):
                phase_records.append(vars(pr))
            else:
                phase_records.append({"raw": str(pr)})

        # Pending hints
        pending_hints = []
        if self._state.pending_redirect_hint:
            pending_hints.append(self._state.pending_redirect_hint)

        # Current phase
        cp = self._cp_state_holder[0] if self._cp_state_holder else None
        current_phase = str(getattr(cp, "phase", None)) if cp else "unknown"

        ckpt = Checkpoint(
            step_n=step_n,
            instance_id=instance_id,
            attempt=attempt,
            trigger=trigger,
            messages_so_far=list(getattr(agent_self, "messages", [])),
            cp_state=cp_state_dict,
            phase_records=phase_records,
            pending_hints=pending_hints,
            prompt_sections=self._prompt_sections,
            metadata={
                "timestamp_ms": time.time() * 1000,
                "phase": current_phase,
                "trigger_detail": decision.reason or "",
            },
        )
        result = save_checkpoint(ckpt, self._output_dir / instance_id)
        if result:
            print(
                f"    [p231] checkpoint saved: step={step_n} trigger={trigger}"
                f" phase={current_phase} path={result.name}",
                flush=True,
            )

    # -- design admission gate (in-loop, pre-execution) --------------------

    def generate_design_record(
        self, attempt: int, previous_failure: str = "",
    ) -> "DesignAdmissionResult":
        """Pre-execution DESIGN gate: separate LLM call to produce a DesignRecord.

        Called before run_attempt(). If the design is not admitted, the attempt
        is short-circuited (agent never runs freely).

        Feature flag: DESIGN_ADMISSION_ENABLED=1
        """
        from design_admission import (
            DesignRecord, DesignAdmissionResult,
            build_design_prompt, parse_design_response, validate_design,
            is_design_admission_enabled,
        )
        import time as _time_da

        if not is_design_admission_enabled():
            # Feature disabled — auto-admit with empty record
            print(f"    [design-admission] DISABLED (DESIGN_ADMISSION_ENABLED={__import__('os').environ.get('DESIGN_ADMISSION_ENABLED', '0')})", flush=True)
            return DesignAdmissionResult(admitted=True, record=DesignRecord(), gate_path="disabled")

        instance = self._instance
        instance_id = instance["instance_id"]

        _da_model = __import__("os").environ.get(
            "JINGU_DESIGN_MODEL",
            "bedrock/global.anthropic.claude-sonnet-4-6",
        )
        _da_max_tokens = 2048
        _da_temperature = 0.3

        prompt = build_design_prompt(instance, previous_failure)

        print(
            f"    [design-admission] attempt={attempt} generating design record "
            f"(model={_da_model}, single-shot)",
            flush=True,
        )

        # Single-shot: no internal retry. If the LLM can't produce a valid
        # design on the first try, the attempt is short-circuited.
        # This ensures the gate has real admission power (not softened by retry).
        t0 = _time_da.monotonic()
        try:
            import litellm
            response = litellm.completion(
                model=_da_model,
                messages=[
                    {"role": "user", "content": prompt},
                ],
                max_tokens=_da_max_tokens,
                temperature=_da_temperature,
                drop_params=True,
            )
            raw = response.choices[0].message.content or ""
        except Exception as e:
            print(
                f"    [design-admission] LLM call failed: {e}",
                flush=True,
            )
            return DesignAdmissionResult(
                admitted=True,  # fail-open: don't block on infra errors
                record=DesignRecord(),
                failure_reasons=[f"llm_error:{e}"],
                gate_path="fail_open_on_llm_error",
            )

        elapsed = round((_time_da.monotonic() - t0) * 1000, 1)
        record = parse_design_response(raw)
        result = validate_design(record)

        _gp = "admit_first_try" if result.admitted else "reject_first_try"
        result.gate_path = _gp
        print(
            f"    [design-admission] admitted={result.admitted} "
            f"gate_path={_gp} "
            f"failures={result.failure_reasons} "
            f"target_files={record.target_files} "
            f"principals={record.principals} "
            f"scope_boundary={(record.scope_boundary[:80] if record.scope_boundary else '')!r} "
            f"elapsed_ms={elapsed}",
            flush=True,
        )
        return result

    def generate_dual_hypotheses(
        self, attempt: int, previous_failure: str = "",
    ) -> "DualHypothesisResult":
        """Generate TWO structurally different fix hypotheses via LLM call.

        Feature flag: DHG_ENABLED=1
        Returns DualHypothesisResult with hypothesis_a and hypothesis_b.
        Falls back to single design record if DHG disabled or LLM fails.
        """
        from design_admission import (
            DualHypothesisResult,
            build_dual_hypothesis_prompt, parse_dual_hypothesis_response,
            is_dhg_enabled,
        )
        import time as _time_dhg

        if not is_dhg_enabled():
            print(f"    [dhg] DISABLED (DHG_ENABLED={__import__('os').environ.get('DHG_ENABLED', '0')})", flush=True)
            return DualHypothesisResult(parse_ok=False)

        instance = self._instance

        _dhg_model = __import__("os").environ.get(
            "JINGU_DESIGN_MODEL",
            "bedrock/global.anthropic.claude-sonnet-4-6",
        )
        _dhg_max_tokens = 4096
        _dhg_temperature = 0.7  # higher temp for diversity

        prompt = build_dual_hypothesis_prompt(instance, previous_failure)

        print(
            f"    [dhg] attempt={attempt} generating dual hypotheses "
            f"(model={_dhg_model}, temp=0.7)",
            flush=True,
        )

        t0 = _time_dhg.monotonic()
        try:
            import litellm
            response = litellm.completion(
                model=_dhg_model,
                messages=[
                    {"role": "user", "content": prompt},
                ],
                max_tokens=_dhg_max_tokens,
                temperature=_dhg_temperature,
                drop_params=True,
            )
            raw = response.choices[0].message.content or ""
        except Exception as e:
            print(f"    [dhg] LLM call failed: {e}", flush=True)
            return DualHypothesisResult(parse_ok=False)

        elapsed = round((_time_dhg.monotonic() - t0) * 1000, 1)
        result = parse_dual_hypothesis_response(raw)

        if result.parse_ok:
            print(
                f"    [dhg] parse_ok=True diversity={result.diversity_score:.2f} "
                f"hyp_a_files={result.hypothesis_a.target_files} "
                f"hyp_b_files={result.hypothesis_b.target_files} "
                f"hyp_a_approach={result.hypothesis_a.solution_approach[:60]!r} "
                f"hyp_b_approach={result.hypothesis_b.solution_approach[:60]!r} "
                f"elapsed_ms={elapsed}",
                flush=True,
            )
        else:
            print(
                f"    [dhg] parse_ok=False elapsed_ms={elapsed}",
                flush=True,
            )

        return result

    # -- attempt-level hooks ------------------------------------------------

    def on_attempt_start(self, attempt: int, previous_failure: str | None) -> list[str]:
        """Build the full extra_parts list for this attempt.

        Moved from run_agent() in run_with_jingu_gate.py (p225-09).
        Returns list of prompt parts to join with double-newline.
        """
        extra_parts: list[str] = []

        # jingu-specific constraint: prevent ENVIRONMENT_NOT_AGENT_WORK violations.
        # baseline uses the official prompt without this block.
        if self._mode == "jingu":
            extra_parts.append(
                "## FORBIDDEN ACTIONS\n\n"
                "The following actions are STRICTLY FORBIDDEN. Do NOT do any of these:\n\n"
                "- `pip install`, `pip3 install`, `uv pip install`, `python setup.py install`, `conda install`\n"
                "- `apt install`, `apt-get install`, `dnf install`, `brew install`\n"
                "- Installing or configuring any software or dependencies\n\n"
                "The environment is already fully set up. If something appears missing, "
                "read the existing code more carefully — the solution is always a code change, not an environment change."
            )

        # B4: phase-structured reasoning protocol — p224-09: loaded via compile_bundle().
        # All phase prompts, principal requirements, type contracts, forbidden moves
        # are derived from bundle.json (compiled by jingu-cognition TS). Zero hardcoded strings.
        global _bundle_activation_proof  # PR1: exposed for run_report.json
        _phase_prompt_parts: list[str] = []
        _type_contracts_block = "Type contracts: (see principal_gate for v2.0 contracts)"
        # SST: derive principal requirements from contract_registry (no hardcoded fallback)
        try:
            from contract_registry import get_required_principals as _grp
            _analysis_req = ", ".join(_grp("ANALYZE"))
            _decision_req = ", ".join(_grp("DECIDE"))
            _execute_req  = ", ".join(_grp("EXECUTE"))
        except Exception:
            _analysis_req = ""  # SST2: fallback is empty, not stale copy
            _decision_req = ""
            _execute_req  = ""

        try:
            from bundle_compiler import compile_bundle as _compile_bundle
            import logging as _logging
            _bundle = _compile_bundle()
            _report = _bundle.activation_report
            _logging.getLogger(__name__).info(
                "[jingu-compiler] activation_ok=%s bundle_version=%s compiler_version=%s "
                "generator_commit=%s phases=%s contracts=%d principals=%d "
                "inference_eligible=%d fake_check_eligible=%d warnings=%d",
                _report.activation_ok, _report.bundle_version, _report.compiler_version,
                _report.generator_commit, _report.phases_compiled, _report.contracts_compiled,
                _report.principals_total, _report.principals_inference_eligible,
                _report.principals_fake_check_eligible, len(_report.prompt_warnings),
            )
            _gov_prompt = _bundle.governance
            # Assemble full reasoning protocol from per-phase prompts
            for _pp_phase in _gov_prompt.list_phases():
                _pp_text = _gov_prompt.get_phase_prompt(_pp_phase)
                if _pp_text:
                    _phase_prompt_parts.append(_pp_text)
            # Build type contracts block from gate configs
            _type_contracts_lines = []
            for _pp_phase in _gov_prompt.list_phases():
                _pp_gate = _gov_prompt.get_gate(_pp_phase)
                if _pp_gate:
                    _pp_req = ", ".join(_pp_gate.required_principals)
                    _pp_forb = ", ".join(_pp_gate.forbidden_principals)
                    _pp_forb_str = f"  forbidden=[{_pp_forb}]" if _pp_forb else ""
                    _type_contracts_lines.append(
                        f"  {_pp_gate.subtype.split('.')[-1]:<20} required=[{_pp_req}]{_pp_forb_str}"
                    )
            _type_contracts_block = "Type contracts:\n" + "\n".join(_type_contracts_lines)
            # Per-step principal requirements
            def _get_req(p: str) -> str:
                _g = _gov_prompt.get_gate(p)
                return ", ".join(_g.required_principals) if _g else ""
            _analysis_req = _get_req("ANALYZE")
            _decision_req = _get_req("DECIDE")
            _execute_req  = _get_req("EXECUTE")
            # Activation proof (RT4)
            _bundle_activation_proof = {
                "bundle_loaded": True,
                "bundle_version": _report.bundle_version,
                "compiler_version": _report.compiler_version,
                "phases_compiled": _report.phases_compiled,
                "contracts_compiled": _report.contracts_compiled,
                "principals_total": _report.principals_total,
                "activation_ok": _report.activation_ok,
            }
            print(
                f"    [BUNDLE_ACTIVATED] version={_report.bundle_version} "
                f"phases={_report.phases_compiled} contracts={_report.contracts_compiled} "
                f"principals={_report.principals_total} ok={_report.activation_ok}",
                flush=True,
            )
        except Exception as _onb_exc:
            import traceback as _tb
            _bundle_error_msg = str(_onb_exc)
            _bundle_error_trace = "".join(
                _tb.format_exception(type(_onb_exc), _onb_exc, _onb_exc.__traceback__)
            )
            print(
                f"    [BUNDLE_LOAD_FAILURE] compile_bundle() failed: {_bundle_error_msg}\n"
                f"    {_bundle_error_trace}",
                flush=True,
            )
            _bundle_activation_proof = {
                "bundle_loaded": False,
                "error": _bundle_error_msg,
                "fallback_active": True,
            }

        # If bundle provides compiled phase prompts, use them directly
        if _phase_prompt_parts:
            _combined_prompt = "\n\n".join(_phase_prompt_parts)
            extra_parts.append(
                f"REASONING PROTOCOL (governance system enforces these — follow exactly):\n\n"
                f"{_combined_prompt}\n\n"
                f"{_type_contracts_block}\n\n"
                f"Rules:\n"
                f"  - Output PHASE: markers exactly as shown — the governance system parses them\n"
                f"  - FIX_TYPE must match CLAIMS from your decision step\n"
                f"  - PRINCIPALS must include ALL required for your type, none of the forbidden"
            )
        else:
            # Fallback: hardcoded protocol (pre-bundle)
            extra_parts.append(
                "REASONING PROTOCOL (output these markers as you work — they are parsed by the governance system):\n\n"
                "## STEP 1 — before writing any code, output all three:\n"
                "  PHASE: analysis\n"
                f"  PRINCIPALS: {_analysis_req}\n"
                "  EVIDENCE: <file:line or test name that shows the bug>\n"
                "  ROOT_CAUSE: <the specific line or logic that causes the failure>\n\n"
                "## STEP 2 — once root cause is clear, output:\n"
                "  PHASE: decision\n"
                f"  PRINCIPALS: {_decision_req}\n"
                "  CLAIMS: <chosen fix type — execution | diagnosis | design | planning>\n"
                "  SCOPE: <which files/functions will be changed>\n\n"
                "## STEP 3 — BEFORE writing any code, output these lines first:\n"
                "  PHASE: execution\n"
                f"  PRINCIPALS: {_execute_req}\n"
                "  EVIDENCE: <which analysis step or file:line justified this change>\n"
                "  Then write the patch.\n\n"
                "## STEP 4 — before calling submit, output these two lines exactly:\n"
                "  FIX_TYPE: <one of: understanding | observation | analysis | diagnosis | decision | design | planning | execution | validation>\n"
                "  PRINCIPALS: <space-separated list — must satisfy the contract for your chosen type>\n\n"
                f"{_type_contracts_block}\n\n"
                "Rules:\n"
                "  - Output PHASE: markers exactly as shown — the governance system parses them\n"
                "  - FIX_TYPE must match CLAIMS from STEP 2\n"
                "  - PRINCIPALS must include ALL required for your type, none of the forbidden"
            )

        fail_to_pass = _parse_fail_to_pass(self._instance)
        if fail_to_pass:
            tests_str = "\n".join(f"  - {t}" for t in fail_to_pass[:10])
            extra_parts.append(
                f"IMPORTANT: Your fix must make the following tests pass:\n{tests_str}\n\n"
                f"Run the failing tests FIRST to understand what they expect. "
                f"Fix only the minimal code needed to make the tests pass. "
                f"SUBMIT IMMEDIATELY once these tests pass — do NOT add extra tests, "
                f"demonstration scripts, or comment updates. "
                f"Every step matters — go straight to submission as soon as the required tests pass."
            )
        # ScopeLockGate v0.1: inject scope constraints into A2+ prompt
        if previous_failure and self._scope_lock_envelope is not None:
            from scope_lock import build_scope_lock_prompt_block
            _sl_block = build_scope_lock_prompt_block(self._scope_lock_envelope)
            extra_parts.append(_sl_block)
            print(f"    [scope-lock] prompt block injected: "
                  f"allowed_files={sorted(self._scope_lock_envelope.allowed_files)} "
                  f"size_limit={self._scope_lock_envelope.size_limit}", flush=True)

        if previous_failure:
            extra_parts.append(f"Previous attempt failed: {previous_failure}")

        # p229: prompt assembly snapshot — persist assembled prompt for offline analysis.
        try:
            def _classify_part(idx: int, part: str) -> str:
                if "FORBIDDEN" in part:
                    return "forbidden_actions"
                if "REASONING PROTOCOL" in part:
                    return "reasoning_protocol"
                if "IMPORTANT: Your fix must" in part or "FAIL_TO_PASS" in part:
                    return "fail_to_pass"
                if "Previous attempt failed" in part:
                    return "retry_hint"
                return f"section_{idx}"

            _snap_sections = []
            _snap_total_chars = 0
            for _i, _part in enumerate(extra_parts):
                _snap_sections.append({
                    "name": _classify_part(_i, _part),
                    "char_count": len(_part),
                    "content": _part,
                })
                _snap_total_chars += len(_part)

            _snap_instance_id = self._instance.get("instance_id", "unknown")
            prompt_snapshot = {
                "attempt": attempt,
                "instance_id": _snap_instance_id,
                "mode": self._mode,
                "sections": _snap_sections,
                "has_retry_hint": bool(previous_failure),
                "fail_to_pass_count": len(_parse_fail_to_pass(self._instance)),
                "total_chars": _snap_total_chars,
            }

            _snap_dir = self._output_dir / _snap_instance_id / f"attempt_{attempt}"
            _snap_dir.mkdir(parents=True, exist_ok=True)
            _snap_path = _snap_dir / "prompt_snapshot.json"
            with open(_snap_path, "w") as _snap_f:
                json.dump(prompt_snapshot, _snap_f, indent=2)
            # p231: store prompt sections for checkpoint inclusion
            self._prompt_sections = _snap_sections
        except Exception as _snap_exc:
            logging.getLogger(__name__).warning("[p229] prompt snapshot failed: %s", _snap_exc)

        return extra_parts

    def on_container_ready(self, container_id: str) -> None:
        """Called once when container_id is first observed in on_step_start().

        Sets self._state.container_id so in-loop controlled_verify can begin.
        Equivalent to: container_id injection that was in _verifying_run (pre-p225-08).
        """
        assert self._state is not None
        self._state.container_id = container_id

    def on_attempt_end(self, agent_self: Any, submission: str | None) -> None:
        """Called after each attempt completes (before container is destroyed).

        Runs the end-of-attempt governance checks (previously in the _verifying_run
        closure in run_with_jingu_gate.py, removed in p225-08):
          1. Cognition gate (p187) — fires when cp_state.phase == "JUDGE"
          2. In-loop judge (p191) — patch format + semantic weakening checks
          3. Unified prerequisite gate (p192) — aggregates cognition + judge
          4. End-of-attempt controlled_verify (step=-1) — oracle eval signal

        Container-lifecycle invariant: this method is called from JinguDefaultAgent.run()
        immediately after super().run() returns. The env (Docker container) is still
        alive at this point — it goes out of scope only after jingu_process_instance()'s
        try block exits.
        """
        from controlled_verify import run_controlled_verify
        from control.reasoning_state import VerdictStop

        # p228: close step event emitter (before any early return)
        if self._step_emitter is not None:
            try:
                self._step_emitter.close()
            except Exception:
                pass
            self._step_emitter = None

        # p230: close decision provenance logger
        if self._decision_logger is not None:
            try:
                self._decision_logger.close()
            except Exception:
                pass
            self._decision_logger = None

        _monitor = self._state
        if _monitor is None:
            print(f"    [attempt-end] skip: no monitor state", flush=True)
            return

        # p241: attempt-end telemetry — observable signal for every attempt termination
        _cp_phase = "unknown"
        if self._cp_state_holder:
            _cp_phase = self._cp_state_holder[0].phase
        _has_submission = bool(submission)

        cid = getattr(getattr(agent_self, "env", None), "container_id", None)
        if not cid:
            print(f"    [attempt-end] phase={_cp_phase} submission={_has_submission}"
                  f" cv_triggered=false cv_skip_reason=no_container_id", flush=True)
            return
        submitted = submission or ""
        if not submitted:
            print(f"    [attempt-end] phase={_cp_phase} submission=false"
                  f" cv_triggered=false cv_skip_reason=no_submission", flush=True)
            return

        print(f"    [attempt-end] phase={_cp_phase} submission=true"
              f" cv_triggered=true container={cid[:12]}", flush=True)

        cp_state_holder = self._cp_state_holder if self._cp_state_holder else None

        # p187: cognition gate — check declaration quality before controlled_verify.
        # Fires when cp_state.phase == "JUDGE" (EXECUTE->JUDGE advance by verdict routing).
        # Pass  → continue to controlled_verify as normal.
        # Fail  → inject feedback as pending_redirect_hint, skip controlled_verify.
        _cg_result_str: str | None = None
        if cp_state_holder is not None and cp_state_holder[0].phase == "JUDGE":
            _cg_decl: dict = {}
            try:
                from declaration_extractor import (
                    extract_declaration,
                    extract_last_agent_message,
                    extract_from_structured,
                )
                from patch_signals import extract_patch_signals
                _cg_msgs = getattr(agent_self, "messages", [])
                # p221: try structured output first
                from run_with_jingu_gate import _try_parse_structured_output
                _cg_structured = _try_parse_structured_output(_cg_msgs)
                if _cg_structured is not None:
                    _cg_decl = extract_from_structured(_cg_structured)
                else:
                    _cg_last = extract_last_agent_message(_cg_msgs)
                    _cg_decl = extract_declaration(_cg_last) if _cg_last else {}
            except Exception:
                pass
            _cg_signals = extract_patch_signals(submitted) if submitted else []
            from cognition_check import check_cognition_at_judge as _cg_judge
            _cg_pass, _cg_feedback = _cg_judge(_cg_decl, _cg_signals)
            _cg_result_str = "pass" if _cg_pass else "fail"
            print(f"    [cognition_gate] phase=JUDGE result={_cg_result_str}", flush=True)
            if not _cg_pass:
                # Inject feedback as redirect hint — agent receives it on next attempt
                _monitor.pending_redirect_hint = f"[COGNITION_FAIL] {_cg_feedback}"
                print(
                    f"    [cognition_gate] skipping controlled_verify — feedback injected",
                    flush=True,
                )

        # p191: in-loop judge — patch format + semantic weakening checks.
        # Runs after cognition gate, before controlled_verify.
        # Hard checks (block): patch_non_empty, patch_format, no_semantic_weakening.
        # Soft check (warn only): changed_file_relevant.
        # Exception-safe: judge failure never crashes main flow.
        _judge_result = None
        try:
            from in_loop_judge import run_in_loop_judge as _run_ilj
            _judge_result = _run_ilj(submitted)
            print(
                f"    [in_loop_judge] "
                f"patch_non_empty={'pass' if _judge_result.patch_non_empty else 'fail'} "
                f"patch_format={'pass' if _judge_result.patch_format else 'fail'} "
                f"semantic_weakening={'pass' if _judge_result.no_semantic_weakening else 'fail'} "
                f"changed_file_relevant={'pass' if _judge_result.changed_file_relevant else 'fail'}",
                flush=True,
            )
            if not _judge_result.all_pass:
                # Hard check failures — set redirect hints (controlled_verify gated below)
                if not _judge_result.patch_non_empty:
                    _monitor.early_stop_verdict = VerdictStop(reason="empty_patch")
                elif not _judge_result.patch_format:
                    _monitor.pending_redirect_hint = "[REDIRECT:EXECUTE] patch_format_error"
                elif not _judge_result.no_semantic_weakening:
                    _monitor.pending_redirect_hint = "[REDIRECT:ANALYZE] semantic_weakening_detected"
                elif not _judge_result.changed_file_relevant:
                    # p204: changed_file_relevant promoted to hard check
                    # Agent modified only test files (not source) — redirect back to EXECUTE
                    _monitor.pending_redirect_hint = "[REDIRECT:EXECUTE] wrong_file_changed"
                print(
                    f"    [in_loop_judge] skipping controlled_verify (hard check failed)",
                    flush=True,
                )
        except Exception as _ilj_exc:
            print(f"    [in_loop_judge] error (non-fatal): {_ilj_exc}", flush=True)

        # p192: unified prerequisite gate — aggregates cognition + judge results
        from run_with_jingu_gate import _verify_prerequisites
        _prereq_pass, _prereq_reason = _verify_prerequisites(
            cognition_result=_cg_result_str,
            judge_result=_judge_result,
        )
        print(
            f"    [verify_gate] prerequisite={'pass' if _prereq_pass else f'fail({_prereq_reason})'} "
            f"controlled_verify={'run' if _prereq_pass else 'skipped'}",
            flush=True,
        )

        if not _prereq_pass:
            _monitor._verify_skipped = True
            _monitor._verify_skip_reason = _prereq_reason
            return

        t_cv0 = time.monotonic()
        cv_result = run_controlled_verify(submitted, self._instance, cid, timeout_s=None)
        cv_result["elapsed_ms"] = round((time.monotonic() - t_cv0) * 1000, 1)
        # Store as last verify_history entry (step=-1 means end-of-attempt)
        _monitor.record_verify(-1, cv_result)
        # v2 two-column log: final-verify (oracle/eval) vs inner-verify (agent-visible)
        _er = cv_result.get("eval_resolved")
        if _er is not None:
            print(
                f"    [outcome-eval] eval_resolved={_er}"
                f"  f2p={cv_result.get('f2p_passed')}/{(cv_result.get('f2p_passed', 0) or 0) + (cv_result.get('f2p_failed', 0) or 0)}"
                f"  p2p={cv_result.get('p2p_passed')}/{(cv_result.get('p2p_passed', 0) or 0) + (cv_result.get('p2p_failed', 0) or 0)}",
                flush=True,
            )

    # -- top-level lifecycle ------------------------------------------------

    def run_attempt(
        self,
        attempt: int,
        previous_failure: str | None = "",
        parent_timer: Any = None,
        design_lock_context: str = "",
    ) -> "AttemptOutcome":
        """Execute a single attempt: prompt -> inner agent -> traj parse -> jingu_body.

        Moved from run_agent() in run_with_jingu_gate.py (p225-09).
        Returns AttemptOutcome wrapping AttemptResult with all attempt outputs.
        """
        from minisweagent.config import get_config_from_spec
        from minisweagent.utils.serialize import recursive_merge
        from minisweagent.run.benchmarks.swebench import RunBatchProgressManager
        from run_with_jingu_gate import (
            Timer, ModelUsage, BASE_CONFIG, _usage_tracker,
            extract_jingu_body, classify_failure, get_failure_routing,
            parse_pytest_output,
        )
        from failure_classifier import classify_failure_layer, route_from_failure, derive_failure_mode, route_from_failure_mode
        from step_monitor_state import StepMonitorState, StopExecution

        instance = self._instance
        instance_id = instance["instance_id"]
        attempt_dir = self._output_dir / f"attempt_{attempt}"
        attempt_dir.mkdir(parents=True, exist_ok=True)

        t_agent = Timer(f"agent attempt={attempt}", parent=parent_timer)

        # Start from jingu-swebench.yaml (fork of swebench.yaml with FORBIDDEN ACTIONS block,
        # patched system_template, and Recommended Workflow steps 2/4/5 removed).
        # Config lives in mini-swe-agent/src/minisweagent/config/benchmarks/jingu-swebench.yaml.
        t_cfg = Timer("config load", parent=t_agent)
        config = get_config_from_spec("jingu-swebench.yaml")
        config = recursive_merge(config, BASE_CONFIG)

        # Build instance_template_extra: tests that must pass + optional retry hint.
        # on_attempt_start() returns the extra_parts list (prompt assembly).
        extra_parts = self.on_attempt_start(attempt, previous_failure)
        if extra_parts:
            # Append directly to instance_template — instance_template_extra is NOT a recognized
            # AgentConfig field and would never be rendered. Direct append is the only correct path.
            config["agent"]["instance_template"] = (
                config["agent"]["instance_template"] + "\n\n" + "\n\n".join(extra_parts)
            )
        # Design admission gate: inject admitted design as hard constraint
        if design_lock_context:
            config["agent"]["instance_template"] = (
                config["agent"]["instance_template"] + "\n\n" + design_lock_context
            )
            print(f"    [design-lock] injected admitted design context into prompt", flush=True)
        t_cfg.stop()

        # p228: create step event emitter for this attempt
        try:
            from step_event_emitter import StepEventEmitter
            self._step_emitter = StepEventEmitter(
                self._output_dir / instance_id, attempt
            )
        except Exception as _emit_exc:
            print(f"    [step-emitter] WARNING: init failed: {_emit_exc}", flush=True)
            self._step_emitter = None

        # p230: create decision provenance logger for this attempt
        try:
            from decision_logger import DecisionLogger
            self._decision_logger = DecisionLogger(
                self._output_dir / instance_id, attempt
            )
        except Exception as _dl_exc:
            print(f"    [decision-logger] WARNING: init failed: {_dl_exc}", flush=True)
            self._decision_logger = None

        print(f"    [agent] running {instance_id} attempt={attempt}...")

        preds_path = attempt_dir / "preds.json"
        progress = RunBatchProgressManager(num_instances=1)

        # Initialize StepMonitorState for this attempt.
        _monitor = StepMonitorState(
            instance_id=instance_id,
            attempt=attempt,
            instance=instance,
        )
        # p226-05 + Plan-B: per-attempt extraction metrics counters
        _monitor._extraction_structured = 0
        _monitor._extraction_regex_fallback = 0
        _monitor._extraction_no_schema = 0
        _monitor._extraction_tool_submitted = 0
        _monitor._missing_submission_count = 0
        # Plan-B: separate storage for diagnostic-only records (never admitted)
        _monitor.diagnostic_phase_records = []
        # C-09: per-phase extraction telemetry from extract_phase_output()
        _monitor.extraction_telemetry = {}
        # Plan-A: reset extraction retry counts per attempt
        _monitor.extraction_retry_counts = {}

        # Pass previous P2P regression names as priority sentinels for this attempt
        if attempt > 1 and self._prev_p2p_regression_names:
            _monitor.priority_sentinel_tests = list(self._prev_p2p_regression_names)
            print(f"    [sentinel-priority] injecting {len(self._prev_p2p_regression_names)} "
                  f"prev regression tests: {self._prev_p2p_regression_names[:3]}", flush=True)
        self._state = _monitor
        # P0.2: cross-attempt routing enforcement
        # _monitor IS self._state — the same StepMonitorState object that gets
        # passed as state= to admit_phase_record() and all step sections.
        # Writing self._state.required_next_phase here is the SAME as writing
        # state.required_next_phase that Gate 0 reads in admit_phase_record().
        _routed_phase = str(self._cp_state_holder[0].phase).upper() if self._cp_state_holder else "OBSERVE"
        if attempt > 1 and _routed_phase != "OBSERVE":
            self._state.required_next_phase = _routed_phase
            print(
                f"    [routing-enforcement] attempt={attempt}"
                f" required_next_phase={_routed_phase}",
                flush=True,
            )
        # p231: reset checkpoint tracking for new attempt
        self._prev_phase_records_count = 0
        # p230: pass decision logger to state for step_sections access
        _monitor._decision_logger = self._decision_logger
        # cp_state_holder already set by caller (run_agent wrapper or run_with_jingu)

        t_llm = Timer("LLM agent loop (Bedrock)", parent=t_agent)
        try:
            jingu_process_instance(
                instance, attempt_dir, config, progress,
                agent_class=JinguDefaultAgent,
                agent_kwargs={"jingu_agent": self},
            )
        except StopExecution as e:
            # VerdictStop: clean early exit — not an error.
            # _monitor.early_stop_verdict is already set; caller (run_with_jingu) will
            # log and break the attempt loop.
            print(
                f"  [cp] early_stop instance={instance_id} attempt={attempt}"
                f" reason={e.reason} — StopExecution caught, exiting agent loop",
                flush=True,
            )
        except Exception as e:
            print(f"    [agent] ERROR: {e}")
            traceback.print_exc()
        finally:
            # p228: ensure emitter is closed even if on_attempt_end was skipped
            if self._step_emitter is not None:
                try:
                    self._step_emitter.close()
                except Exception:
                    pass
                self._step_emitter = None
            # p230: ensure decision logger is closed
            if self._decision_logger is not None:
                try:
                    self._decision_logger.close()
                except Exception:
                    pass
                self._decision_logger = None
        t_llm.stop()

        # Parse traj for usage + submission
        traj_path = attempt_dir / instance_id / f"{instance_id}.traj.json"
        usage = ModelUsage(instance_id, attempt)
        usage.load_from_traj(traj_path)
        _usage_tracker.record(usage)

        sub_from_traj = None
        sub_from_traj_diff = None  # fallback: last valid git diff in tool outputs
        exit_status = None
        jingu_body = None
        if traj_path.exists():
            try:
                traj = json.loads(traj_path.read_text())
                sub_from_traj = traj.get("info", {}).get("submission", "")
                exit_status = traj.get("info", {}).get("exit_status", "")
                # Fallback: if agent hit LimitsExceeded without calling submit,
                # extract the last valid git diff from tool output messages.
                if not sub_from_traj:
                    for m in reversed(traj.get("messages", [])):
                        if m.get("role") != "tool":
                            continue
                        content = str(m.get("content", ""))
                        output_match = re.search(r"<output>(.*?)</output>", content, re.DOTALL)
                        if not output_match:
                            continue
                        output = output_match.group(1).strip()
                        if (output.startswith("diff --git")
                                and re.search(r"^---", output, re.MULTILINE)
                                and re.search(r"^\+\+\+", output, re.MULTILINE)
                                and re.search(r"^@@", output, re.MULTILINE)):
                            sub_from_traj_diff = output
                            print(f"    [agent] fallback: extracted git diff from traj "
                                  f"({len(output)} chars)")
                            break
                # Build jingu_body from traj (deterministic, no LLM call)
                patch_for_body = sub_from_traj or sub_from_traj_diff or ""
                problem_stmt = instance.get("problem_statement", "")
                jingu_body = extract_jingu_body(traj, patch_for_body, problem_stmt)
                # Merge verify signal from _monitor into jingu_body.
                # Priority: final verify (step=-1) > last inner-loop verify > nothing.
                # verify_history[-1] is the end-of-attempt verify (most accurate).
                _final_cv = None
                _fallback_cv = None
                if _monitor.verify_history:
                    # Use the last controlled_fail_to_pass result (final verify or last mid-run)
                    for _vh in reversed(_monitor.verify_history):
                        if _vh["kind"] == "controlled_fail_to_pass":
                            _final_cv = _vh
                            break
                    # Fallback: if no controlled_fail_to_pass but controlled_error exists,
                    # treat as F2P_ALL_FAIL: controlled_passed=0, controlled_failed=N.
                    # This allows governance pack to classify and reroute these cases.
                    if _final_cv is None:
                        for _vh in reversed(_monitor.verify_history):
                            if _vh["kind"] == "controlled_error" and _vh.get("tests_failed", 0) > 0:
                                _fallback_cv = _vh
                                break
                _cv_source = _final_cv or _fallback_cv
                if _cv_source:
                    # P2-fix: include output_tail so build_repair_prompt can
                    # inject concrete test failure output into retry hints.
                    # verify_history now stores output_tail (FAIL/ERROR lines
                    # extracted by controlled_verify); use it directly.
                    cv_flat = {
                        "verification_kind": _cv_source["kind"],
                        "tests_passed": _cv_source["tests_passed"],
                        "tests_failed": _cv_source["tests_failed"],
                        "exit_code": _cv_source["exit_code"],
                        "elapsed_ms": _cv_source["elapsed_ms"],
                        "step": _cv_source["step"],
                        # BUG-10 fix: eval-aligned fields
                        "f2p_passed": _cv_source.get("f2p_passed"),
                        "f2p_failed": _cv_source.get("f2p_failed"),
                        "p2p_passed": _cv_source.get("p2p_passed"),
                        "p2p_failed": _cv_source.get("p2p_failed"),
                        "p2p_failing_names": _cv_source.get("p2p_failing_names", []),
                        "f2p_failing_names": _cv_source.get("f2p_failing_names", []),
                        "eval_resolved": _cv_source.get("eval_resolved"),
                        "output_tail": _cv_source.get("output_tail", ""),
                    }
                    jingu_body["controlled_verify"] = cv_flat
                    # v0.3: store stdout for residual gap payload extraction (not in cv_flat to avoid traj bloat)
                    self._last_cv_stdout = _cv_source.get("stdout") or ""
                    # Store P2P regression names for next attempt's sentinel priority
                    _p2p_names = _cv_source.get("p2p_failing_names", [])
                    if _p2p_names:
                        self._prev_p2p_regression_names = list(_p2p_names)
                    jingu_body["test_results"]["ran_tests"] = True
                    jingu_body["test_results"]["controlled_passed"] = _cv_source["tests_passed"]
                    jingu_body["test_results"]["controlled_failed"] = _cv_source["tests_failed"]
                    jingu_body["test_results"]["controlled_exit_code"] = _cv_source["exit_code"]
                    # BUG-10: log eval-aligned verdict
                    _er = _cv_source.get("eval_resolved")
                    if _er is not None:
                        print(f"    [controlled_verify] eval_resolved={_er}"
                              f"  f2p={_cv_source.get('f2p_passed')}/{(_cv_source.get('f2p_passed',0) or 0)+(_cv_source.get('f2p_failed',0) or 0)}"
                              f"  p2p={_cv_source.get('p2p_passed')}/{(_cv_source.get('p2p_passed',0) or 0)+(_cv_source.get('p2p_failed',0) or 0)}",
                              flush=True)
                    if _fallback_cv and _final_cv is None:
                        print(f"    [cv-fallback] F2P_ALL_FAIL inferred from controlled_error: "
                              f"passed={_cv_source['tests_passed']} failed={_cv_source['tests_failed']}")
                    # p208: failure classification — classify cv_result into typed failure category
                    _ft = classify_failure(cv_flat)
                    if _ft:
                        _routing = get_failure_routing(_ft)
                        jingu_body["failure_type"] = _ft
                        jingu_body["failure_routing"] = _routing
                        jingu_body["repair_directive"] = {
                            "failure_type": _ft,
                            "next_phase": _routing["next_phase"],
                            "repair_goal": _routing["repair_goal"],
                        }
                        jingu_body["retry_mode"] = "phase_specific"
                        # v0.3: record f2p for stall/backslide detection
                        _v03_f2p_p = cv_flat.get("f2p_passed") or 0
                        _v03_f2p_t = _v03_f2p_p + (cv_flat.get("f2p_failed") or 0)
                        self._f2p_history.append((_v03_f2p_p, _v03_f2p_t))
                        print(f"    [failure-classify] type={_ft} next_phase={_routing['next_phase']} "
                              f"f2p_pass={cv_flat.get('f2p_passed', 0)} "
                              f"f2p_fail={cv_flat.get('f2p_failed', 0)}", flush=True)
                        # ScopeLockGate v0.1: build envelope for near_miss/residual_gap
                        # Guard: only build on first qualifying attempt — never overwrite
                        # (spec: "A2 reject does NOT update envelope; A3 uses A1 constraints")
                        if self._scope_lock_envelope is not None:
                            print(f"    [scope-lock] envelope KEPT from attempt "
                                  f"{self._scope_lock_envelope.origin_attempt} "
                                  f"(current attempt={attempt}, skip rebuild)", flush=True)
                        else:
                            try:
                                from scope_lock import build_scope_lock_envelope
                                from run_with_jingu_gate import patch_fingerprint as _sl_pfp, patch_content_hash as _sl_pch
                                _sl_fp = jingu_body.get("patch_fp") if jingu_body else None
                                if not _sl_fp and patch_for_body:
                                    _sl_fp = _sl_pfp(patch_for_body)
                                _sl_hash = _sl_pch(patch_for_body) if patch_for_body else ""
                                if _sl_fp:
                                    _sl_env = build_scope_lock_envelope(
                                        _sl_fp, cv_flat, _ft,
                                        attempt=attempt,
                                        patch_hash=_sl_hash,
                                    )
                                    if _sl_env is not None:
                                        self._scope_lock_envelope = _sl_env
                                        print(f"    [scope-lock] envelope built: "
                                              f"origin_attempt={attempt} "
                                              f"files={_sl_env.touched_files} "
                                              f"total={_sl_env.patch_total} "
                                              f"limit={_sl_env.size_limit} "
                                              f"type={_ft} "
                                              f"patch_hash={_sl_hash[:8]}", flush=True)
                            except Exception as _sl_exc:
                                print(f"    [scope-lock] envelope build failed (non-fatal): {_sl_exc}", flush=True)
                        # EFR telemetry: structured feedback emission
                        _evidence_quality = "rich" if cv_flat.get("output_tail") or cv_flat.get("stdout") else "counts_only"
                        _current_phase = str(self._cp_state_holder[0].phase).upper() if self._cp_state_holder else "?"
                        _cross_phase = _routing["next_phase"].upper() != _current_phase
                        print(f"    [efr-emit] failure_type={_ft} repair_target={_routing['next_phase']} "
                              f"current_phase={_current_phase} cross_phase={_cross_phase} "
                              f"evidence_quality={_evidence_quality} "
                              f"has_repair_goal={bool(_routing.get('repair_goal'))} "
                              f"has_principals={bool(_routing.get('required_principals'))}", flush=True)
                    else:
                        jingu_body["failure_type"] = None
                        jingu_body["failure_routing"] = None
                        jingu_body["repair_directive"] = None
                        jingu_body["retry_mode"] = "generic"
                        # ScopeLockGate lifecycle: clear envelope when no scope-lockable failure
                        if self._scope_lock_envelope is not None:
                            print(f"    [scope-lock] envelope CLEARED: failure_type=None "
                                  f"(was origin_attempt={self._scope_lock_envelope.origin_attempt})", flush=True)
                            self._scope_lock_envelope = None
                        # v0.3: record f2p even on success (for history completeness)
                        _v03_f2p_p = cv_flat.get("f2p_passed") or 0
                        _v03_f2p_t = _v03_f2p_p + (cv_flat.get("f2p_failed") or 0)
                        self._f2p_history.append((_v03_f2p_p, _v03_f2p_t))
                    # Failure layer: semantic rootcause classification (full FailureRecord)
                    _qj_hist = _monitor.quick_judge_history if hasattr(_monitor, 'quick_judge_history') else None
                    _fr = classify_failure_layer(cv_flat, _qj_hist, _ft, instance_id=instance_id)
                    jingu_body["failure_layer"] = _fr.failure_layer
                    jingu_body["failure_record"] = _fr.to_dict()
                    # Route from failure record for enhanced retry
                    _fr_routing = route_from_failure(_fr)
                    jingu_body["failure_layer_routing"] = _fr_routing
                    # P2: Prediction error — compare DECIDE predictions vs actual
                    try:
                        from prediction_error import compute_prediction_error
                        _pred_err = compute_prediction_error(
                            _monitor.phase_records, cv_flat,
                        )
                        jingu_body["prediction_error"] = _pred_err.to_dict()
                        if _pred_err.error_type != "prediction_no_data":
                            print(
                                f"    [prediction-error] type={_pred_err.error_type}"
                                f" severity={_pred_err.severity}"
                                f" f2p={_pred_err.actual_f2p_passed}/{_pred_err.actual_f2p_passed + _pred_err.actual_f2p_failed}"
                                f" repair_target={_pred_err.repair_target}",
                                flush=True,
                            )
                    except Exception as _pe_exc:
                        jingu_body["prediction_error"] = {"error_type": "computation_error", "detail": str(_pe_exc)[:200]}
                        logger.warning("prediction_error computation failed: %s", _pe_exc)
                    if _fr.failure_layer != "unknown":
                        print(f"    [failure-layer] {_fr.failure_layer}"
                              f"  phase={_fr.phase_of_failure}"
                              f"  confidence={_fr.confidence:.2f}"
                              f"  actions={[a.type for a in _fr.recommended_actions]}",
                              flush=True)
                # p207-P4: store parsed test results as structured data for all consumers.
                # Calls parse_pytest_output on CV stdout so GovernancePacks, retry_controller,
                # and any future consumer can access failing_tests/error_excerpts/summary
                # without re-parsing.
                if _cv_source and _cv_source.get("kind") == "controlled_fail_to_pass":
                    _cv_stdout_p4 = _cv_source.get("stdout", "")
                    _cv_stderr_p4 = _cv_source.get("stderr", "")
                    if _cv_stdout_p4 or _cv_stderr_p4:
                        _parsed = parse_pytest_output(_cv_stdout_p4, _cv_stderr_p4)
                        _cp = _cv_source.get("tests_passed", 0) or 0
                        _cf = _cv_source.get("tests_failed", 0) or 0
                        jingu_body["parsed_test_results"] = {
                            "failing_tests": _parsed["failing_tests"],
                            "error_excerpts": _parsed["error_excerpts"],
                            "summary": _parsed["summary"],
                            "partial_progress": _cp > 0 and _cf > 0,
                        }
                        print(f"    [p207-P4] parsed_test_results: "
                              f"failing={len(_parsed['failing_tests'])} "
                              f"excerpts={len(_parsed['error_excerpts'])} "
                              f"partial={_cp > 0 and _cf > 0}")
                # Store full verify_history for observability
                jingu_body["verify_history"] = _monitor.verify_history
                # E1: Quick Judge telemetry
                if hasattr(_monitor, 'quick_judge_history') and _monitor.quick_judge_history:
                    jingu_body["quick_judge_history"] = _monitor.quick_judge_history
                    jingu_body["quick_judge_invoked"] = len(_monitor.quick_judge_history)
                    jingu_body["quick_judge_acknowledged"] = sum(
                        1 for qj in _monitor.quick_judge_history if qj.get("acknowledged")
                    )
                    jingu_body["quick_judge_directions"] = [
                        qj.get("direction", "unknown") for qj in _monitor.quick_judge_history
                    ]
                    jingu_body["quick_judge_target_statuses"] = [
                        qj.get("target_status", "unknown") for qj in _monitor.quick_judge_history
                    ]
                    jingu_body["quick_judge_signal_kinds"] = [
                        qj.get("signal_kind", "non_corrective_noise") for qj in _monitor.quick_judge_history
                    ]
                    # L3 effectiveness detection
                    try:
                        from quick_judge import detect_effective
                        jingu_body["quick_judge_effective"] = detect_effective(_monitor.quick_judge_history)
                    except Exception:
                        jingu_body["quick_judge_effective"] = None
                    # Log quick judge summary (target-aware)
                    _qj_targets = [qj.get("target_status", "?") for qj in _monitor.quick_judge_history]
                    _qj_signals = [qj.get("signal_kind", "?") for qj in _monitor.quick_judge_history]
                    print(f"    [quick_judge] invoked={len(_monitor.quick_judge_history)} "
                          f"target_statuses={_qj_targets} signals={_qj_signals} "
                          f"effective={jingu_body.get('quick_judge_effective')}",
                          flush=True)
                else:
                    jingu_body["quick_judge_invoked"] = 0
                    jingu_body["quick_judge_effective"] = None
                # p190: per-phase records — one entry per VerdictAdvance during this attempt
                jingu_body["phase_records"] = [r.as_dict() for r in _monitor.phase_records]
                # Plan-B strong: extraction metrics — admitted vs diagnostic rates
                _em_tool = getattr(_monitor, "_extraction_tool_submitted", 0)
                _em_structured = getattr(_monitor, "_extraction_structured", 0)
                _em_regex = getattr(_monitor, "_extraction_regex_fallback", 0)
                _em_no_schema = getattr(_monitor, "_extraction_no_schema", 0)
                _em_missing = getattr(_monitor, "_missing_submission_count", 0)
                _em_total_attempts = _em_tool + _em_missing
                jingu_body["extraction_metrics"] = {
                    "tool_submitted": _em_tool,
                    "missing_submissions": _em_missing,
                    "phase_completion_rate": (
                        f"{_em_tool}/{_em_total_attempts}"
                        if _em_total_attempts > 0 else "0/0"
                    ),
                    "diagnostic_structured": _em_structured,
                    "diagnostic_regex": _em_regex,
                    "diagnostic_no_schema": _em_no_schema,
                }
                print(
                    f"    [extraction_metrics] attempt={attempt}"
                    f" tool_submitted={_em_tool}"
                    f" missing_submissions={_em_missing}"
                    f" phase_completion_rate={_em_tool}/{_em_total_attempts}"
                    f" diagnostic_structured={_em_structured}"
                    f" diagnostic_regex={_em_regex}",
                    flush=True,
                )
                # C-09: merge extraction_telemetry from step_sections into jingu_body
                _ext_telem = getattr(_monitor, "extraction_telemetry", None)
                if _ext_telem:
                    jingu_body["extraction_telemetry"] = _ext_telem

                # p207-P9: log selective bypass summary at attempt end
                if _monitor._bypassed_principals:
                    _bp_sorted = sorted(_monitor._bypassed_principals)
                    jingu_body["bypassed_principals"] = _bp_sorted
                    print(
                        f"    [fake_loop_summary] total_bypassed={len(_bp_sorted)}"
                        f" principals={_bp_sorted}",
                        flush=True,
                    )
                # p195: principal inference telemetry — rich result with signals/explanation
                try:
                    from principal_inference import run_inference, diff_principals
                    from jingu_onboard import onboard as _onb_endtelem
                    _gov_endtelem = _onb_endtelem()
                    _pi_telemetry = []
                    for _telem_pr in _monitor.phase_records:
                        _telem_phase = str(getattr(_telem_pr, "phase", ""))
                        _telem_cfg = _gov_endtelem.get_phase_config(_telem_phase)
                        _telem_subtype = _telem_cfg.subtype if _telem_cfg else ""
                        _telem_rich = run_inference(_telem_pr, _telem_subtype)
                        _telem_diff = diff_principals(
                            getattr(_telem_pr, "principals", []) or [],
                            _telem_rich,
                            phase=_telem_phase,
                        )
                        _telem_details = {
                            p: {
                                "score": round(r.score, 2),
                                "signals": r.signals,
                                "explanation": r.explanation,
                            }
                            for p, r in _telem_rich.details.items()
                        }
                        _pi_telemetry.append({
                            "phase": _telem_phase,
                            "subtype": _telem_subtype,
                            "declared": list(getattr(_telem_pr, "principals", []) or []),
                            "inferred": {
                                "present": _telem_rich.present,
                                "absent": _telem_rich.absent,
                            },
                            "details": _telem_details,
                            "diff": {
                                "missing_required": _telem_diff.get("missing_required", []),
                                "missing_expected": _telem_diff.get("missing_expected", []),
                                "fake": _telem_diff.get("fake", []),
                            },
                        })
                    jingu_body["principal_inference"] = _pi_telemetry
                except Exception:
                    pass
                # p192: verify_skipped — distinct from controlled_verify fail
                # Only set when prereq gate blocked controlled_verify from running.
                if getattr(_monitor, "_verify_skipped", False):
                    jingu_body["verify_skipped"] = True
                    jingu_body["verify_skip_reason"] = getattr(_monitor, "_verify_skip_reason", "unknown")
                    jingu_body["controlled_verify_result"] = "skipped"
                # ── Dual-layer failure classification ──────────────────────
                # failure_mode: behavioral (full coverage, all attempts)
                # failure_type: semantic (CV-based, high confidence only)
                # failure_source: provenance marker
                _fm = derive_failure_mode(jingu_body)
                jingu_body["failure_mode"] = _fm
                if jingu_body.get("failure_type") is not None:
                    jingu_body["failure_source"] = "cv_based"
                elif _cv_source is None:
                    jingu_body["failure_source"] = "behavioral_fallback"
                else:
                    # CV existed but classify_failure returned None (success)
                    jingu_body["failure_source"] = "cv_based"
                print(f"    [failure-mode] mode={_fm} source={jingu_body['failure_source']}"
                      f" type={jingu_body.get('failure_type', 'none')}", flush=True)
                # Write jingu_body back into traj.json so gate_runner.js can read it
                traj["jingu_body"] = jingu_body
                traj_path.write_text(json.dumps(traj, indent=2, default=str))
                cv_summary = ""
                if _final_cv:
                    cv_summary = (f" cv_kind={_final_cv['kind']}"
                                  f" cv_passed={_final_cv['tests_passed']}"
                                  f" cv_failed={_final_cv['tests_failed']}"
                                  f" cv_step={_final_cv['step']}")
                print(f"    [jingu_body] extracted: exit={jingu_body['exit_status']} "
                      f"files_written={len(jingu_body['files_written'])} "
                      f"tests_ran={jingu_body['test_results']['ran_tests']} "
                      f"patch_hunks={jingu_body['patch_summary']['hunks']}"
                      f"{cv_summary}")
            except (json.JSONDecodeError, OSError):
                pass

        t_agent.llm_calls = usage.api_calls  # stash for timing tree
        avg_s = t_llm.elapsed / usage.api_calls if usage.api_calls else 0
        print(f"    [agent] LLM loop done in {t_llm.elapsed:.1f}s  "
              f"bedrock_calls={usage.api_calls}  avg={avg_s:.1f}s/call  "
              f"tokens={usage.input_tokens}in/{usage.output_tokens}out  "
              f"cost=${usage.cost_usd:.4f}")

        t_agent.stop()

        # Determine final patch — same priority as original run_agent()
        patch: str | None = None

        # Read submission from preds.json
        if preds_path.exists():
            preds = json.loads(preds_path.read_text())
            if instance_id in preds:
                sub = preds[instance_id].get("model_patch", "")
                if sub:
                    patch = sub

        if patch is None and sub_from_traj:
            patch = sub_from_traj

        if patch is None and sub_from_traj_diff:
            patch = sub_from_traj_diff

        # Container git diff fallback.
        # agent used str_replace_editor to modify files but never called submit, and never
        # printed git diff. sub_from_traj and sub_from_traj_diff are both empty, but the
        # container may still have real changes — pull them directly.
        if patch is None:
            _cid = _monitor.container_id if _monitor else None
            if _cid:
                try:
                    _base_c = instance.get("base_commit", "HEAD")
                    _diff_r = subprocess.run(
                        ["docker", "exec", "-w", "/testbed", _cid, "git", "diff", _base_c],
                        capture_output=True, text=True, timeout=30,
                    )
                    _diff_patch = _diff_r.stdout.strip() if _diff_r.returncode == 0 else ""
                    if _diff_patch:
                        print(
                            f"    [agent] container-diff fallback: extracted {len(_diff_patch)}c patch "
                            f"from container {_cid[:12]}...",
                            flush=True,
                        )
                        patch = _diff_patch
                except Exception as _e:
                    print(f"    [agent] container-diff fallback failed: {_e}", flush=True)

        result = AttemptResult(
            patch=patch,
            exit_status=exit_status,
            jingu_body=jingu_body,
            monitor=_monitor,
        )
        return AttemptOutcome(attempt=attempt, result=result)

    def run(self) -> "InstanceResult":
        """Execute the full multi-attempt loop with retry, gate evaluation, and governance.

        Moved from run_with_jingu() in run_with_jingu_gate.py (p225-10).
        Owns: onboarding check, attempt loop, NBR/EFR enforcement, gate evaluation,
        retry planning, early stop handling, candidate selection.
        """
        # Lazy imports to avoid circular dependencies — all from run_with_jingu_gate.py
        # and its re-exports.
        from run_with_jingu_gate import (
            Timer, _timing_root, _instance_timers, _usage_tracker,
            GATE_MODE, RETRY_CONTROLLER_ENABLED, STRUCTURED_OUTPUT_ENABLED,
            STRATEGY_LOG_PATH, STRATEGY_TABLE_PATH,
            _try_parse_structured_output,
            classify_admission, patch_fingerprint, patch_content_hash, patch_similarity,
            score_patch, normalize_patch, extract_test_counts,
            check_test_progress_invariant, compute_attempt_delta,
            build_execution_feedback, extract_principal_violation_codes,
            jingu_structural_check,
        )
        from jingu_gate_bridge import evaluate_patch_from_traj
        from retry_controller import build_retry_plan, RetryPlan
        from failure_classifier import (
            classify_failure, get_routing as get_failure_routing,
            classify_failure_layer, route_from_failure,
            route_from_failure_mode,
        )
        from repair_prompts import build_repair_prompt
        from failure_routing import route_failure as route_failure_p216, is_data_driven_routing_enabled
        from strategy_prompts import get_strategy_prompt
        from governance_runtime import (
            run_governance_packs, override_retry_plan_from_pack,
            ExecutionContext as GovExecutionContext,
        )
        from strategy_logger import log_strategy_entry, make_entry as make_strategy_entry
        from declaration_extractor import (
            extract_declaration, extract_last_agent_message, extract_from_structured,
        )
        from patch_signals import extract_patch_signals
        from cognition_check import check_cognition, format_cognition_feedback
        from control.reasoning_state import (
            initial_reasoning_state, update_reasoning_state, decide_next,
            normalize_signals, VerdictStop, VerdictRedirect,
        )
        from control.swe_signal_adapter import extract_verify_signals
        from control.phase_result import build_phase_result, route_from_phase_result
        from signal_extraction import compute_steps_since_last_signal
        from step_monitor_state import early_stop_scope
        from controlled_verify import _check_onboarding, _build_execution_model, _print_execution_model

        instance_id = self._instance["instance_id"]

        t_inst = Timer(f"instance: {instance_id}", parent=_timing_root)
        _instance_timers[instance_id] = t_inst

        print(f"  [jingu] loading instance {instance_id}...")

        # ONBOARDING_FIRST: verify official harness path is known before any execution
        _ok, _reason = _check_onboarding(self._instance)
        if not _ok:
            print(f"[onboarding-check] FAIL: {_reason}")
            return InstanceResult(
                instance_id=instance_id,
                accepted=False,
                patch="",
                attempts=self._max_attempts,
                status="rejected",
                failure_type="ONBOARDING_REQUIRED",
                reason=_reason,
            )
        print("[onboarding-check] PASS")
        _print_execution_model(_build_execution_model(self._instance))

        candidates: list[dict] = []
        attempts_log: list[dict] = []
        last_failure = ""
        _prev_raw_patch = ""
        _prev_root_cause = ""  # dual-cause: persist attempt 1's root cause for attempt 2 prompt
        _prev_strategy_type = ""  # dual-cause: agent-declared repair_strategy_type from ANALYZE
        _no_progress_streak = 0
        total_llm_calls = 0
        _strategy_entries: list[dict] = []
        _past_approach_summaries: list[str] = []  # WS-4: track approach directions across attempts
        self._prev_files_written: set[str] = set()  # P0.1: L2 same-files detection
        self._prev_failure_mode: str | None = None  # P0.2: environment_failure early terminate
        _test_counts_by_attempt: dict[int, int] = {}
        _next_attempt_start_phase: str = "OBSERVE"  # p-fix: repair routing target for next attempt
        _next_attempt_start_phase_for_ack: str | None = None  # EFR: prescribed phase for ack check
        _last_failure_type: str = ""  # telemetry: which failure_type drove the routing
        self._f2p_history: list[tuple[int, int]] = []  # v0.3: (f2p_passed, f2p_total) per attempt for stall/backslide
        self._last_cv_stdout: str = ""  # v0.3: raw CV stdout for residual payload extraction
        self._scope_lock_envelope = None  # ScopeLockGate v0.1: built after A1 CV for near_miss/residual_gap
        cp_state_holder: list = [initial_reasoning_state("OBSERVE")]
        self._cp_state_holder = cp_state_holder

        # ── DHG Phase 1.5: pre-generate dual hypotheses before attempt loop ──
        # When DHG_ENABLED=1, generate both hypotheses once upfront.
        # Attempt 1 uses hypothesis A, attempt 2 uses hypothesis B.
        # This gives empirical CV comparison between candidates.
        _dhg_hypotheses = None  # DualHypothesisResult, stored across attempts
        _dhg_attempt_results = {}  # {1: {label, f2p_passed, ...}, 2: {...}}
        from design_admission import is_dhg_enabled as _is_dhg_enabled_check
        if _is_dhg_enabled_check():
            _dhg_hypotheses = self.generate_dual_hypotheses(1, "")
            if _dhg_hypotheses.parse_ok:
                print(
                    f"    [dhg] pre-generated hypotheses: "
                    f"A_files={_dhg_hypotheses.hypothesis_a.target_files} "
                    f"B_files={_dhg_hypotheses.hypothesis_b.target_files} "
                    f"diversity={_dhg_hypotheses.diversity_score:.2f}",
                    flush=True,
                )
            else:
                print(f"    [dhg] pre-generation failed, falling back to single design", flush=True)
                _dhg_hypotheses = None

        for attempt in range(1, self._max_attempts + 1):
            print(f"  [attempt {attempt}/{self._max_attempts}] {instance_id}")

            # Reset cp_state at attempt boundary — phase must match repair routing target
            # (p-fix: without this, cp_state.phase retains attempt N's final phase
            #  while the prompt says "REPAIR PHASE: X" — 100% mismatch on attempt 2+)
            if attempt > 1:
                # DHG Phase 1.5: when DHG is active, attempt 2 is a FRESH start
                # with hypothesis B, not a retry of attempt 1's failure.
                if _dhg_hypotheses is not None:
                    cp_state_holder[0] = initial_reasoning_state("OBSERVE")
                    last_failure = ""  # fresh start, no failure context from attempt 1
                    _last_failure_type = ""
                    self._file_ban_active = False
                    self._direction_search_required = False
                    self._prev_files_written = set()
                    print(f"    [cp-reset] attempt={attempt} start_phase=OBSERVE"
                          f" failure_routing_source=dhg_hypothesis_b (fresh start)", flush=True)
                    _next_attempt_start_phase = "OBSERVE"
                else:
                    # Normal retry routing
                    # Normalize alias → canonical phase name (defense-in-depth)
                    # _next_attempt_start_phase is already canonical (from FAILURE_ROUTING_RULES)
                    cp_state_holder[0] = initial_reasoning_state(_next_attempt_start_phase)
                    print(f"    [cp-reset] attempt={attempt} start_phase={_next_attempt_start_phase}"
                          f" failure_routing_source={_last_failure_type or 'none'}", flush=True)
                    _next_attempt_start_phase = "OBSERVE"  # reset for next iteration

                # File-ban enforcement: activate when wrong_direction + have previous files
                if not (_dhg_hypotheses is not None) and _last_failure_type.startswith("wrong_direction") and self._prev_files_written:
                    self._file_ban_active = True
                    self._file_ban_files = set(self._prev_files_written)
                    self._file_ban_violations = 0
                    # WDRG v0.2: activate direction-search contract (pre-write guard)
                    self._direction_search_required = True
                    self._direction_search_admitted = False
                    self._direction_search_attempts = 0
                    self._direction_search_record = None
                    self._direction_search_last_failures = []
                    print(f"    [file-ban] ACTIVATED: wrong_direction, banned_files="
                          f"{sorted(self._file_ban_files)}", flush=True)
                    print(f"    [wdrg-v02] direction-search contract ACTIVATED: "
                          f"writes blocked until hypothesis admitted", flush=True)
                else:
                    self._file_ban_active = False
                    self._direction_search_required = False
            else:
                import dataclasses as _dc_boundary
                cp_state_holder[0] = _dc_boundary.replace(cp_state_holder[0], principal_violation="")

            # NBR enforcement: No Blind Retry
            # Skip NBR check when DHG is active — attempt 2 is a fresh hypothesis, not a retry
            if attempt > 1 and not last_failure.strip() and self._mode != "baseline" and _dhg_hypotheses is None:
                raise RuntimeError(
                    f"[NBR violation] attempt {attempt} has empty last_failure. "
                    "Execution feedback is required before retry. "
                    "Check build_execution_feedback() and ensure tests_ran signal is captured."
                )

            # ── Design Admission Gate (in-loop, pre-execution) ────────────
            # Feature flag: DESIGN_ADMISSION_ENABLED=1
            # Generates a DesignRecord via separate LLM call, validates it,
            # and injects the admitted design as execution constraint.
            _design_lock_context = ""
            _da_result = self.generate_design_record(attempt, last_failure)
            if not _da_result.admitted:
                # Design rejected — short-circuit this attempt
                print(
                    f"    [design-admission] attempt={attempt} SHORT-CIRCUIT: "
                    f"design not admitted, skipping agent execution",
                    flush=True,
                )
                attempts_log.append({
                    "attempt": attempt,
                    "admission_reason": "design_invalid",
                    "patch_fp": None,
                    "gate_reason_codes": ["DESIGN_INVALID"],
                    "exit_status": None,
                    "design_failures": _da_result.failure_reasons,
                    "design_gate_path": _da_result.gate_path,
                    "execution_started_after_design_admit": False,
                })
                last_failure = _da_result.repair_hint or (
                    "Your DESIGN record was rejected. You must submit a valid design "
                    "before the agent can proceed to write code."
                )
                _last_failure_type = "design_invalid"
                continue  # skip to next attempt
            elif _da_result.record and _da_result.record.target_files:
                # Design admitted — build lock context for agent prompt
                from design_admission import build_design_lock_context
                _design_lock_context = build_design_lock_context(_da_result.record)
                print(
                    f"    [design-admission] ADMITTED: target_files="
                    f"{_da_result.record.target_files}",
                    flush=True,
                )

            # ── Divergent Hypothesis Generation (DHG) — Phase 1.5 ────────
            # Uses pre-generated hypotheses: attempt 1 → hyp A, attempt 2 → hyp B.
            # This gives empirical CV comparison between candidates.
            _dhg_telemetry = {}
            if _dhg_hypotheses is not None:
                _ha = _dhg_hypotheses.hypothesis_a
                _hb = _dhg_hypotheses.hypothesis_b

                # Assign hypothesis by attempt number
                if attempt == 1:
                    _active_hyp, _other_hyp, _active_label = _ha, _hb, "A"
                else:
                    _active_hyp, _other_hyp, _active_label = _hb, _ha, "B"

                # Override design lock context with active hypothesis
                from design_admission import build_design_lock_context as _dhg_build_lock
                _design_lock_context = _dhg_build_lock(_active_hyp)

                # Inject alternative hypothesis as awareness context
                _alt_context = (
                    "\n\n=== ALTERNATIVE HYPOTHESIS (for awareness) ===\n"
                    f"Root cause: {_other_hyp.scope_boundary}\n"
                    f"Target files: {_other_hyp.target_files}\n"
                    f"Approach: {_other_hyp.solution_approach}\n"
                    "If your primary approach fails, consider this alternative.\n"
                    "=== END ALTERNATIVE ==="
                )
                _design_lock_context += _alt_context

                # Override _da_result record for execution_admission check
                _da_result.record = _active_hyp

                # Heuristic scores for telemetry
                _score_a = len(_ha.target_files) + len(_ha.solution_approach) / 10000
                _score_b = len(_hb.target_files) + len(_hb.solution_approach) / 10000
                _heuristic_winner = "A" if _score_a <= _score_b else "B"

                _dhg_telemetry = {
                    "enabled": True,
                    "diversity_score": _dhg_hypotheses.diversity_score,
                    "active_hypothesis": _active_label,
                    "heuristic_winner": _heuristic_winner,
                    "hypothesis_a": {
                        "files": _ha.target_files,
                        "root_cause": _ha.scope_boundary[:200],
                        "approach": _ha.solution_approach[:200],
                        "mechanism": _ha.validation_plan[:200],
                    },
                    "hypothesis_b": {
                        "files": _hb.target_files,
                        "root_cause": _hb.scope_boundary[:200],
                        "approach": _hb.solution_approach[:200],
                        "mechanism": _hb.validation_plan[:200],
                    },
                    "selection_reason": (
                        f"attempt={attempt} → hypothesis_{_active_label} "
                        f"(heuristic_winner={_heuristic_winner}, "
                        f"focus_score: A={_score_a:.2f} B={_score_b:.2f})"
                    ),
                }

                print(
                    f"    [dhg] attempt={attempt} ACTIVE=hypothesis_{_active_label} "
                    f"files={_active_hyp.target_files} "
                    f"diversity={_dhg_hypotheses.diversity_score:.2f} "
                    f"heuristic_winner={_heuristic_winner}",
                    flush=True,
                )

            outcome = self.run_attempt(
                attempt,
                previous_failure=last_failure,
                parent_timer=t_inst,
                design_lock_context=_design_lock_context,
            )
            patch = outcome.result.patch
            agent_exit = outcome.result.exit_status
            jingu_body = outcome.result.jingu_body
            _attempt_monitor = outcome.result.monitor

            # Inject DHG telemetry into jingu_body
            if jingu_body and _dhg_telemetry:
                jingu_body["dhg"] = _dhg_telemetry

            # DHG Phase 1.5: record attempt result for empirical comparison
            if _dhg_hypotheses is not None and _dhg_telemetry:
                _dhg_cv = (jingu_body or {}).get("test_results", {})
                _dhg_f2p_p = _dhg_cv.get("f2p_passed", 0) or 0
                _dhg_f2p_f = _dhg_cv.get("f2p_failed", 0) or 0
                _dhg_f2p_t = _dhg_f2p_p + _dhg_f2p_f
                _dhg_attempt_results[attempt] = {
                    "label": _dhg_telemetry.get("active_hypothesis", "?"),
                    "f2p_passed": _dhg_f2p_p,
                    "f2p_total": _dhg_f2p_t,
                    "f2p_ratio": _dhg_f2p_p / _dhg_f2p_t if _dhg_f2p_t > 0 else 0.0,
                    "patch_generated": bool(patch and patch.strip()),
                    "files_written": sorted((jingu_body or {}).get("files_written", [])),
                }
                print(
                    f"    [dhg-record] attempt={attempt} "
                    f"hypothesis={_dhg_attempt_results[attempt]['label']} "
                    f"f2p={_dhg_f2p_p}/{_dhg_f2p_t} "
                    f"patch={'yes' if _dhg_attempt_results[attempt]['patch_generated'] else 'no'}",
                    flush=True,
                )

            # Persist design admission result into jingu_body for telemetry
            if jingu_body:
                jingu_body["design_admission"] = {
                    "admitted": _da_result.admitted,
                    "gate_path": _da_result.gate_path,
                    "target_files": (_da_result.record.target_files if _da_result.record else []),
                    "solution_approach": (
                        _da_result.record.solution_approach[:200]
                        if _da_result.record else ""
                    ),
                    "failure_reasons": _da_result.failure_reasons,
                }
                # Key telemetry: did execution actually start after design admit?
                jingu_body["execution_started_after_design_admit"] = True

            # ── Execution Admission: verify patch files ⊆ design target_files ──
            # This is the real in-loop constraint: design commits to files,
            # execution is checked against that commitment.
            if (
                _da_result
                and _da_result.admitted
                and _da_result.record
                and _da_result.record.target_files
                and jingu_body
            ):
                _design_files = set(_da_result.record.target_files)
                _written_files = set(jingu_body.get("files_written", []))
                _out_of_scope = _written_files - _design_files
                _exec_admission = {
                    "design_target_files": sorted(_design_files),
                    "actual_files_written": sorted(_written_files),
                    "out_of_scope_files": sorted(_out_of_scope),
                    "scope_violation": len(_out_of_scope) > 0,
                    "overlap": (
                        len(_written_files & _design_files) / len(_written_files)
                        if _written_files else 1.0
                    ),
                }
                # Classify violation type
                def _is_test_file(path: str) -> bool:
                    """Heuristic: is this file a test file?"""
                    import os
                    base = os.path.basename(path)
                    # Pattern matches for common test file conventions
                    if "/tests/" in path or "/test/" in path or path.startswith("tests/") or path.startswith("test/"):
                        return True
                    if "/__tests__/" in path or "/testing/" in path:
                        return True
                    if base.startswith("test_") or base.endswith("_test.py") or base.endswith("_tests.py"):
                        return True
                    if base.endswith(".test.py") or base.endswith(".spec.py"):
                        return True
                    if base == "tests.py":
                        return True
                    return False

                _violation_type = "none"
                _all_oos_are_tests = False
                if _out_of_scope:
                    _oos_overlap = _exec_admission["overlap"]
                    _test_files = {f for f in _out_of_scope if _is_test_file(f)}
                    _all_oos_are_tests = len(_test_files) == len(_out_of_scope) and len(_out_of_scope) > 0
                    if _oos_overlap == 0.0:
                        _violation_type = "wrong_direction"
                    elif _all_oos_are_tests:
                        _violation_type = "test_expansion"
                    else:
                        _violation_type = "boundary_expansion"
                _exec_admission["violation_type"] = _violation_type
                _exec_admission["all_oos_are_tests"] = _all_oos_are_tests
                jingu_body["execution_admission"] = _exec_admission
                if _out_of_scope:
                    print(
                        f"    [exec-admission] SCOPE VIOLATION: "
                        f"type={_violation_type} "
                        f"design_files={sorted(_design_files)} "
                        f"written_files={sorted(_written_files)} "
                        f"out_of_scope={sorted(_out_of_scope)} "
                        f"overlap={_exec_admission['overlap']:.2f}",
                        flush=True,
                    )
                else:
                    print(
                        f"    [exec-admission] OK: "
                        f"written_files={sorted(_written_files)} "
                        f"all within design target_files",
                        flush=True,
                    )

            # EFR telemetry: acknowledgment — did attempt N enter the prescribed repair phase?
            if attempt > 1 and _last_failure_type and _next_attempt_start_phase_for_ack:
                _phase_records = (jingu_body or {}).get("phase_records", [])
                _entered_phases = [r.get("phase", "").upper() for r in _phase_records if r.get("phase")]
                _entered_prescribed = _next_attempt_start_phase_for_ack.upper() in _entered_phases
                _first_phase = _entered_phases[0] if _entered_phases else "none"
                print(f"    [efr-ack] attempt={attempt} prescribed_phase={_next_attempt_start_phase_for_ack} "
                      f"entered={_entered_prescribed} first_phase={_first_phase} "
                      f"all_phases={_entered_phases} failure_type={_last_failure_type}", flush=True)
            # NOTE: _next_attempt_start_phase_for_ack is saved at end of loop
            # (after all routing logic has set _next_attempt_start_phase)

            # Early stop verdict handling
            if _attempt_monitor is not None and _attempt_monitor.early_stop_verdict is not None:
                _esv = _attempt_monitor.early_stop_verdict
                print(
                    f"  [cp] early_stop instance={instance_id} attempt={attempt}"
                    f" reason={_esv.reason} — verdict-driven attempt termination",
                    flush=True,
                )
                if _esv.reason == "no_signal":
                    _mon = _attempt_monitor
                    _tr = (jingu_body or {}).get("test_results", {})
                    _phase_result = build_phase_result(
                        str(cp_state_holder[0].phase).upper(),
                        has_patch=_mon._prev_patch_non_empty,
                        has_inner_verify=len(_mon.verify_history) > 0,
                        test_results=_tr,
                        no_progress_steps=cp_state_holder[0].no_progress_steps,
                        early_stop_reason=_esv.reason,
                        files_written=len((jingu_body or {}).get("files_written", [])),
                    )
                    _pr_route, _pr_target, _pr_hint = route_from_phase_result(_phase_result)
                    print(
                        f"  [phase_result] phase={_phase_result.phase}"
                        f" outcome={_phase_result.outcome}"
                        f" verdict={_phase_result.verdict}"
                        f" route={_pr_route}"
                        f" target={_pr_target or '-'}"
                        f" trust={_phase_result.trust_score or '-'}"
                        f" reason={_phase_result.judge_reason}",
                        flush=True,
                    )
                    _typed_subtypes = {
                        "NO_PATCH_NO_ATTEMPT",
                        "NO_PATCH_NO_WRITE",
                        "NO_PATCH_WRITE_FAIL",
                        "NO_PATCH_ABORTED",
                        "NO_SIGNAL_NO_VERIFY",
                        "NO_SIGNAL_STALLED_AFTER_VERIFY",
                    }
                    if _phase_result.outcome in _typed_subtypes and _pr_hint:
                        last_failure = _pr_hint
                    else:
                        last_failure = (
                            "Previous attempt stopped early: no progress signal detected "
                            "(control-plane verdict=STOP no_signal). "
                            "Change your approach entirely — avoid repeated reads without writing code."
                        )
                    # p-fix: propagate phase_result routing target to next attempt cp_state
                    if _pr_target:
                        _next_attempt_start_phase = _pr_target.upper()
                if _esv.reason == "task_success":
                    _mon_ts = _attempt_monitor
                    _tr_ts = (jingu_body or {}).get("test_results", {})
                    _pr_ts = build_phase_result(
                        str(cp_state_holder[0].phase).upper(),
                        has_patch=_mon_ts._prev_patch_non_empty,
                        has_inner_verify=len(_mon_ts.verify_history) > 0,
                        test_results=_tr_ts,
                        no_progress_steps=cp_state_holder[0].no_progress_steps,
                        early_stop_reason="task_success",
                        files_written=len((_tr_ts or {}).get("files_written", [])),
                    )
                    _pr_ts_route, _pr_ts_target, _ = route_from_phase_result(_pr_ts)
                    print(
                        f"  [phase_result] phase={_pr_ts.phase}"
                        f" outcome={_pr_ts.outcome}"
                        f" verdict={_pr_ts.verdict}"
                        f" route={_pr_ts_route}"
                        f" target={_pr_ts_target or '-'}"
                        f" trust={_pr_ts.trust_score or '-'}"
                        f" reason={_pr_ts.judge_reason}",
                        flush=True,
                    )
                    break  # task_success = instance-terminal

                # WS-3: step governance timeout — phase-specific failure attribution
                if _esv.reason.startswith("step_governance_timeout_"):
                    _stalled_phase = _esv.reason.replace("step_governance_timeout_", "").upper()
                    last_failure = (
                        f"GOVERNANCE TIMEOUT: You spent too many steps in {_stalled_phase} phase "
                        f"without submitting a phase record. "
                        f"On retry, you MUST submit a phase record within the deadline. "
                        f"Be direct: read the relevant code, form your conclusion, and submit immediately. "
                        f"Do NOT explore endlessly."
                    )
                    print(
                        f"  [cp] step_governance_timeout phase={_stalled_phase}"
                        f" attempt={attempt}/{self._max_attempts}"
                        f" — attempt-terminal, resetting cp_state for next attempt",
                        flush=True,
                    )
                    cp_state_holder[0] = initial_reasoning_state("OBSERVE")
                    continue

                _scope = early_stop_scope(_esv.reason)
                if _scope == "attempt_terminal":
                    if patch:
                        cp_state_holder[0] = initial_reasoning_state("OBSERVE")
                        print(
                            f"  [cp] no_signal attempt={attempt}/{self._max_attempts}"
                            f" — submission preserved ({len(patch)}c patch),"
                            f" falling through to gate (p24 submission persistence)",
                            flush=True,
                        )
                    else:
                        print(
                            f"  [cp] no_signal attempt={attempt}/{self._max_attempts}"
                            f" — attempt-terminal (no patch), resetting cp_state for next attempt",
                            flush=True,
                        )
                        cp_state_holder[0] = initial_reasoning_state("OBSERVE")
                        continue

            # p179: record test counts
            _test_counts_by_attempt[attempt] = extract_test_counts(jingu_body)

            t_gate = Timer(f"jingu gate attempt={attempt}", parent=t_inst)

            # ── NPRG pre-gate: runs BEFORE patch check (L2 uses files_written, not patch) ──
            _nprg_enabled_pre = __import__("os").environ.get("NPRG_ENABLED", "1") != "0"
            _nprg_prompt = ""  # deferred: applied AFTER retry controller sets last_failure
            _nprg_curr_files = set((jingu_body or {}).get("files_written", []))
            # Fallback: extract files from patch diff if files_written is empty
            if not _nprg_curr_files and patch:
                import re as _re_nprg
                _nprg_curr_files = set(_re_nprg.findall(r'^--- a/(.+)$', patch, _re_nprg.MULTILINE))
            _nprg_prev_files = getattr(self, '_prev_files_written', set())
            _nprg_curr_hash = patch_content_hash(patch) if patch else "empty"
            _nprg_prev_hash = patch_content_hash(_prev_raw_patch) if _prev_raw_patch else None

            _nprg_l1_pre = (attempt > 1 and _nprg_prev_hash is not None
                            and _nprg_curr_hash != "empty"
                            and _nprg_curr_hash == _nprg_prev_hash)
            _nprg_l2_pre = (attempt > 1 and _nprg_curr_files and _nprg_prev_files
                            and _nprg_curr_files == _nprg_prev_files
                            and not _nprg_l1_pre)

            if attempt > 1:
                print(f"    [nprg_state] attempt={attempt} "
                      f"curr_files={sorted(_nprg_curr_files)} prev_files={sorted(_nprg_prev_files)} "
                      f"curr_hash={_nprg_curr_hash} prev_hash={_nprg_prev_hash} "
                      f"l1={_nprg_l1_pre} l2={_nprg_l2_pre} enabled={_nprg_enabled_pre}",
                      flush=True)

            if _nprg_l1_pre or _nprg_l2_pre:
                _nprg_level_pre = "L1_identical_patch" if _nprg_l1_pre else "L2_same_files"
                print(f"    [nprg_detected] attempt={attempt} level={_nprg_level_pre} "
                      f"enabled={_nprg_enabled_pre}", flush=True)
                if jingu_body:
                    jingu_body["nprg_detected"] = _nprg_level_pre

                if _nprg_enabled_pre:
                    if _nprg_l1_pre and _prev_raw_patch and patch:
                        print(f"    [nprg_triggered] level=L1 action=FORCE_NEW_APPROACH", flush=True)
                        if jingu_body:
                            jingu_body["no_progress_repeat"] = "L1_identical_patch"
                        # L1: store prompt for deferred injection AFTER retry controller
                        # (setting last_failure here gets overwritten by retry controller at line ~2441)
                        _nprg_prompt = (
                            "CRITICAL: Your patch is IDENTICAL to your previous attempt — "
                            "exact same diff, zero progress. Your entire approach is wrong. "
                            "You MUST: (1) re-read the failing test output carefully, "
                            "(2) identify what the test ACTUALLY checks (not what you assumed), "
                            "(3) try a COMPLETELY different fix strategy targeting different logic. "
                            f"BANNED: Do not touch {', '.join(sorted(_nprg_curr_files))} "
                            "with the same change pattern."
                        )
                    if _nprg_l2_pre:
                        print(f"    [nprg_triggered] level=L2 action=FORCE_DIRECTION_CHANGE "
                              f"files={sorted(_nprg_curr_files)}", flush=True)
                        if jingu_body:
                            jingu_body["no_progress_repeat"] = "L2_same_files"
                        # L2: store prompt for deferred injection AFTER retry controller
                        _nprg_prompt = (
                            "HARD DIRECTION CHANGE REQUIRED: You modified the exact same files "
                            "as your previous attempt and still failed. Your hypothesis about "
                            "WHERE the bug is located is wrong. "
                            "You MUST: (1) identify a DIFFERENT root cause in DIFFERENT files, "
                            "(2) re-read the failing test to understand what it actually checks, "
                            "(3) write a fix targeting different code. "
                            f"BANNED files: {', '.join(sorted(_nprg_curr_files))}"
                        )

            # ── Exp J: Hard similarity rejection (upgraded from Exp H) ─────────
            # After attempt 2+, measure Jaccard similarity between current and previous
            # patch. If > 0.7 threshold: REJECT this attempt's patch from candidates.
            # This is a HARD gate, not just a prompt escalation.
            _dp_sim = -1.0
            _dp_rejected = False
            if attempt > 1 and patch and _prev_raw_patch:
                _dp_sim = patch_similarity(_prev_raw_patch, patch)
                _dp_threshold = 0.7
                _dp_too_similar = _dp_sim > _dp_threshold
                print(f"    [exp-j-similarity] attempt={attempt} similarity={_dp_sim:.3f} "
                      f"threshold={_dp_threshold} too_similar={_dp_too_similar}",
                      flush=True)
                if jingu_body:
                    jingu_body["dual_patch_similarity"] = round(_dp_sim, 3)
                    jingu_body["dual_patch_too_similar"] = _dp_too_similar
                # HARD REJECTION: drop this patch from candidates if too similar
                # BUT: CV eval_resolved=true overrides similarity rejection —
                # a verified-correct patch must never be dropped by a heuristic gate.
                _cv_resolved = ((jingu_body or {}).get("controlled_verify") or {}).get("eval_resolved")
                if _dp_too_similar and _cv_resolved:
                    print(f"    [exp-j-similarity] CV OVERRIDE: similarity={_dp_sim:.0%} but "
                          f"cv_eval_resolved=True — keeping patch in candidates", flush=True)
                    if jingu_body:
                        jingu_body["cv_override"] = {
                            "type": "similarity",
                            "attempt": attempt,
                            "similarity": round(_dp_sim, 3),
                            "cv_eval_resolved": True,
                            "counterfactual_verdict": "rejected",
                            "final_verdict": "admitted",
                        }
                elif _dp_too_similar:
                    _dp_rejected = True
                    # Remove this attempt's candidate if it was already added
                    candidates = [c for c in candidates if c.get("attempt") != attempt]
                    print(f"    [exp-j-similarity] HARD REJECT: patch {_dp_sim:.0%} similar, "
                          f"dropped from candidates", flush=True)
                    if jingu_body:
                        jingu_body["exp_j_rejected"] = "similarity"
                    # Still escalate prompt for next attempt if available
                    if attempt < self._max_attempts:
                        _nprg_prompt = (
                            "SIMILARITY REJECTION: Your patch is {:.0f}% similar to the previous "
                            "attempt — this does NOT count as a different approach.\n\n"
                            "Your previous patch:\n```diff\n{}\n```\n\n"
                            "You MUST produce a FUNDAMENTALLY different patch:\n"
                            "- Target a DIFFERENT root cause\n"
                            "- Modify DIFFERENT functions or code paths\n"
                            "- Do NOT make cosmetic changes to the same fix\n"
                            "The similarity check will reject patches >70% similar."
                        ).format(
                            _dp_sim * 100,
                            (_prev_raw_patch[:1200] + "\n... [truncated]")
                            if len(_prev_raw_patch) > 1200 else _prev_raw_patch
                        )

            # ── Direction Change Gate: hard reject when wrong_direction + same files ──
            # When previous attempt was classified as wrong_direction, the agent MUST
            # change target files. If A2 modifies a subset of A1's files, the patch
            # is dropped from candidates (same mechanism as Exp-J similarity rejection).
            _dcg_rejected = False
            if (attempt > 1
                    and _nprg_curr_files and _nprg_prev_files
                    and not _dp_rejected):  # skip if already rejected by similarity
                _dcg = check_direction_change(_nprg_prev_files, _nprg_curr_files, _last_failure_type)
                print(f"    [direction-gate] attempt={attempt} failure_type={_last_failure_type} "
                      f"prev_files={sorted(_nprg_prev_files)} curr_files={sorted(_nprg_curr_files)} "
                      f"new_files={sorted(_dcg['new_files'])} overlap={sorted(_dcg['overlap'])} "
                      f"direction_changed={_dcg['direction_changed']} "
                      f"should_reject={_dcg['should_reject']}", flush=True)
                if jingu_body:
                    jingu_body["direction_gate"] = {
                        "prev_files": sorted(_nprg_prev_files),
                        "curr_files": sorted(_nprg_curr_files),
                        "new_files": sorted(_dcg["new_files"]),
                        "direction_changed": _dcg["direction_changed"],
                    }
                _dcg_cv_resolved = ((jingu_body or {}).get("controlled_verify") or {}).get("eval_resolved")
                if _dcg["should_reject"] and _dcg_cv_resolved:
                    print(f"    [direction-gate] CV OVERRIDE: same files but "
                          f"cv_eval_resolved=True — keeping patch in candidates", flush=True)
                    if jingu_body:
                        jingu_body["cv_override"] = {
                            "type": "direction",
                            "attempt": attempt,
                            "prev_files": sorted(_nprg_prev_files),
                            "curr_files": sorted(_nprg_curr_files),
                            "cv_eval_resolved": True,
                            "counterfactual_verdict": "rejected",
                            "final_verdict": "admitted",
                        }
                elif _dcg["should_reject"]:
                    # HARD REJECT: agent did not change direction
                    _dcg_rejected = True
                    candidates = [c for c in candidates if c.get("attempt") != attempt]
                    print(f"    [direction-gate] HARD REJECT: wrong_direction but same files, "
                          f"dropped from candidates", flush=True)
                    if jingu_body:
                        jingu_body["direction_gate_rejected"] = True

            # ── ScopeLockGate v0.1: replaces judge_near_miss_patch with 3-rule system ──
            _nm_rejected = False
            _sl_rejection_hint = ""  # scope lock rejection feedback for agent
            _nm_legacy_reason = ""   # legacy near-miss rejection reason
            # Explicit gate condition: envelope exists + active + current attempt > origin
            _sl_env = self._scope_lock_envelope
            _sl_should_enforce = (
                _sl_env is not None
                and _sl_env.active
                and attempt > _sl_env.origin_attempt
                and patch
                and not _dp_rejected
                and not _dcg_rejected
            )
            if _sl_should_enforce:
                from scope_lock import evaluate_scope_lock
                from run_with_jingu_gate import patch_fingerprint as _sl_pfp
                _sl_a2_fp = _sl_pfp(patch)
                _sl_verdict = evaluate_scope_lock(_sl_env, _sl_a2_fp)
                print(f"    [scope-lock] attempt={attempt} "
                      f"origin_attempt={_sl_env.origin_attempt} "
                      f"admitted={_sl_verdict.admitted} "
                      f"violations={_sl_verdict.violation_codes} "
                      f"a1_total={_sl_verdict.observed['a1_total']} "
                      f"a2_total={_sl_verdict.observed['a2_total']} "
                      f"size_limit={_sl_verdict.observed['size_limit']} "
                      f"overlap={_sl_verdict.observed['overlap_ratio']} "
                      f"new_files={_sl_verdict.observed['new_files']}",
                      flush=True)
                if jingu_body:
                    jingu_body["scope_lock_gate"] = {
                        "admitted": _sl_verdict.admitted,
                        "violation_codes": _sl_verdict.violation_codes,
                        "observed": _sl_verdict.observed,
                    }
                    # activation proof
                    jingu_body["scope_lock_enabled"] = True
                    jingu_body["scope_lock_origin_attempt"] = _sl_env.origin_attempt
                    jingu_body["scope_lock_origin_hash"] = _sl_env.origin_patch_hash[:8]
                    jingu_body["scope_lock_allowed_files"] = sorted(_sl_env.allowed_files)
                    jingu_body["scope_lock_size_limit"] = _sl_env.size_limit
                if not _sl_verdict.admitted:
                    _nm_rejected = True
                    _sl_rejection_hint = _sl_verdict.repair_hint
                    candidates = [c for c in candidates if c.get("attempt") != attempt]
                    print(f"    [scope-lock] HARD REJECT: {_sl_verdict.violation_codes}, "
                          f"dropped from candidates", flush=True)
            elif (not _sl_should_enforce
                    and attempt > 1
                    and _last_failure_type == "near_miss"
                    and patch
                    and not _dp_rejected
                    and not _dcg_rejected):
                # Fallback: use legacy near-miss gate when no envelope available
                _nm_verdict_legacy = judge_near_miss_patch(
                    _nprg_prev_files, _nprg_curr_files, patch, max_lines=30,
                )
                _nm_m = _nm_verdict_legacy["metrics"]
                print(f"    [near-miss-gate-legacy] attempt={attempt} "
                      f"pass={_nm_verdict_legacy['pass']} "
                      f"reason={_nm_verdict_legacy['reject_reason']} "
                      f"lines={_nm_m['total_lines_changed']}",
                      flush=True)
                if jingu_body:
                    jingu_body["near_miss_gate"] = _nm_verdict_legacy
                if not _nm_verdict_legacy["pass"]:
                    _nm_rejected = True
                    _nm_legacy_reason = _nm_verdict_legacy["reject_reason"] or "unknown"
                    candidates = [c for c in candidates if c.get("attempt") != attempt]
                    print(f"    [near-miss-gate-legacy] HARD REJECT: {_nm_legacy_reason}", flush=True)

            # ── ScopeLockGate telemetry: persist gate status for ALL paths (RT4) ──
            if jingu_body and attempt > 1:
                _slt: dict = {"attempt": attempt}
                _sl_env_t = self._scope_lock_envelope
                if _sl_env_t is not None:
                    _slt["envelope_present"] = True
                    _slt["origin_attempt"] = _sl_env_t.origin_attempt
                    _slt["allowed_files"] = sorted(_sl_env_t.allowed_files)
                    _slt["size_limit"] = _sl_env_t.size_limit
                else:
                    _slt["envelope_present"] = False
                if _sl_should_enforce:
                    _slt["eligible"] = True
                    _slt["admitted"] = _sl_verdict.admitted
                    _slt["violation_codes"] = _sl_verdict.violation_codes
                    _slt["observed"] = _sl_verdict.observed
                    if not _sl_verdict.admitted:
                        _slt["repair_hint"] = _sl_verdict.repair_hint
                else:
                    _slt["eligible"] = False
                    # classify skip reason
                    if _sl_env_t is None:
                        _slt["skip_reason"] = "no_envelope"
                    elif not _sl_env_t.active:
                        _slt["skip_reason"] = "envelope_inactive"
                    elif not (attempt > _sl_env_t.origin_attempt):
                        _slt["skip_reason"] = "same_or_earlier_attempt"
                    elif not patch:
                        _slt["skip_reason"] = "no_patch"
                    elif _dp_rejected:
                        _slt["skip_reason"] = "dp_rejected"
                    elif _dcg_rejected:
                        _slt["skip_reason"] = "dcg_rejected"
                    else:
                        _slt["skip_reason"] = "unknown"
                jingu_body["scope_lock_telemetry"] = _slt
                print(f"    [scope-lock-telemetry] attempt={attempt} "
                      f"envelope={'yes' if _slt.get('envelope_present') else 'no'} "
                      f"eligible={_slt['eligible']} "
                      f"{'admitted=' + str(_slt.get('admitted')) if _slt['eligible'] else 'skip=' + _slt.get('skip_reason', '?')}",
                      flush=True)

            self._prev_files_written = _nprg_curr_files
            _prev_raw_patch = patch or _prev_raw_patch  # preserve previous if current empty
            # dual-cause: save root cause from this attempt for next attempt's prompt
            _jb_dc = jingu_body or {}
            _phase_recs_dc = _jb_dc.get("phase_records", [])
            _analyze_rec_dc = next((r for r in _phase_recs_dc if r.get("phase") == "ANALYZE"), None)
            if _analyze_rec_dc and _analyze_rec_dc.get("root_cause"):
                _prev_root_cause = _analyze_rec_dc["root_cause"]
            if _analyze_rec_dc and _analyze_rec_dc.get("repair_strategy_type"):
                _prev_strategy_type = _analyze_rec_dc["repair_strategy_type"]
            # persist nprg/dual-patch fields into traj
            if jingu_body and (_nprg_l1_pre or _nprg_l2_pre or _dp_sim >= 0):
                _nprg_traj_path = (self._output_dir / f"attempt_{attempt}"
                                   / instance_id / f"{instance_id}.traj.json")
                if _nprg_traj_path.exists():
                    try:
                        _nprg_traj = json.loads(_nprg_traj_path.read_text())
                        _nprg_traj["jingu_body"] = jingu_body
                        _nprg_traj_path.write_text(json.dumps(_nprg_traj, indent=2, default=str))
                    except (json.JSONDecodeError, OSError) as _nprg_e:
                        print(f"    [nprg] traj persist failed: {_nprg_e}", flush=True)
            # ── end NPRG pre-gate ──

            # Exp J: if similarity gate already rejected this patch, skip gate evaluation
            # and treat as a failed attempt (do not add to candidates)
            if _dp_rejected:
                print(f"    [exp-j] skipping gate — patch rejected by similarity gate", flush=True)
                attempts_log.append({
                    "attempt": attempt,
                    "admission_reason": "similarity_rejected",
                    "patch_fp": patch_fingerprint(patch) if patch else None,
                    "gate_reason_codes": ["SIMILARITY_REJECTED"],
                    "exit_status": agent_exit,
                })
                last_failure = (
                    f"Your patch was REJECTED by the similarity gate "
                    f"({_dp_sim:.0%} similar to previous). "
                    "You must produce a fundamentally different fix."
                )
                continue  # skip to next attempt

            # ScopeLockGate / Near-miss: if rejected, skip gate and set focused feedback
            if _nm_rejected:
                if _sl_rejection_hint:
                    # ScopeLockGate v0.1 rejection — use structured hint from verdict
                    print(f"    [scope-lock] skipping gate — patch rejected by scope lock", flush=True)
                    attempts_log.append({
                        "attempt": attempt,
                        "admission_reason": "scope_lock_rejected",
                        "patch_fp": patch_fingerprint(patch) if patch else None,
                        "gate_reason_codes": ["SCOPE_LOCK_REJECTED"],
                        "exit_status": agent_exit,
                    })
                    last_failure = _sl_rejection_hint
                else:
                    # Legacy near-miss gate rejection
                    print(f"    [near-miss-gate] skipping gate — patch rejected by scope gate", flush=True)
                    attempts_log.append({
                        "attempt": attempt,
                        "admission_reason": f"near_miss_rejected:{_nm_legacy_reason}",
                        "patch_fp": patch_fingerprint(patch) if patch else None,
                        "gate_reason_codes": [_nm_legacy_reason.upper()],
                        "exit_status": agent_exit,
                    })
                    if _nm_legacy_reason == "near_miss_new_file":
                        last_failure = (
                            "SCOPE VIOLATION: Your near-miss repair introduced a NEW file. "
                            "You MUST only modify files from the previous attempt. "
                            "This is a focused repair — do not expand scope."
                        )
                    elif _nm_legacy_reason == "near_miss_patch_too_large":
                        last_failure = (
                            "SCOPE VIOLATION: Your near-miss repair was too large. "
                            "A near-miss repair must be SURGICAL — "
                            "find the exact condition or branch that fails and fix ONLY that."
                        )
                    else:
                        last_failure = f"SCOPE VIOLATION: {_nm_legacy_reason}"
                continue  # skip to next attempt

            if not patch:
                print(f"    [gate] EMPTY — no submission (exit={agent_exit})")
                attempts_log.append({
                    "attempt": attempt,
                    "admission_reason": "no_patch",
                    "patch_fp": None,
                    "gate_reason_codes": [],
                    "exit_status": agent_exit,
                })
                if agent_exit and "LimitsExceeded" in agent_exit:
                    last_failure = (
                        "You ran out of steps before submitting. "
                        "SKIP all exploration and testing this time. "
                        "Go DIRECTLY to the fix: read the failing test, identify the exact line to change, "
                        "make the minimal edit, then call submit IMMEDIATELY."
                    )
                else:
                    _jb = jingu_body or {}
                    _files_written_count = len(_jb.get("files_written", []))
                    _phase_recs = _jb.get("phase_records", [])
                    _analyze_rec = next((r for r in _phase_recs if r.get("phase") == "ANALYZE"), None)
                    _execute_rec = next((r for r in _phase_recs if r.get("phase") == "EXECUTE"), None)
                    _has_root_cause = bool(_analyze_rec and _analyze_rec.get("root_cause"))
                    _has_plan = bool(_analyze_rec and _analyze_rec.get("plan")) or bool(_execute_rec and _execute_rec.get("plan"))
                    _execution_ready = bool(_execute_rec or _has_plan)
                    if _files_written_count == 0 and _analyze_rec and _execution_ready:
                        _rc_snippet = ""
                        if _has_root_cause:
                            _rc_snippet = f" Root cause from your analysis: {_analyze_rec['root_cause'][:120]}"
                        print(
                            f"    [execution-gate] EXECUTION_NO_MATERIALIZATION"
                            f" files_written={_files_written_count}"
                            f" has_root_cause={_has_root_cause}"
                            f" has_plan={_has_plan}"
                            f" execute_rec={_execute_rec is not None}",
                            flush=True,
                        )
                        last_failure = (
                            "EXECUTION REQUIRED: You identified the root cause but never edited any file. "
                            "Analysis is complete. You MUST write the patch NOW.\n\n"
                            "MANDATORY this attempt:\n"
                            "1. Do NOT re-read files or re-analyze.\n"
                            "2. Open the exact file identified in your analysis.\n"
                            "3. Make the minimal code change to fix the root cause.\n"
                            "4. Run the required tests.\n"
                            "5. Call submit.\n\n"
                            "Failure to edit at least one file = attempt counts as FAILED."
                            + (_rc_snippet if _rc_snippet else "")
                        )
                    elif _files_written_count == 0 and _analyze_rec and not _execution_ready:
                        print(
                            f"    [execution-gate] ANALYZE_NOT_READY"
                            f" files_written={_files_written_count}"
                            f" has_root_cause={_has_root_cause}"
                            f" has_plan={_has_plan}",
                            flush=True,
                        )
                        last_failure = "No patch was generated"
                    elif _files_written_count == 0 and attempt > 1:
                        # Early stall detector: 2+ attempts with 0 files written
                        # Derive candidate files and force replan
                        _stall_candidates = derive_candidate_files(
                            self._instance,
                            cv_result=_jb.get("controlled_verify"),
                            verify_history=_jb.get("verify_history"),
                        )
                        _stall_hint = (
                            "[STALL DETECTED — NO FILES WRITTEN IN MULTIPLE ATTEMPTS]\n\n"
                            "You have failed to write any files in this attempt.\n"
                            "Your exploration is not converging. You MUST change strategy.\n\n"
                        )
                        if _stall_candidates:
                            _stall_hint += (
                                "CANDIDATE FILES (derived from test failures and problem statement):\n"
                                + "\n".join(f"  - {f}" for f in _stall_candidates)
                                + "\n\nStart your investigation at these files.\n"
                            )
                        _stall_hint += (
                            "MANDATORY:\n"
                            "1. Do NOT repeat your previous exploration path\n"
                            "2. Go DIRECTLY to a specific file and make a concrete change\n"
                            "3. If unsure, pick the MOST LIKELY file and try a minimal fix\n"
                            "4. Submit SOMETHING — a failed attempt with a patch is better than no patch"
                        )
                        last_failure = _stall_hint
                        _next_attempt_start_phase = "DESIGN"
                        print(
                            f"    [stall-detector] NO_PATCH attempt={attempt} "
                            f"candidates={_stall_candidates}",
                            flush=True,
                        )
                    else:
                        last_failure = "No patch was generated"
                t_gate.stop()
                continue

            # Re-save traj with updated jingu_body (design_admission + execution_admission)
            # The traj was saved inside run_attempt() before these fields were added.
            if jingu_body:
                _attempt_dir = self._output_dir / f"attempt_{attempt}"
                _traj_resave = _attempt_dir / instance_id / f"{instance_id}.traj.json"
                if _traj_resave.exists():
                    try:
                        _traj_data = json.loads(_traj_resave.read_text())
                        _traj_data["jingu_body"] = jingu_body
                        _traj_resave.write_text(json.dumps(_traj_data))
                    except Exception as _e:
                        print(f"    [traj-resave] failed: {_e}", flush=True)

            patch = normalize_patch(patch)

            if self._mode == "baseline":
                score = score_patch(patch)
                fp = patch_fingerprint(patch)
                print(f"    [gate] BASELINE (no gate)  score={score:.0f}  lines={len(patch.splitlines())}")
                attempts_log.append({
                    "attempt": attempt,
                    "admission_reason": "baseline_no_gate",
                    "patch_fp": fp,
                    "gate_reason_codes": [],
                    "exit_status": agent_exit,
                })
                _cv_bl = (jingu_body or {}).get("controlled_verify") or {}
                candidates.append({"attempt": attempt, "patch": patch, "score": score,
                                    "gate_code": "BASELINE_NO_GATE",
                                    "cv_eval_resolved": _cv_bl.get("eval_resolved"),
                                    "cv_p2p_failed": _cv_bl.get("p2p_failed", 0) or 0,
                                    "cv_f2p_passed": _cv_bl.get("f2p_passed", 0) or 0})
                last_failure = ""
                agent_exit = None
            elif GATE_MODE == "trust_gate":
                attempt_dir = self._output_dir / f"attempt_{attempt}"
                traj_path = attempt_dir / instance_id / f"{instance_id}.traj.json"
                gate_result = evaluate_patch_from_traj(
                    patch_text=patch,
                    traj_path=traj_path if traj_path.exists() else None,
                    exit_status=agent_exit,
                    proposal_id=f"{instance_id}-attempt-{attempt}",
                    jingu_body=jingu_body,
                )
                exp = gate_result.explanation
                exp_str = (f"units={exp.total_units} approved={exp.approved} "
                           f"downgraded={exp.downgraded} rejected={exp.rejected}"
                           if exp else "no explanation")
                admission = classify_admission(gate_result, patch, agent_exit)
                fp = patch_fingerprint(patch)
                attempts_log.append({
                    "attempt": attempt,
                    "admission_reason": admission,
                    "patch_fp": fp,
                    "gate_reason_codes": gate_result.reason_codes,
                    "exit_status": agent_exit,
                })
                if gate_result.admitted:
                    score = score_patch(patch)
                    patch_lines = len(patch.splitlines())
                    grade = gate_result.gate_code
                    print(f"    [gate] {grade}  score={score:.0f}  lines={patch_lines}  {exp_str}")
                    print(f"    [telemetry] admission={admission}  files={fp['files']}  "
                          f"hunks={fp['hunks']}  +{fp['lines_added']}/-{fp['lines_removed']}")
                    t_gate.stop()

                    # CV-aware candidate: store verify signals for outcome-aware selection
                    _cv = (jingu_body or {}).get("controlled_verify") or {}

                    # Multi-candidate direction selection: if CV didn't resolve,
                    # generate alternative patch with different target_files and test it.
                    _sel_patch = patch
                    _sel_cv = _cv
                    _sel_source = "original"
                    if not _cv.get("eval_resolved") and _cv.get("f2p_failed", 0):
                        try:
                            from candidate_selection import (
                                CANDIDATE_SELECTION_ENABLED,
                                generate_alternative_candidate,
                                select_better_candidate,
                            )
                            if CANDIDATE_SELECTION_ENABLED:
                                _sel_cid = (self._state.container_id
                                            if self._state else None)
                                if _sel_cid:
                                    _alt = generate_alternative_candidate(
                                        instance=self._instance,
                                        container_id=_sel_cid,
                                        current_patch=patch,
                                        cv_result=_cv,
                                    )
                                    if _alt is not None:
                                        _sel_patch, _sel_cv, _sel_reason = (
                                            select_better_candidate(patch, _cv, _alt)
                                        )
                                        if _sel_patch != patch:
                                            _sel_source = "alternative"
                                            print(f"    [candidate-sel] SWAPPED to alternative: "
                                                  f"{_sel_reason}", flush=True)
                                        else:
                                            print(f"    [candidate-sel] KEPT original: "
                                                  f"{_sel_reason}", flush=True)
                        except Exception as _sel_exc:
                            print(f"    [candidate-sel] error (non-fatal): "
                                  f"{str(_sel_exc)[:200]}", flush=True)

                    candidates.append({
                        "attempt": attempt,
                        "patch": _sel_patch,
                        "score": score,
                        "gate_code": gate_result.gate_code,
                        "gate_reason_codes": gate_result.reason_codes,
                        "cv_eval_resolved": _sel_cv.get("eval_resolved"),
                        "cv_p2p_failed": _sel_cv.get("p2p_failed", 0) or 0,
                        "cv_f2p_passed": _sel_cv.get("f2p_passed", 0) or 0,
                        "candidate_source": _sel_source,
                    })
                    # Patch bloat detection
                    if attempt >= 2 and len(attempts_log) >= 2:
                        prev = attempts_log[-2].get("patch_fp") or {}
                        prev_size = prev.get("lines_added", 0) + prev.get("lines_removed", 0)
                        curr_size = fp["lines_added"] + fp["lines_removed"]
                        if prev_size > 0 and curr_size > prev_size * 1.5:
                            print(f"    [bloat-warn] attempt {attempt} patch is {curr_size} lines "
                                  f"(+{curr_size - prev_size} vs attempt {attempt-1} {prev_size}). "
                                  f"Possible wrong direction.")
                    # B3: retry-controller
                    if attempt < self._max_attempts:
                        fail_to_pass = _parse_fail_to_pass(self._instance)
                        exec_feedback = build_execution_feedback(
                            jingu_body=jingu_body or {},
                            fail_to_pass_tests=fail_to_pass,
                            patch_fp=fp,
                        )
                        print(f"    [exec-feedback] {exec_feedback[:200]}")
                        # EFR enforcement
                        tests_ran = (jingu_body or {}).get("test_results", {}).get("ran_tests", False)
                        if tests_ran and not exec_feedback.strip():
                            raise RuntimeError(
                                "[EFR violation] tests ran but exec_feedback is empty. "
                                "build_execution_feedback() must extract test output."
                            )
                        # B4: cognition gate
                        _traj_path = self._output_dir / f"attempt_{attempt}" / instance_id / f"{instance_id}.traj.json"
                        _decl = None
                        _traj_msgs_for_signal: list[dict] = []
                        if _traj_path.exists():
                            try:
                                _traj_msgs_for_signal = json.loads(_traj_path.read_text()).get("messages", [])
                                _structured_decl = _try_parse_structured_output(_traj_msgs_for_signal)
                                if _structured_decl is not None:
                                    _decl = extract_from_structured(_structured_decl)
                                    print(f"    [cognition] extraction_method=structured", flush=True)
                                else:
                                    _last_msg = extract_last_agent_message(_traj_msgs_for_signal)
                                    _decl = extract_declaration(_last_msg)
                                if _decl:
                                    _signals = extract_patch_signals(patch)
                                    _cog = check_cognition(_decl, _signals)
                                    _cog_fb = format_cognition_feedback(_cog)
                                    if _cog_fb:
                                        print(f"    [cognition] violation: {_cog_fb[:200]}")
                                        exec_feedback = exec_feedback + "\n" + _cog_fb if exec_feedback else _cog_fb
                                    else:
                                        print(f"    [cognition] pass  type={_decl['type']}  signals={_signals}")
                                else:
                                    print(f"    [cognition] skip  (no FIX_TYPE declaration)")
                            except (json.JSONDecodeError, OSError):
                                pass
                        _steps_since_signal = compute_steps_since_last_signal(_traj_msgs_for_signal)
                        if _steps_since_signal > 0:
                            print(f"    [no-signal] steps_since_last_signal={_steps_since_signal}")
                        _principal_viol_codes = extract_principal_violation_codes(_decl)
                        if _principal_viol_codes:
                            print(f"    [principal-viol] {_principal_viol_codes}")
                        if RETRY_CONTROLLER_ENABLED:
                            _tests_now = _test_counts_by_attempt.get(attempt, -1)
                            _tests_prev = _test_counts_by_attempt.get(attempt - 1, -1)
                            _tests_delta = (_tests_now - _tests_prev) if _tests_now >= 0 and _tests_prev >= 0 else None
                            _progress_ok, _progress_code = check_test_progress_invariant(_tests_prev, _tests_now)
                            print(f"    [test-progress] ok={_progress_ok}  code={_progress_code}  "
                                  f"prev={_tests_prev}  now={_tests_now}  delta={_tests_delta}")
                            _inner_cv = (jingu_body or {}).get("controlled_verify") or {}
                            prev_fp = attempts_log[-2]["patch_fp"] if len(attempts_log) >= 2 else None
                            t_ctrl = Timer(f"B3 retry-controller attempt={attempt}", parent=t_inst)
                            retry_plan = build_retry_plan(
                                problem_statement=self._instance.get("problem_statement", ""),
                                patch_text=patch,
                                jingu_body=jingu_body or {},
                                fail_to_pass_tests=fail_to_pass,
                                gate_admitted=True,
                                gate_reason_codes=gate_result.reason_codes,
                                instance_id=instance_id,
                                patch_fp=fp,
                                prev_patch_fp=prev_fp,
                                exec_feedback=exec_feedback,
                                attempt=attempt,
                                steps_since_last_signal=_steps_since_signal,
                                principal_violation_codes=_principal_viol_codes,
                                strategy_table_path=STRATEGY_TABLE_PATH,
                                tests_delta=_tests_delta,
                                tests_passed_after=_tests_now,
                                controlled_verify=(jingu_body or {}).get("controlled_verify", {}),
                                patch_exists=bool(patch and patch.strip()),
                                inner_f2p_passed=_inner_cv.get("f2p_passed") if _inner_cv.get("f2p_passed") is not None else -1,
                                inner_f2p_total=(_inner_cv.get("f2p_passed") or 0) + (_inner_cv.get("f2p_failed") or 0),
                                inner_new_failures=_inner_cv.get("p2p_failed") or 0,
                            )
                            t_ctrl.stop()
                            # p179: override control_action based on TEST_PROGRESS_MONOTONICITY
                            if not _progress_ok and _progress_code == "TEST_REGRESSION":
                                print(f"    [test-progress-gate] REGRESSION detected — overriding to STOP_FAIL")
                                retry_plan = RetryPlan(
                                    root_causes=retry_plan.root_causes + [f"invariant=TEST_REGRESSION"],
                                    must_do=["Revert the direction of your fix — you made tests worse"],
                                    must_not_do=["Do not continue in the same direction as the previous attempt"],
                                    validation_requirement="Run required tests and confirm delta > 0",
                                    next_attempt_prompt=(
                                        "REGRESSION: Your previous patch made the tests worse. "
                                        "You must completely change your approach. "
                                        "Do NOT expand the previous change. "
                                        "Reread the failing tests from scratch and fix the actual root cause."
                                    ),
                                    control_action="STOP_FAIL",
                                    principal_violations=retry_plan.principal_violations,
                                )
                            elif not _progress_ok and _progress_code == "NO_TEST_PROGRESS":
                                _curr_hash = patch_content_hash(patch)
                                _prev_hash = patch_content_hash(_prev_raw_patch) if _prev_raw_patch else None
                                _same_patch = (_prev_hash is not None and _curr_hash == _prev_hash)
                                _patch_direction = "stuck" if _same_patch else "exploring"
                                print(f"    [outcome-gate] NO_PROGRESS direction={_patch_direction} "
                                      f"curr_hash={_curr_hash} prev_hash={_prev_hash}")
                                if _same_patch:
                                    print(f"    [test-progress-gate] NO_PROGRESS stuck — overriding to ADJUST (force change)")
                                    retry_plan = RetryPlan(
                                        root_causes=retry_plan.root_causes + ["invariant=NO_TEST_PROGRESS", "direction=stuck"],
                                        must_do=["Write a completely different patch — different approach or different file"],
                                        must_not_do=["Do not reuse any part of your previous patch"],
                                        validation_requirement="Run required tests and confirm delta > 0",
                                        next_attempt_prompt=(
                                            "NO PROGRESS + SAME PATCH: Your approach is stuck. "
                                            "You must write a fundamentally different fix. "
                                            "Abandon your current hypothesis entirely. "
                                            "Reread the failing tests with fresh eyes and form a new hypothesis."
                                        ),
                                        control_action="ADJUST",
                                        principal_violations=retry_plan.principal_violations,
                                    )
                                    _no_progress_streak = 0
                                else:
                                    _no_progress_streak += 1
                                    print(f"    [outcome_gate] consecutive_no_progress={_no_progress_streak} "
                                          f"strategy_change_forced={_no_progress_streak >= 2}")
                                    if _no_progress_streak >= 2:
                                        print(f"    [test-progress-gate] NO_PROGRESS exploring streak={_no_progress_streak} — FORCED STRATEGY CHANGE")
                                        retry_plan = RetryPlan(
                                            root_causes=retry_plan.root_causes + ["invariant=NO_TEST_PROGRESS", "direction=exploring", f"no_progress_streak={_no_progress_streak}"],
                                            must_do=[
                                                "ABANDON your current hypothesis entirely — it has failed multiple times",
                                                "Re-read the failing test to understand what it ACTUALLY checks",
                                                "Identify a completely different root cause",
                                                "Write a fundamentally different fix targeting different code",
                                            ],
                                            must_not_do=[
                                                "Do NOT make small variations of your previous patches",
                                                "Do NOT modify the same function or method as before",
                                                "Do NOT assume your previous diagnosis was correct",
                                            ],
                                            validation_requirement="Run required tests and confirm delta > 0",
                                            next_attempt_prompt=(
                                                f"STRATEGY CHANGE REQUIRED (attempt streak={_no_progress_streak}): "
                                                f"Your last {_no_progress_streak} attempts with DIFFERENT patches all failed to improve test results. "
                                                "This means your fundamental hypothesis about the bug is wrong. "
                                                "You MUST: "
                                                "(1) ABANDON your current hypothesis entirely. "
                                                "(2) Re-read the failing test to understand what it ACTUALLY checks. "
                                                "(3) Identify a completely different root cause. "
                                                "(4) Write a fundamentally different fix — different file or different function. "
                                                "Do NOT make small variations of previous patches."
                                            ),
                                            control_action="ADJUST",
                                            principal_violations=retry_plan.principal_violations,
                                        )
                                    else:
                                        print(f"    [test-progress-gate] NO_PROGRESS exploring — gentle ADJUST")
                                        if retry_plan.control_action == "CONTINUE":
                                            retry_plan = RetryPlan(
                                                root_causes=retry_plan.root_causes + ["invariant=NO_TEST_PROGRESS", "direction=exploring"],
                                                must_do=retry_plan.must_do,
                                                must_not_do=retry_plan.must_not_do,
                                                validation_requirement=retry_plan.validation_requirement,
                                                next_attempt_prompt=(
                                                    retry_plan.next_attempt_prompt
                                                    + "\n[Tests not yet improving — keep iterating, change approach if needed]"
                                                ),
                                                control_action="ADJUST",
                                                principal_violations=retry_plan.principal_violations,
                                            )
                            else:
                                _no_progress_streak = 0
                            # GovernancePack pipeline
                            _gov_ctx = GovExecutionContext(
                                jingu_body=jingu_body or {},
                                fail_to_pass=fail_to_pass,
                                attempt=attempt,
                                instance_id=instance_id,
                                patch_text=patch,
                            )
                            _pack_decision = run_governance_packs(_gov_ctx)
                            if _pack_decision and _pack_decision.action == "REROUTE":
                                retry_plan = override_retry_plan_from_pack(retry_plan, _pack_decision)
                            # P2: Enrich retry_plan with prediction error feedback
                            _pred_err_data = (jingu_body or {}).get("prediction_error", {})
                            _pred_err_type = _pred_err_data.get("error_type", "")
                            _pred_feedback = _pred_err_data.get("feedback", "")
                            if _pred_err_type in ("prediction_wrong_direction", "prediction_partial") and _pred_feedback:
                                _pred_repair = _pred_err_data.get("repair_target", "")
                                retry_plan = RetryPlan(
                                    root_causes=retry_plan.root_causes + [f"prediction_error={_pred_err_type}"],
                                    must_do=retry_plan.must_do,
                                    must_not_do=retry_plan.must_not_do,
                                    validation_requirement=retry_plan.validation_requirement,
                                    next_attempt_prompt=(
                                        f"[PREDICTION ERROR — {_pred_err_type.upper()}]\n"
                                        f"{_pred_feedback}\n\n"
                                        + retry_plan.next_attempt_prompt
                                    ),
                                    control_action=retry_plan.control_action,
                                    principal_violations=retry_plan.principal_violations,
                                )
                                print(
                                    f"    [p2-prediction] enriched retry_plan:"
                                    f" error={_pred_err_type}"
                                    f" repair_target={_pred_repair}"
                                    f" severity={_pred_err_data.get('severity', '?')}",
                                    flush=True,
                                )
                            # ── Direction Reconsideration v0.1 ──────────────
                            _recon_telemetry = {}
                            try:
                                from direction_reconsideration import (
                                    should_trigger as _recon_should_trigger,
                                    generate_reconsideration as _gen_recon,
                                    build_recon_prompt_block as _build_recon_block,
                                    build_recon_telemetry as _build_recon_telem,
                                )
                                _recon_cv = (jingu_body or {}).get("controlled_verify") or {}
                                _recon_ft = next(
                                    (rc.split("=", 1)[1] for rc in retry_plan.root_causes
                                     if rc.startswith("failure_type=") and not rc.startswith("failure_type_v2=")),
                                    "",
                                )
                                _recon_ft_v2 = next(
                                    (rc.split("=", 1)[1] for rc in retry_plan.root_causes
                                     if rc.startswith("failure_type_v2=")),
                                    "",
                                )
                                _recon_outcome = next(
                                    (rc.split("=", 1)[1] for rc in retry_plan.root_causes
                                     if rc.startswith("outcome=")),
                                    "",
                                )
                                _recon_triggered = _recon_should_trigger(_recon_cv, _recon_ft, _recon_ft_v2, _recon_outcome)
                                if _recon_triggered:
                                    _recon_prev_files = (jingu_body or {}).get("files_written", [])
                                    _recon_result = _gen_recon(
                                        instance=self._instance,
                                        prev_files=_recon_prev_files,
                                        patch_text=patch,
                                        cv_result=_recon_cv,
                                    )
                                    if _recon_result:
                                        _recon_block = _build_recon_block(_recon_result)
                                        retry_plan = RetryPlan(
                                            root_causes=retry_plan.root_causes + ["direction_reconsideration=applied"],
                                            must_do=retry_plan.must_do,
                                            must_not_do=retry_plan.must_not_do + [
                                                "Do NOT repeat the same files/approach as the previous attempt"
                                            ],
                                            validation_requirement=retry_plan.validation_requirement,
                                            next_attempt_prompt=(
                                                _recon_block + "\n\n" + retry_plan.next_attempt_prompt
                                            ),
                                            control_action=retry_plan.control_action,
                                            principal_violations=retry_plan.principal_violations,
                                        )
                                        print(f"    [dir-recon] INJECTED into retry_plan "
                                              f"alt_files={_recon_result['alternative_files']} "
                                              f"direction_changed={_recon_result['direction_changed']}",
                                              flush=True)
                                    _recon_telemetry = _build_recon_telem(_recon_result, _recon_triggered)
                                else:
                                    _recon_telemetry = _build_recon_telem(None, False)
                            except ImportError:
                                pass
                            except Exception as _recon_exc:
                                print(f"    [dir-recon] error (non-fatal): {_recon_exc}", flush=True)
                            print(f"    [retry-ctrl] action={retry_plan.control_action}  "
                                  f"root_causes={retry_plan.root_causes}")
                            print(f"    [retry-ctrl] must_not_do={retry_plan.must_not_do}")
                            print(f"    [retry-ctrl] hint={retry_plan.next_attempt_prompt[:200]}")
                            # Store strategy metadata
                            _strategy_failure_class = next(
                                (rc.split("=", 1)[1] for rc in retry_plan.root_causes if rc.startswith("failure_type=") and not rc.startswith("failure_type_v2=")),
                                "unknown",
                            )
                            _strategy_failure_class_v2 = next(
                                (rc.split("=", 1)[1] for rc in retry_plan.root_causes if rc.startswith("failure_type_v2=")),
                                "signal_missing",
                            )
                            _strategy_entries.append({
                                "attempt": attempt,
                                "failure_class": _strategy_failure_class,
                                "failure_class_v2": _strategy_failure_class_v2,
                                "control_action": retry_plan.control_action,
                                "steps_since_signal": _steps_since_signal,
                                "enforced_violations": retry_plan.principal_violations,
                                "hint_used": retry_plan.next_attempt_prompt[:300],
                                "tests_passed_count": _tests_now,
                                "tests_passed_prev": _tests_prev,
                                "tests_delta": _tests_delta,
                                "progress_code": _progress_code,
                                "files_written_paths": (jingu_body or {}).get("files_written", []),
                                "direction_reconsideration": _recon_telemetry or {},
                            })
                            # B3-CP: update reasoning state
                            _cv_passed = (_strategy_failure_class_v2 == "verified_pass")
                            _verify_partial = extract_verify_signals(controlled_verify_passed=_cv_passed)
                            cp_state_holder[0] = update_reasoning_state(
                                cp_state_holder[0], normalize_signals(_verify_partial)
                            )
                            _cp_state_now = cp_state_holder[0]
                            cp_verdict = decide_next(_cp_state_now)
                            _iid_short = instance_id.split("__")[-1] if "__" in instance_id else instance_id
                            print(f"    [control-plane] instance={_iid_short} attempt={attempt}"
                                  f" state=phase:{_cp_state_now.phase}"
                                  f" step:{_cp_state_now.step_index} no_progress:{_cp_state_now.no_progress_steps}"
                                  f" task_success:{_cp_state_now.task_success}")
                            print(f"    [control-plane] instance={_iid_short} attempt={attempt} verdict={cp_verdict}")
                            if isinstance(cp_verdict, VerdictStop):
                                # p230: log early_stop from control-plane verdict
                                try:
                                    if self._decision_logger is not None:
                                        from decision_logger import DecisionEvent
                                        self._decision_logger.log(DecisionEvent(
                                            decision_type="early_stop",
                                            step_n=-1,
                                            timestamp_ms=time.time() * 1000,
                                            verdict="stop",
                                            reason_text=f"cp_verdict_stop:{cp_verdict.reason}",
                                        ))
                                except Exception:
                                    pass
                                print(f"    [control-plane] instance={_iid_short} STOPPING — reason={cp_verdict.reason}")
                                _tr_cpv = (jingu_body or {}).get("test_results", {})
                                _pr_cpv = build_phase_result(
                                    str(cp_state_holder[0].phase).upper(),
                                    has_patch=(_attempt_monitor._prev_patch_non_empty if _attempt_monitor else False),
                                    has_inner_verify=len(_attempt_monitor.verify_history) > 0 if _attempt_monitor else False,
                                    test_results=_tr_cpv,
                                    no_progress_steps=cp_state_holder[0].no_progress_steps,
                                    early_stop_reason=cp_verdict.reason,
                                    files_written=len((jingu_body or {}).get("files_written", [])),
                                )
                                _pr_cpv_route, _pr_cpv_target, _ = route_from_phase_result(_pr_cpv)
                                print(
                                    f"  [phase_result] phase={_pr_cpv.phase}"
                                    f" outcome={_pr_cpv.outcome}"
                                    f" verdict={_pr_cpv.verdict}"
                                    f" route={_pr_cpv_route}"
                                    f" target={_pr_cpv_target or '-'}"
                                    f" trust={_pr_cpv.trust_score or '-'}"
                                    f" reason={_pr_cpv.judge_reason}",
                                    flush=True,
                                )
                                break
                            if isinstance(cp_verdict, VerdictRedirect):
                                print(f"    [control-plane] instance={_iid_short} REDIRECT → forcing ADJUST  reason={cp_verdict.reason}")
                                import dataclasses as _dc
                                retry_plan = _dc.replace(
                                    retry_plan,
                                    control_action="ADJUST",
                                    next_attempt_prompt=(
                                        retry_plan.next_attempt_prompt
                                        + f"\n\n[Control-plane redirect: {cp_verdict.reason} — re-examine environment assumptions before patching]"
                                    ),
                                )

                            if retry_plan.control_action in ("STOP_FAIL", "STOP_NO_SIGNAL"):
                                # p230: log early_stop from retry controller
                                try:
                                    if self._decision_logger is not None:
                                        from decision_logger import DecisionEvent
                                        self._decision_logger.log(DecisionEvent(
                                            decision_type="early_stop",
                                            step_n=-1,
                                            timestamp_ms=time.time() * 1000,
                                            verdict="stop",
                                            reason_text=f"retry_ctrl:{retry_plan.control_action}",
                                            signals_evaluated={"root_causes": retry_plan.root_causes[:5]},
                                        ))
                                except Exception:
                                    pass
                                print(f"    [retry-ctrl] STOPPING — action={retry_plan.control_action}")
                                _tr_sf = (jingu_body or {}).get("test_results", {})
                                _pr_sf = build_phase_result(
                                    str(cp_state_holder[0].phase).upper(),
                                    has_patch=(_attempt_monitor._prev_patch_non_empty if _attempt_monitor else False),
                                    has_inner_verify=len(_attempt_monitor.verify_history) > 0 if _attempt_monitor else False,
                                    test_results=_tr_sf,
                                    no_progress_steps=cp_state_holder[0].no_progress_steps,
                                    early_stop_reason=retry_plan.control_action.lower(),
                                    files_written=len((jingu_body or {}).get("files_written", [])),
                                )
                                _pr_sf_route, _pr_sf_target, _ = route_from_phase_result(_pr_sf)
                                print(
                                    f"  [phase_result] phase={_pr_sf.phase}"
                                    f" outcome={_pr_sf.outcome}"
                                    f" verdict={_pr_sf.verdict}"
                                    f" route={_pr_sf_route}"
                                    f" target={_pr_sf_target or '-'}"
                                    f" trust={_pr_sf.trust_score or '-'}"
                                    f" reason={_pr_sf.judge_reason}",
                                    flush=True,
                                )
                                break
                            if _strategy_failure_class_v2 == "verified_pass":
                                # p230: log early_stop from verified_pass
                                try:
                                    if self._decision_logger is not None:
                                        from decision_logger import DecisionEvent
                                        self._decision_logger.log(DecisionEvent(
                                            decision_type="early_stop",
                                            step_n=-1,
                                            timestamp_ms=time.time() * 1000,
                                            verdict="stop",
                                            reason_text="verified_pass",
                                        ))
                                except Exception:
                                    pass
                                print(f"    [retry-ctrl] STOPPING — verified_pass (controlled_verify tests_failed=0)")
                                _tr_vp = (jingu_body or {}).get("test_results", {})
                                _pr_vp = build_phase_result(
                                    str(cp_state_holder[0].phase).upper(),
                                    has_patch=(_attempt_monitor._prev_patch_non_empty if _attempt_monitor else False),
                                    has_inner_verify=len(_attempt_monitor.verify_history) > 0 if _attempt_monitor else False,
                                    test_results=_tr_vp,
                                    no_progress_steps=cp_state_holder[0].no_progress_steps,
                                    early_stop_reason="verified_pass",
                                    files_written=len((jingu_body or {}).get("files_written", [])),
                                )
                                _pr_vp_route, _pr_vp_target, _ = route_from_phase_result(_pr_vp)
                                print(
                                    f"  [phase_result] phase={_pr_vp.phase}"
                                    f" outcome={_pr_vp.outcome}"
                                    f" verdict={_pr_vp.verdict}"
                                    f" route={_pr_vp_route}"
                                    f" target={_pr_vp_target or '-'}"
                                    f" trust={_pr_vp.trust_score or '-'}"
                                    f" reason={_pr_vp.judge_reason}",
                                    flush=True,
                                )
                                break

                            # p230: log retry decision
                            try:
                                if self._decision_logger is not None:
                                    from decision_logger import DecisionEvent
                                    self._decision_logger.log(DecisionEvent(
                                        decision_type="retry_decision",
                                        step_n=-1,
                                        timestamp_ms=time.time() * 1000,
                                        verdict=retry_plan.control_action,
                                        reason_text=retry_plan.next_attempt_prompt[:200],
                                        signals_evaluated={
                                            "root_causes": retry_plan.root_causes[:5],
                                            "tests_delta": _tests_delta,
                                            "progress_code": _progress_code,
                                        },
                                    ))
                            except Exception:
                                pass

                            # ── P0.2: environment_failure early terminate ─────────
                            _fm_now = (jingu_body or {}).get("failure_mode")
                            if _fm_now == "environment_failure" and attempt > 1:
                                _prev_fm = getattr(self, '_prev_failure_mode', None)
                                if _prev_fm == "environment_failure":
                                    print(f"    [env-early-terminate] consecutive environment_failure "
                                          f"(attempt {attempt-1}→{attempt}) — STOPPING (non-retryable)",
                                          flush=True)
                                    jingu_body["no_progress_repeat"] = "environment_failure_consecutive"
                                    break
                            self._prev_failure_mode = _fm_now

                            # P0.1 (no_progress_repeat_gate) now runs in pre-gate section
                            # before `if not patch:` check — see "NPRG pre-gate" above.

                            # WS-4: Track approach direction for exploration enforcement
                            _approach_summary = _extract_approach_summary(jingu_body, patch, fp)
                            if _approach_summary:
                                _past_approach_summaries.append(_approach_summary)
                            last_failure = retry_plan.next_attempt_prompt
                            # EFR telemetry: base last_failure from retry_plan
                            print(f"    [efr-base] attempt={attempt} source=retry_plan "
                                  f"last_failure_len={len(last_failure)}", flush=True)
                            # Decision Quality v1: prediction error feedback
                            try:
                                from prediction_feedback import compute_prediction_error, build_prediction_error_hint
                                _decide_rec = next(
                                    (r for r in (jingu_body or {}).get("phase_records", [])
                                     if r.get("phase") == "DECIDE"),
                                    None,
                                )
                                if _decide_rec and _decide_rec.get("testable_hypothesis"):
                                    _cv = (jingu_body or {}).get("controlled_verify", {})
                                    _pred_err = compute_prediction_error(
                                        _decide_rec, _cv,
                                        actual_files_changed=fp.get("files", []) if fp else [],
                                    )
                                    _pred_hint = build_prediction_error_hint(_pred_err, _decide_rec)
                                    if _pred_hint:
                                        last_failure = _pred_hint + "\n\n" + last_failure
                                    print(f"    [prediction-error] score={_pred_err['score']:.2f} "
                                          f"type={_pred_err['error_type']} "
                                          f"hit={_pred_err['pass_hit']:.2f} "
                                          f"miss={_pred_err['pass_miss']:.2f}")
                            except ImportError:
                                pass  # prediction_feedback module not yet available
                            except Exception as _pe_exc:
                                print(f"    [prediction-error] error (non-fatal): {_pe_exc}")
                            _jb_ft = (jingu_body or {}).get("failure_type")
                            _jb_routing = (jingu_body or {}).get("failure_routing")
                            _jb_cv = (jingu_body or {}).get("controlled_verify") or {}
                            if _jb_ft and _jb_routing:
                                _patch_ctx = None
                                if _jb_ft == "wrong_direction":
                                    _patch_ctx = {
                                        "files_written": (jingu_body or {}).get("files_written"),
                                        "patch_summary": (jingu_body or {}).get("patch_summary"),
                                        "prev_root_cause": _prev_root_cause,
                                        "prev_strategy_type": _prev_strategy_type,
                                    }
                                elif _jb_ft == "near_miss":
                                    _patch_ctx = {
                                        "files_written": (jingu_body or {}).get("files_written"),
                                        "patch_summary": (jingu_body or {}).get("patch_summary"),
                                    }
                                # v0.3: near_miss → residual_gap_repair protocol
                                _v03_repair_mode = None
                                _v03_nm_state = None
                                _v03_payload = None
                                _v03_routing = _jb_routing  # default: use classifier routing
                                if _jb_ft == "near_miss":
                                    try:
                                        from failure_classifier import (
                                            classify_near_miss_state, get_near_miss_routing,
                                        )
                                        from repair_prompts import build_residual_gap_payload
                                        _v03_nm = classify_near_miss_state(
                                            _jb_cv, attempt, self._f2p_history,
                                        )
                                        if _v03_nm:
                                            _v03_repair_mode = _v03_nm.repair_mode
                                            _v03_nm_state = _v03_nm.to_dict()
                                            _v03_routing = get_near_miss_routing(_v03_nm)
                                            # Merge stdout into cv for payload extraction (not in cv_flat to avoid traj bloat)
                                            _v03_cv_with_stdout = {**_jb_cv, "stdout": getattr(self, "_last_cv_stdout", "")}
                                            _v03_payload = build_residual_gap_payload(_v03_cv_with_stdout, _v03_nm_state)
                                            _v03_tests = [t.test_name for t in (_v03_payload.failing_tests if _v03_payload else [])]
                                            _v03_hyp = _v03_payload.shared_gap_hypothesis if _v03_payload else None
                                            print(f"    [v03-near-miss] repair_mode={_v03_repair_mode} "
                                                  f"stall={_v03_nm.same_patch_suspected} "
                                                  f"backslide={_v03_nm.backslide_detected} "
                                                  f"gap={_v03_nm.residual_gap_size} "
                                                  f"route={_v03_routing['next_phase']} "
                                                  f"payload_tests={_v03_tests} "
                                                  f"hypothesis={_v03_hyp}",
                                                  flush=True)
                                    except Exception as _v03_exc:
                                        print(f"    [v03-near-miss] fallback (error: {_v03_exc})",
                                              flush=True)
                                _repair = build_repair_prompt(
                                    _jb_ft, _jb_cv, _v03_routing,
                                    patch_context=_patch_ctx,
                                    repair_mode=_v03_repair_mode,
                                    nm_state=_v03_nm_state,
                                    residual_payload=_v03_payload,
                                )
                                # Near-miss finisher: when f2p_ratio > 0.9, prepend targeted prompt
                                _nm_f2p_p = _jb_cv.get("f2p_passed", 0) or 0
                                _nm_f2p_f = _jb_cv.get("f2p_failed", 0) or 0
                                _nm_total = _nm_f2p_p + _nm_f2p_f
                                _nm_ratio = _nm_f2p_p / _nm_total if _nm_total > 0 else 0
                                if _jb_ft == "near_miss" and _nm_ratio > 0.9 and _nm_f2p_f > 0:
                                    _nm_finisher = build_near_miss_finisher_prompt(
                                        f2p_passed=_nm_f2p_p,
                                        f2p_failed=_nm_f2p_f,
                                        f2p_failing_names=_jb_cv.get("f2p_failing_names", []) or [],
                                        files_written=(jingu_body or {}).get("files_written", []),
                                    )
                                    _repair = _nm_finisher + "\n\n" + _repair
                                    print(f"    [near-miss-finisher] injected: "
                                          f"f2p={_nm_f2p_p}/{_nm_total} ratio={_nm_ratio:.3f} "
                                          f"failing={_jb_cv.get('f2p_failing_names', [])}", flush=True)
                                last_failure = _repair + "\n\n" + last_failure
                                # p-fix: propagate repair routing target to next attempt cp_state
                                _next_attempt_start_phase = _v03_routing['next_phase'].upper()
                                _last_failure_type = _jb_ft or ""
                                # EFR telemetry: structured repair consumed (retry_plan branch)
                                print(f"    [efr-consume] attempt={attempt} failure_type={_jb_ft} "
                                      f"repair_target={_jb_routing['next_phase']} "
                                      f"repair_len={len(_repair)} "
                                      f"has_evidence={'Evidence from previous attempt' in _repair} "
                                      f"has_phase_decl={'[REPAIR PHASE:' in _repair} "
                                      f"branch=retry_plan", flush=True)
                                print(f"    [repair-route] attempt={attempt} failure_type={_jb_ft} "
                                      f"next_phase={_jb_routing['next_phase']}", flush=True)
                            elif not _jb_ft:
                                # P1 fallback: route from failure_mode when CV absent
                                _jb_fm = (jingu_body or {}).get("failure_mode")
                                if _jb_fm:
                                    _fm_routing = route_from_failure_mode(_jb_fm)
                                    _fm_hint = f"[FAILURE MODE: {_jb_fm}] {_fm_routing['repair_goal']}"
                                    last_failure = _fm_hint + "\n\n" + last_failure
                                    _next_attempt_start_phase = _fm_routing['next_phase'].upper()
                                    _last_failure_type = f"fm:{_jb_fm}"
                                    print(f"    [repair-route-fm] attempt={attempt} failure_mode={_jb_fm} "
                                          f"next_phase={_fm_routing['next_phase']}", flush=True)
                            # WDRG v0.2: prediction error can also indicate wrong_direction
                            # when CV is absent (signal_missing). Ensures file-ban + direction
                            # search contract activates even without CV classification.
                            if (not _last_failure_type.startswith("wrong_direction")
                                    and _pred_err_type == "prediction_wrong_direction"):
                                _last_failure_type = "wrong_direction"
                                print(f"    [wdrg-v02] prediction_wrong_direction detected, "
                                      f"setting _last_failure_type=wrong_direction", flush=True)
                            if is_data_driven_routing_enabled():
                                try:
                                    _p216_phase = (jingu_body or {}).get("last_phase", "ANALYZE").upper()
                                    _p216_principal = (jingu_body or {}).get("top_failed_principal", "")
                                    if _p216_principal:
                                        _p216_next, _p216_strategy = route_failure_p216(_p216_phase, _p216_principal)
                                        _p216_prompt = get_strategy_prompt(_p216_strategy)
                                        last_failure = _p216_prompt + "\n\n" + last_failure
                                        # p-fix: data-driven routing overrides repair routing target
                                        _next_attempt_start_phase = _p216_next.upper()
                                        _last_failure_type = f"{_jb_ft or ''}+p216"
                                        print(f"    [p216-routing] attempt={attempt} phase={_p216_phase} "
                                              f"principal={_p216_principal} -> next={_p216_next} "
                                              f"strategy={_p216_strategy}", flush=True)
                                except Exception as _p216_exc:
                                    print(f"    [p216-routing] error (non-fatal): {_p216_exc}", flush=True)
                            # WS-4: Exploration enforcement — warn about repeated approaches
                            if len(_past_approach_summaries) >= 2:
                                _last_approach = _past_approach_summaries[-1]
                                _repeated = sum(1 for a in _past_approach_summaries[:-1] if a == _last_approach)
                                if _repeated > 0:
                                    _past_str = "\n".join(f"  attempt {i+1}: {a}" for i, a in enumerate(_past_approach_summaries))
                                    _exploration_warning = (
                                        f"EXPLORATION ENFORCEMENT: You have tried the same approach {_repeated + 1} times.\n"
                                        f"Past approaches:\n{_past_str}\n"
                                        f"You MUST try a DIFFERENT approach — different files, different root cause hypothesis.\n\n"
                                    )
                                    last_failure = _exploration_warning + last_failure
                                    print(f"    [ws4-exploration] REPEATED approach detected (count={_repeated + 1})")
                        else:
                            # WS-4: Track approach direction (else branch — no retry_plan)
                            _approach_summary = _extract_approach_summary(jingu_body, patch, fp)
                            if _approach_summary:
                                _past_approach_summaries.append(_approach_summary)
                            last_failure = exec_feedback
                            print(f"    [efr-base] attempt={attempt} source=exec_feedback "
                                  f"last_failure_len={len(last_failure)}", flush=True)
                            _jb_ft = (jingu_body or {}).get("failure_type")
                            _jb_routing = (jingu_body or {}).get("failure_routing")
                            _jb_cv = (jingu_body or {}).get("controlled_verify") or {}
                            if _jb_ft and _jb_routing:
                                _patch_ctx = None
                                if _jb_ft == "wrong_direction":
                                    _patch_ctx = {
                                        "files_written": (jingu_body or {}).get("files_written"),
                                        "patch_summary": (jingu_body or {}).get("patch_summary"),
                                        "prev_root_cause": _prev_root_cause,
                                        "prev_strategy_type": _prev_strategy_type,
                                    }
                                elif _jb_ft == "near_miss":
                                    _patch_ctx = {
                                        "files_written": (jingu_body or {}).get("files_written"),
                                        "patch_summary": (jingu_body or {}).get("patch_summary"),
                                    }
                                # v0.3: near_miss → residual_gap_repair protocol (no_retry_plan branch)
                                _v03_repair_mode2 = None
                                _v03_nm_state2 = None
                                _v03_payload2 = None
                                _v03_routing2 = _jb_routing
                                if _jb_ft == "near_miss":
                                    try:
                                        from failure_classifier import (
                                            classify_near_miss_state, get_near_miss_routing,
                                        )
                                        from repair_prompts import build_residual_gap_payload
                                        _v03_nm2 = classify_near_miss_state(
                                            _jb_cv, attempt, self._f2p_history,
                                        )
                                        if _v03_nm2:
                                            _v03_repair_mode2 = _v03_nm2.repair_mode
                                            _v03_nm_state2 = _v03_nm2.to_dict()
                                            _v03_routing2 = get_near_miss_routing(_v03_nm2)
                                            _v03_cv_with_stdout2 = {**_jb_cv, "stdout": getattr(self, "_last_cv_stdout", "")}
                                            _v03_payload2 = build_residual_gap_payload(_v03_cv_with_stdout2, _v03_nm_state2)
                                            _v03_tests2 = [t.test_name for t in (_v03_payload2.failing_tests if _v03_payload2 else [])]
                                            print(f"    [v03-near-miss] repair_mode={_v03_repair_mode2} "
                                                  f"stall={_v03_nm2.same_patch_suspected} "
                                                  f"backslide={_v03_nm2.backslide_detected} "
                                                  f"gap={_v03_nm2.residual_gap_size} "
                                                  f"route={_v03_routing2['next_phase']} "
                                                  f"payload_tests={_v03_tests2} "
                                                  f"branch=no_retry_plan",
                                                  flush=True)
                                    except Exception as _v03_exc2:
                                        print(f"    [v03-near-miss] fallback (error: {_v03_exc2})",
                                              flush=True)
                                _repair = build_repair_prompt(
                                    _jb_ft, _jb_cv, _v03_routing2,
                                    patch_context=_patch_ctx,
                                    repair_mode=_v03_repair_mode2,
                                    nm_state=_v03_nm_state2,
                                    residual_payload=_v03_payload2,
                                )
                                last_failure = _repair + "\n\n" + last_failure
                                # p-fix: propagate repair routing target to next attempt cp_state
                                _next_attempt_start_phase = _v03_routing2['next_phase'].upper()
                                _last_failure_type = _jb_ft or ""
                                # EFR telemetry: structured repair consumed (no retry_plan branch)
                                print(f"    [efr-consume] attempt={attempt} failure_type={_jb_ft} "
                                      f"repair_target={_v03_routing2['next_phase']} "
                                      f"repair_len={len(_repair)} "
                                      f"has_evidence={'Evidence from previous attempt' in _repair} "
                                      f"has_phase_decl={'[REPAIR PHASE:' in _repair} "
                                      f"branch=no_retry_plan", flush=True)
                                print(f"    [repair-route] attempt={attempt} failure_type={_jb_ft} "
                                      f"next_phase={_jb_routing['next_phase']}", flush=True)
                            elif not _jb_ft:
                                # P1 fallback: route from failure_mode when CV absent
                                _jb_fm = (jingu_body or {}).get("failure_mode")
                                if _jb_fm:
                                    _fm_routing = route_from_failure_mode(_jb_fm)
                                    _fm_hint = f"[FAILURE MODE: {_jb_fm}] {_fm_routing['repair_goal']}"
                                    last_failure = _fm_hint + "\n\n" + last_failure
                                    _next_attempt_start_phase = _fm_routing['next_phase'].upper()
                                    _last_failure_type = f"fm:{_jb_fm}"
                                    print(f"    [repair-route-fm] attempt={attempt} failure_mode={_jb_fm} "
                                          f"next_phase={_fm_routing['next_phase']}", flush=True)
                            # WDRG v0.2: prediction error can also indicate wrong_direction
                            # when CV is absent (signal_missing). Ensures file-ban + direction
                            # search contract activates even without CV classification.
                            if (not _last_failure_type.startswith("wrong_direction")
                                    and _pred_err_type == "prediction_wrong_direction"):
                                _last_failure_type = "wrong_direction"
                                print(f"    [wdrg-v02] prediction_wrong_direction detected, "
                                      f"setting _last_failure_type=wrong_direction", flush=True)
                            if is_data_driven_routing_enabled():
                                try:
                                    _p216_phase = (jingu_body or {}).get("last_phase", "ANALYZE").upper()
                                    _p216_principal = (jingu_body or {}).get("top_failed_principal", "")
                                    if _p216_principal:
                                        _p216_next, _p216_strategy = route_failure_p216(_p216_phase, _p216_principal)
                                        _p216_prompt = get_strategy_prompt(_p216_strategy)
                                        last_failure = _p216_prompt + "\n\n" + last_failure
                                        # p-fix: data-driven routing overrides repair routing target
                                        _next_attempt_start_phase = _p216_next.upper()
                                        _last_failure_type = f"{_jb_ft or ''}+p216"
                                        print(f"    [p216-routing] attempt={attempt} phase={_p216_phase} "
                                              f"principal={_p216_principal} -> next={_p216_next} "
                                              f"strategy={_p216_strategy}", flush=True)
                                except Exception as _p216_exc:
                                    print(f"    [p216-routing] error (non-fatal): {_p216_exc}", flush=True)
                            # ── P0.1/P0.2 (else branch): same gates apply ──────
                            _fm_now_e = (jingu_body or {}).get("failure_mode")
                            if _fm_now_e == "environment_failure" and attempt > 1:
                                _prev_fm_e = getattr(self, '_prev_failure_mode', None)
                                if _prev_fm_e == "environment_failure":
                                    print(f"    [env-early-terminate] consecutive environment_failure "
                                          f"(attempt {attempt-1}→{attempt}) — STOPPING",
                                          flush=True)
                                    jingu_body["no_progress_repeat"] = "environment_failure_consecutive"
                                    break
                            self._prev_failure_mode = _fm_now_e
                            # P0.1 (no_progress_repeat_gate) now runs in pre-gate section
                            # before `if not patch:` check — see "NPRG pre-gate" above.
                            # WS-4: Exploration enforcement (else branch — no retry_plan)
                            if len(_past_approach_summaries) >= 2:
                                _last_approach = _past_approach_summaries[-1]
                                _repeated = sum(1 for a in _past_approach_summaries[:-1] if a == _last_approach)
                                if _repeated > 0:
                                    _past_str = "\n".join(f"  attempt {i+1}: {a}" for i, a in enumerate(_past_approach_summaries))
                                    _exploration_warning = (
                                        f"EXPLORATION ENFORCEMENT: You have tried the same approach {_repeated + 1} times.\n"
                                        f"Past approaches:\n{_past_str}\n"
                                        f"You MUST try a DIFFERENT approach — different files, different root cause hypothesis.\n\n"
                                    )
                                    last_failure = _exploration_warning + last_failure
                                    print(f"    [ws4-exploration] REPEATED approach detected (count={_repeated + 1})")
                    else:
                        last_failure = ""
                    agent_exit = None
                else:
                    codes = ", ".join(gate_result.reason_codes)
                    print(f"    [gate] REJECTED  codes={codes}  {exp_str}")
                    if gate_result.error:
                        print(f"    [gate-error] {gate_result.error[:300]}")
                    print(f"    [telemetry] admission={admission}  files={fp['files']}  "
                          f"hunks={fp['hunks']}  +{fp['lines_added']}/-{fp['lines_removed']}")
                    hint = gate_result.retry_hint
                    if not hint:
                        if "APPLY_FAILED" in gate_result.reason_codes:
                            hint = ("Previous patch failed to apply. Check for merge conflicts "
                                    "or incorrect line numbers. Generate a clean diff.")
                        elif "PARSE_FAILED" in gate_result.reason_codes:
                            hint = ("Previous patch was malformed (missing ---, +++, @@ markers). "
                                    "Use git diff format exactly.")
                        else:
                            hint = f"Gate rejected patch ({codes}). Generate a better patch."
                    last_failure = hint
                    t_gate.stop()
                    continue
            else:
                # B0 fallback: structural check only
                sg = jingu_structural_check(patch)
                if not sg["pass"]:
                    print(f"    [gate] FAIL structural: {sg['code']} — {sg.get('message','')}")
                    last_failure = f"Structural gate failed: {sg['message']}"
                    t_gate.stop()
                    continue
                score = score_patch(patch)
                patch_lines = len(patch.splitlines())
                print(f"    [gate] OK  score={score:.0f}  lines={patch_lines}")
                t_gate.stop()
                _cv_st = (jingu_body or {}).get("controlled_verify") or {}
                candidates.append({"attempt": attempt, "patch": patch, "score": score,
                                    "gate_code": "STRUCTURAL_OK",
                                    "cv_eval_resolved": _cv_st.get("eval_resolved"),
                                    "cv_p2p_failed": _cv_st.get("p2p_failed", 0) or 0,
                                    "cv_f2p_passed": _cv_st.get("f2p_passed", 0) or 0})
                last_failure = ""
                agent_exit = None

            # ── Exp J: Dual-cause + dual-patch + hard enforcement + strategy taxonomy ──
            # Three layers of diversity enforcement:
            # Layer 1 (dual-cause): require different causal hypothesis + different strategy type
            # Layer 2 (dual-patch): Jaccard rejection prevents similar patches (at line ~1900)
            # Layer 3 (strategy taxonomy): expand hypothesis space with explicit repair patterns
            _dual_patch_enabled = __import__("os").environ.get("DUAL_PATCH", "1") != "0"
            if _dual_patch_enabled and attempt == 1 and patch and last_failure:
                _cv_dp = (jingu_body or {}).get("controlled_verify", {})
                _cv_resolved_dp = _cv_dp.get("eval_resolved", False)
                if not _cv_resolved_dp and _nprg_curr_files:
                    _patch_preview = patch[:1500]
                    if len(patch) > 1500:
                        _patch_preview += "\n... [truncated]"

                    # Use agent-declared repair_strategy_type from ANALYZE structured output
                    # Protocol Compiler: read via get_control_field (no silent fallback)
                    try:
                        from protocol_compiler import get_control_field
                        _prev_strategy = get_control_field(
                            _analyze_rec_dc, "repair_strategy_type", phase="ANALYZE"
                        )
                    except Exception:
                        _prev_strategy = _prev_strategy_type or ""
                        if not _prev_strategy:
                            print(f"    [exp-k] PROTOCOL: repair_strategy_type missing from ANALYZE record — strategy ban disabled", flush=True)

                    # Build the cause section with strategy ban
                    _cause_section = ""
                    if _prev_root_cause:
                        _rc_preview = _prev_root_cause[:800]
                        _cause_section = (
                            f"Your previous ROOT CAUSE HYPOTHESIS (PROVEN WRONG):\n"
                            f'"{_rc_preview}"\n\n'
                            f"Strategy type used: {_prev_strategy}\n\n"
                            "This hypothesis led to a patch that FAILED all tests. "
                            "The root cause analysis itself is wrong or incomplete.\n\n"
                        )

                    _dual_patch_prompt = (
                        "DUAL-CAUSE EXPLORATION REQUIRED (with strategy diversity).\n\n"
                        + _cause_section
                        + "Your previous patch based on that hypothesis:\n"
                        "```diff\n"
                        f"{_patch_preview}\n"
                        "```\n\n"
                        "This patch DID NOT fix the issue. The test still fails.\n\n"
                        "STRATEGY TAXONOMY — you MUST choose a DIFFERENT strategy type:\n"
                        + "".join(f"  {i+1}. {s}\n" for i, s in enumerate(__import__('strategy_registry').all_strategies()))
                        + "\n"
                        f"{'BANNED strategy: ' + _prev_strategy + ' (already tried and FAILED)' if _prev_strategy else 'Previous strategy type unknown — choose ANY different approach'}\n\n"
                        "You MUST now:\n"
                        "1. DISCARD your previous root cause hypothesis entirely\n"
                        "2. Re-read the FAILING TEST to understand what it ACTUALLY checks\n"
                        "3. Pick a DIFFERENT strategy type from the taxonomy above\n"
                        "4. Form a root cause that matches your chosen strategy\n"
                        "5. Produce a patch that follows from your NEW hypothesis\n\n"
                        "HARD CONSTRAINTS (violation = automatic rejection):\n"
                        "- Your patch will be compared to the previous one — "
                        "if >70% similar, it will be REJECTED\n"
                        f"- Files previously modified: {', '.join(sorted(_nprg_curr_files))}\n"
                        "- If modifying the same file, you MUST change DIFFERENT lines "
                        "with a DIFFERENT rationale\n"
                        "- State which strategy type you chose and why\n"
                    )
                    last_failure = _dual_patch_prompt + "\n" + last_failure
                    _has_cause = "with cause" if _prev_root_cause else "no cause"
                    print(f"    [exp-j] attempt=1 injected ({_has_cause}, "
                          f"strategy={_prev_strategy}, patch={len(_patch_preview)}c) "
                          f"ban files={sorted(_nprg_curr_files)}", flush=True)

            # ── NPRG deferred injection: prepend NPRG prompt AFTER retry controller ──
            # Two modes:
            # 1. Reactive (attempt>1): _nprg_prompt set by L1/L2 detection earlier
            # 2. Preemptive (attempt=1): same-approach ban (now superseded by dual-patch above)
            if _nprg_prompt and last_failure:
                last_failure = _nprg_prompt + "\n\n" + last_failure
                print(f"    [nprg_inject] prepended NPRG prompt ({len(_nprg_prompt)}c) to last_failure", flush=True)
            elif _nprg_prompt and not last_failure:
                last_failure = _nprg_prompt
                print(f"    [nprg_inject] set last_failure to NPRG prompt ({len(_nprg_prompt)}c)", flush=True)

            # ── Protocol-driven routing: override next_phase based on missing fields ──
            # ONLY when EFR routing has NOT already set a failure-type-specific route.
            # EFR routing (from classify_failure → get_routing) is a precise diagnosis;
            # protocol routing is a generic fallback for incomplete records.
            _efr_route_active = (
                bool(_last_failure_type)
                and bool(_next_attempt_start_phase)
                and _next_attempt_start_phase.upper() in ALL_PHASES
            )
            if _efr_route_active:
                print(
                    f"    [protocol-route] SKIP: EFR route active "
                    f"(failure_type={_last_failure_type} "
                    f"route={_next_attempt_start_phase})",
                    flush=True,
                )
            else:
                try:
                    from protocol_compiler import validate_record_protocol, _get_protocol_specs
                    _proto_specs = _get_protocol_specs()
                    _jb_proto = jingu_body or {}
                    _proto_recs = _jb_proto.get("phase_records", [])
                    _proto_analyze = next((r for r in _proto_recs if r.get("phase") == "ANALYZE"), None)
                    if _proto_analyze:
                        _proto_missing = validate_record_protocol(_proto_analyze, "ANALYZE", _proto_specs)
                        if _proto_missing:
                            _old_route = _next_attempt_start_phase
                            _next_attempt_start_phase = "ANALYZE"
                            _proto_hint = (
                                f"[PROTOCOL ROUTING] Your ANALYZE record is incomplete. "
                                f"Missing fields: {', '.join(_proto_missing)}. "
                                f"You must return to ANALYZE and provide these fields."
                            )
                            last_failure = _proto_hint + "\n\n" + (last_failure or "")
                            print(
                                f"    [protocol-route] OVERRIDE: {_old_route} -> ANALYZE "
                                f"missing={_proto_missing}",
                                flush=True,
                            )
                    elif not _proto_analyze and _proto_recs:
                        # Had phase records but no ANALYZE — route to ANALYZE
                        _old_route = _next_attempt_start_phase
                        _next_attempt_start_phase = "ANALYZE"
                        _proto_hint = (
                            "[PROTOCOL ROUTING] No ANALYZE record found. "
                            "You must complete ANALYZE phase first with all required fields."
                        )
                        last_failure = _proto_hint + "\n\n" + (last_failure or "")
                        print(
                            f"    [protocol-route] OVERRIDE: {_old_route} -> ANALYZE "
                            f"reason=no_analyze_record",
                            flush=True,
                        )
                except ImportError:
                    pass
                except Exception as _proto_route_exc:
                    print(f"    [protocol-route] error (non-fatal): {_proto_route_exc}", flush=True)

            # Execution admission routing: overlap=0.0 → route back to DESIGN
            # This is the only violation type that warrants routing intervention.
            # test_expansion and boundary_expansion are allowed (soft signal only).
            _ea = (jingu_body or {}).get("execution_admission", {})
            if _ea.get("violation_type") == "wrong_direction" and attempt < self._max_attempts:
                _old_route = _next_attempt_start_phase
                _next_attempt_start_phase = "DESIGN"
                _ea["execution_violation_routed_to_design"] = True
                # Build actionable retry hint for wrong_direction reroute
                _wd_design_files = _ea.get("design_target_files", [])
                _wd_written_files = _ea.get("actual_files_written", [])
                _wd_oos_files = _ea.get("out_of_scope_files", [])
                _wd_overlap = _ea.get("overlap", 0.0)
                # Derive candidate files from test failures + problem statement
                _wd_candidates = derive_candidate_files(
                    self._instance,
                    cv_result=(jingu_body or {}).get("controlled_verify"),
                    verify_history=(jingu_body or {}).get("verify_history"),
                )
                _wd_candidate_section = ""
                if _wd_candidates:
                    _wd_candidate_section = (
                        "\nCANDIDATE FILES (derived from test failures and problem statement):\n"
                        + "\n".join(f"  - {f}" for f in _wd_candidates)
                        + "\nThese files appeared in stack traces or are referenced by failing tests.\n"
                        "Start your investigation here.\n"
                    )
                _wd_hint = (
                    "[WRONG DIRECTION — EXECUTION ADMISSION FAILURE]\n\n"
                    "Your previous execution was classified as WRONG_DIRECTION.\n"
                    f"- Planned target files: {_wd_design_files}\n"
                    f"- Actually modified files: {_wd_written_files}\n"
                    f"- Out-of-scope files: {_wd_oos_files}\n"
                    f"- Overlap with plan: {_wd_overlap:.0%}\n\n"
                    "This means the executed files had ZERO overlap with the planned files.\n"
                    "This is NOT boundary drift — you worked on completely wrong files.\n"
                    f"{_wd_candidate_section}\n"
                    "You MUST now:\n"
                    "1. Re-evaluate your root cause analysis — the previous diagnosis led to wrong files\n"
                    "2. Explain WHY the previous target files were insufficient or wrong\n"
                    "3. Produce a NEW bounded target file plan (different files)\n"
                    "4. Do NOT simply restate the prior plan\n"
                    "5. Avoid broad exploratory search — commit to specific mechanism files\n"
                )
                last_failure = _wd_hint + "\n\n" + (last_failure or "")
                _ea["wrong_direction_retry_hint"] = _wd_hint
                _ea["derived_candidate_files"] = _wd_candidates
                print(
                    f"    [exec-admission-route] OVERRIDE: "
                    f"violation_type=wrong_direction overlap=0.0 "
                    f"old_route={_old_route} → new_route=DESIGN "
                    f"reason=agent wrote only files outside design commitment "
                    f"retry_hint_injected=True",
                    flush=True,
                )
            elif _ea.get("violation_type") in ("test_expansion", "boundary_expansion", "none"):
                _ea["execution_violation_routed_to_design"] = False

            # ── Final traj re-save: persist routing telemetry ──
            # execution_violation_routed_to_design and other routing fields
            # are set AFTER the initial traj save, so we re-save here.
            if jingu_body and _ea:
                # Ensure execution_admission reflects routing decisions
                jingu_body["execution_admission"] = _ea
                _attempt_dir = self._output_dir / f"attempt_{attempt}"
                _traj_final = _attempt_dir / instance_id / f"{instance_id}.traj.json"
                if _traj_final.exists():
                    try:
                        _traj_data = json.loads(_traj_final.read_text())
                        _traj_data["jingu_body"] = jingu_body
                        _traj_final.write_text(json.dumps(_traj_data))
                        print(f"    [traj-final-save] persisted routing telemetry", flush=True)
                    except Exception as _e:
                        print(f"    [traj-final-save] failed: {_e}", flush=True)

            # Save prescribed phase for next attempt's ack check (must be after all routing)
            if attempt < self._max_attempts:
                _next_attempt_start_phase_for_ack = _next_attempt_start_phase

                # ── Route fidelity reconciliation log ──
                _jb_routing_phase = (jingu_body or {}).get("failure_routing", {}).get("next_phase", "N/A")
                print(
                    f"    [route-fidelity] attempt={attempt} "
                    f"failure_type={_last_failure_type or 'none'} "
                    f"compiled_route={_jb_routing_phase} "
                    f"runtime_route={_next_attempt_start_phase} "
                    f"efr_active={_efr_route_active} "
                    f"match={_jb_routing_phase.upper() == _next_attempt_start_phase.upper() if _jb_routing_phase != 'N/A' else 'N/A'}",
                    flush=True,
                )

        t_inst.stop()

        inst_usage = _usage_tracker.per_instance().get(instance_id, {})
        llm_calls = inst_usage.get("api_calls", 0)
        t_inst.llm_calls = llm_calls

        delta = compute_attempt_delta(attempts_log)
        if delta:
            print(f"  [attempt_delta] files_changed={delta['files_changed']}  "
                  f"size_delta={delta['size_delta_lines']:+d}  "
                  f"same_reason={delta['same_admission_reason']}  "
                  f"{delta['a1_admission']} → {delta['a2_admission']}")

        # Flush strategy log entries
        if STRATEGY_LOG_PATH and _strategy_entries:
            _inst_final_admitted = bool(candidates)
            _admit_by_attempt = {
                a["attempt"]: a["admission_reason"] not in ("no_patch", "gate_reject_parse_failed",
                    "gate_reject_apply_failed", "gate_reject_empty_patch",
                    "gate_reject_too_many_files", "gate_reject_other", "gate_error")
                for a in attempts_log
            }
            _has_patch_by_attempt = {
                a["attempt"]: a["admission_reason"] != "no_patch"
                for a in attempts_log
            }
            for _se in _strategy_entries:
                _next_att = _se["attempt"] + 1
                _next_admitted = _admit_by_attempt.get(_next_att, False)
                _next_has_patch = _has_patch_by_attempt.get(_next_att, False)
                try:
                    log_strategy_entry(
                        make_strategy_entry(
                            instance_id=instance_id,
                            attempt_id=_se["attempt"],
                            failure_class=_se["failure_class"],
                            control_action=_se["control_action"],
                            steps_since_last_signal=_se["steps_since_signal"],
                            enforced_violation_codes=_se["enforced_violations"],
                            hint_used=_se["hint_used"],
                            next_attempt_admitted=_next_admitted,
                            next_attempt_has_patch=_next_has_patch,
                            instance_final_admitted=_inst_final_admitted,
                            outcome="solved" if _inst_final_admitted else "unsolved",
                            tests_delta=_se.get("tests_delta", None),
                            tests_passed_before=_se.get("tests_passed_prev", -1),
                            tests_passed_after=_se.get("tests_passed_count", -1),
                            files_written_paths=_se.get("files_written_paths", []),
                            failure_class_v2=_se.get("failure_class_v2", "signal_missing"),
                        ),
                        STRATEGY_LOG_PATH,
                    )
                except Exception as _log_err:
                    print(f"    [strategy-log] WARNING: failed to write entry: {_log_err}")

        # DHG Phase 1.5: empirical comparison summary
        if _dhg_hypotheses is not None and len(_dhg_attempt_results) >= 2:
            _r1 = _dhg_attempt_results.get(1, {})
            _r2 = _dhg_attempt_results.get(2, {})
            _emp_winner = "A" if _r1.get("f2p_ratio", 0) >= _r2.get("f2p_ratio", 0) else "B"
            _heur_winner = _dhg_attempt_results.get(1, {}).get("label", "A")  # attempt 1 always gets A
            # The heuristic winner from the telemetry
            if 1 in _dhg_attempt_results:
                # Reconstruct heuristic winner from the telemetry
                _heur_winner = "A" if len(_dhg_hypotheses.hypothesis_a.target_files) <= len(_dhg_hypotheses.hypothesis_b.target_files) else "B"
            _agree = _emp_winner == _heur_winner
            print(
                f"    [dhg-compare] heuristic_winner={_heur_winner} "
                f"empirical_winner={_emp_winner} agree={_agree} "
                f"A: f2p={_r1.get('f2p_passed',0)}/{_r1.get('f2p_total',0)} patch={'yes' if _r1.get('patch_generated') else 'no'} "
                f"B: f2p={_r2.get('f2p_passed',0)}/{_r2.get('f2p_total',0)} patch={'yes' if _r2.get('patch_generated') else 'no'}",
                flush=True,
            )

        if not candidates:
            # Grab failure_layer + failure_record from last attempt's jingu_body
            _last_fl = None
            _last_fr = None
            if jingu_body and isinstance(jingu_body, dict):
                _last_fl = jingu_body.get("failure_layer")
                _last_fr = jingu_body.get("failure_record")
            return InstanceResult(
                instance_id=instance_id,
                accepted=False,
                patch="",
                attempts=self._max_attempts,
                elapsed_s=t_inst.elapsed,
                model_usage=inst_usage,
                attempts_log=attempts_log,
                attempt_delta=delta,
                failure_layer=_last_fl,
                failure_record=_last_fr,
            )

        # Outcome-aware selection: CV results > heuristic score
        # Priority: (1) eval_resolved, (2) no p2p regression, (3) f2p pass count, (4) patch score
        def _attempt_rank(c):
            return (
                1 if c.get("cv_eval_resolved") else 0,   # resolved > not
                0 if (c.get("cv_p2p_failed") or 0) > 0 else 1,  # no regression > regression
                c.get("cv_f2p_passed") or 0,              # more f2p passed > fewer
                c["score"],                                # heuristic tie-break
            )
        best = max(candidates, key=_attempt_rank)
        gate_code = best.get("gate_code", "ADMITTED")
        best_admission = next(
            (a["admission_reason"] for a in attempts_log if a["attempt"] == best["attempt"]),
            gate_code.lower(),
        )
        # Log selection ranking for all candidates
        if len(candidates) > 1:
            for _c in sorted(candidates, key=_attempt_rank, reverse=True):
                _rank = _attempt_rank(_c)
                print(f"    [selection] attempt={_c['attempt']}  rank={_rank}  "
                      f"cv_resolved={_c.get('cv_eval_resolved')}  "
                      f"p2p_failed={_c.get('cv_p2p_failed', '?')}  "
                      f"f2p_passed={_c.get('cv_f2p_passed', '?')}")
        print(f"  [result] ACCEPTED  best_attempt={best['attempt']}  score={best['score']:.0f}  "
              f"gate={gate_code}  admission={best_admission}  elapsed={t_inst.elapsed:.1f}s  "
              f"bedrock_calls={llm_calls}  cost=${inst_usage.get('cost_usd', 0):.4f}")
        return InstanceResult(
            instance_id=instance_id,
            accepted=True,
            patch=best["patch"],
            attempts=self._max_attempts,
            best_attempt=best["attempt"],
            score=best["score"],
            gate_code=gate_code,
            gate_reason_codes=best.get("gate_reason_codes", []),
            admission_reason=best_admission,
            elapsed_s=t_inst.elapsed,
            model_usage=inst_usage,
            attempts_log=attempts_log,
            attempt_delta=delta,
        )


# ---------------------------------------------------------------------------
# JinguDefaultAgent — DefaultAgent subclass that calls on_attempt_end()
# ---------------------------------------------------------------------------

class JinguDefaultAgent(ProgressTrackingAgent):
    """ProgressTrackingAgent subclass with full Jingu governance lifecycle.

    Combines:
    - Per-step governance: delegates step() to jingu_agent.on_step_start / on_step_end
      (same as JinguProgressTrackingAgent)
    - End-of-attempt governance: overrides run() to call jingu_agent.on_attempt_end()
      after super().run() returns — while the Docker container is still alive.

    Extends ProgressTrackingAgent (not raw DefaultAgent) so that jingu_process_instance()
    can pass progress_manager= and instance_id= without extra plumbing.

    This class replaces the combination of:
    - JinguProgressTrackingAgent (step-level governance)
    - ScopedPatch on DefaultAgent.run (end-of-attempt governance, pre-p225-08)
    from run_with_jingu_gate.py.

    Container lifecycle invariant: on_attempt_end() runs before env goes out of scope
    in jingu_process_instance(). DefaultAgent.run() does not close the Docker env —
    env is GC'd after jingu_process_instance()'s try block exits.
    """

    def __init__(self, *args: Any, jingu_agent: "JinguAgent", **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.jingu_agent = jingu_agent

    def step(self) -> dict:
        from step_monitor_state import StopExecution

        self.jingu_agent.on_step_start(self, self.n_calls)
        result = super().step()
        decision = self.jingu_agent.on_step_end(self, self.n_calls)

        if decision.action == "stop":
            raise StopExecution(decision.reason)
        if decision.action == "redirect":
            self.messages.append({"role": "user", "content": decision.message})

        return result

    def run(self, *args: Any, **kwargs: Any) -> dict:
        from step_monitor_state import StopExecution

        try:
            result = super().run(*args, **kwargs)
        except StopExecution:
            # p241: StopExecution bypassed on_attempt_end → controlled_verify never ran.
            # Extract submission from agent messages (if agent submitted before budget exhausted).
            submission = ""
            for msg in reversed(self.messages):
                extra = msg.get("extra", {})
                if isinstance(extra, dict) and extra.get("submission"):
                    submission = extra["submission"]
                    break
            print(f"    [governance] StopExecution caught in JinguDefaultAgent.run()"
                  f" — running forced on_attempt_end (submission={'yes' if submission else 'no'})",
                  flush=True)
            self.jingu_agent.on_attempt_end(self, submission)
            raise  # re-raise so outer handler (run_attempt line 1162) still works

        submission = result.get("submission", "") if isinstance(result, dict) else ""
        self.jingu_agent.on_attempt_end(self, submission)
        return result


# ---------------------------------------------------------------------------
# JinguProgressTrackingAgent — ProgressTrackingAgent with governance hooks
# ---------------------------------------------------------------------------

class JinguProgressTrackingAgent(ProgressTrackingAgent):
    """ProgressTrackingAgent subclass that delegates step lifecycle to JinguAgent.

    Constructor accepts an extra *jingu_agent* keyword argument (the orchestrator).
    On each step(), it calls jingu_agent.on_step_start / on_step_end and acts on
    the returned StepDecision.
    """

    def __init__(self, *args: Any, jingu_agent: JinguAgent, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.jingu_agent = jingu_agent

    def step(self) -> dict:
        from step_monitor_state import StopExecution

        self.jingu_agent.on_step_start(self, self.n_calls)
        result = super().step()
        decision = self.jingu_agent.on_step_end(self, self.n_calls)

        if decision.action == "stop":
            raise StopExecution(decision.reason)
        if decision.action == "redirect":
            self.messages.append({"role": "user", "content": decision.message})

        return result
