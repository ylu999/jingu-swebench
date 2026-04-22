"""
direction_reconsideration.py — In-Loop Direction Reconsideration v0.1

After an attempt ends with f2p=0 and wrong_direction/insufficient_design failure,
force agent to reconsider direction by injecting structured re-analysis into
the next attempt's context.

Integration: called from jingu_agent.py retry flow, after retry_plan is built.
"""
import os
import re
import time
import json

# ── Config ────────────────────────────────────────────────────────────────────

DIRECTION_RECON_ENABLED = os.environ.get("JINGU_DIRECTION_RECON", "0") == "1"

RECON_MODEL = os.environ.get(
    "JINGU_RECON_MODEL",
    "bedrock/global.anthropic.claude-sonnet-4-6",
)
RECON_MAX_TOKENS = 2048
RECON_TEMPERATURE = 0.2


# ── Trigger check ────────────────────────────────────────────────────────────

_TRIGGER_FAILURE_TYPES = {
    "wrong_direction",
    "insufficient_design_depth",
    "target_missing_due_to_test_resolution",
}

# Do NOT trigger for these — they have different root causes
_NO_TRIGGER_FAILURE_TYPES = {
    "near_miss", "incomplete_fix", "patch_format_error",
    "regression", "no_patch", "budget_exhausted",
    "environment_failure", "verified_pass",
}


def should_trigger(
    cv_result: dict,
    failure_type: str,
    failure_type_v2: str,
    outcome: str = "",
) -> bool:
    """Check if direction reconsideration should trigger."""
    if not DIRECTION_RECON_ENABLED:
        return False

    eval_resolved = cv_result.get("eval_resolved", False)
    if eval_resolved:
        return False

    f2p_passed = cv_result.get("f2p_passed", 0) or 0
    f2p_failed = cv_result.get("f2p_failed", 0) or 0
    p2p_failed = cv_result.get("p2p_failed", 0) or 0

    # Must have f2p=0 with f2p_failed>0 (tests ran but none passed)
    if f2p_passed > 0 or f2p_failed == 0:
        return False

    # p2p regressions = low (agent at least didn't break things)
    if p2p_failed > 2:
        return False

    # Check failure type matches trigger conditions
    ft = failure_type.lower().strip() if failure_type else ""
    ft_v2 = failure_type_v2.lower().strip() if failure_type_v2 else ""
    oc = outcome.lower().strip() if outcome else ""

    if ft in _TRIGGER_FAILURE_TYPES:
        return True

    # Also trigger on wrong_direction from v2 classifier
    if ft_v2 in _TRIGGER_FAILURE_TYPES:
        return True

    # outcome= field from retry_plan (governance reroute classification)
    if oc in _TRIGGER_FAILURE_TYPES:
        return True

    # Fallback: if failure_type is "wrong_patch" and f2p=0, treat as wrong_direction
    if ft == "wrong_patch" and f2p_passed == 0 and f2p_failed > 0:
        return True

    return False


# ── LLM re-analysis ─────────────────────────────────────────────────────────

_RECON_SYSTEM = """\
You are an expert software engineer. A previous fix attempt for a bug has completely failed — \
zero failing tests now pass. Your job is to analyze WHY the previous direction was wrong and \
propose an ALTERNATIVE direction.

You must output EXACTLY three sections in this format:

WHY_PREVIOUS_DIRECTION_FAILED:
<1-3 sentences explaining why the previous files/approach were wrong>

ALTERNATIVE_TARGET_FILES:
<comma-separated list of source files that should be modified instead>

ALTERNATIVE_ROOT_CAUSE:
<1-3 sentences with a different root cause hypothesis>

Rules:
- Your alternative must differ materially from the previous attempt.
- If you still believe the previous area is correct, explicitly justify why the previous \
patch was insufficient rather than merely wrong.
- NEVER suggest test files (tests/, test_*, *_test.py).
- Focus on Django source code in the repository."""

_RECON_USER = """\
## Problem Statement
{problem_statement}

## Previous Attempt (FAILED — f2p=0/{f2p_total})
Files modified: {prev_files}
Patch summary:
```diff
{patch_excerpt}
```

## Failing Tests (none pass)
{failing_tests}

## Repository
{repo} version {version}

Analyze why the previous direction failed and propose an alternative."""


def generate_reconsideration(
    instance: dict,
    prev_files: list[str],
    patch_text: str,
    cv_result: dict,
) -> dict | None:
    """
    Call LLM to generate direction reconsideration.

    Returns dict with:
      why_failed: str
      alternative_files: list[str]
      alternative_root_cause: str
      direction_changed: bool
      raw_response: str
      elapsed_ms: float

    Returns None on failure.
    """
    t0 = time.monotonic()

    try:
        import litellm
    except ImportError:
        print("    [dir-recon] SKIP: litellm not available", flush=True)
        return None

    problem_statement = instance.get("problem_statement", "")
    repo = instance.get("repo", "unknown")
    version = instance.get("version", "unknown")

    f2p_failed = cv_result.get("f2p_failed", 0) or 0
    f2p_failing = cv_result.get("f2p_failing_names", [])

    failing_tests = ""
    if f2p_failing:
        failing_tests = "\n".join(f"  - {t}" for t in f2p_failing[:10])
    else:
        failing_tests = "(no test names available)"

    prev_files_str = ", ".join(prev_files) if prev_files else "(none)"
    patch_excerpt = patch_text[:3000] if patch_text else "(no patch)"

    prompt = _RECON_USER.format(
        problem_statement=problem_statement[:2000],
        prev_files=prev_files_str,
        patch_excerpt=patch_excerpt,
        f2p_total=f2p_failed,
        failing_tests=failing_tests,
        repo=repo,
        version=version,
    )

    print(f"    [dir-recon] generating reconsideration (model={RECON_MODEL}, "
          f"prev_files={prev_files_str})", flush=True)

    try:
        response = litellm.completion(
            model=RECON_MODEL,
            messages=[
                {"role": "system", "content": _RECON_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_tokens=RECON_MAX_TOKENS,
            temperature=RECON_TEMPERATURE,
            drop_params=True,
        )
        raw = response.choices[0].message.content or ""
    except Exception as e:
        print(f"    [dir-recon] LLM error: {str(e)[:200]}", flush=True)
        return None

    elapsed_ms = round((time.monotonic() - t0) * 1000, 1)

    # Parse structured output
    why_failed = _extract_section(raw, "WHY_PREVIOUS_DIRECTION_FAILED")
    alt_files_raw = _extract_section(raw, "ALTERNATIVE_TARGET_FILES")
    alt_root_cause = _extract_section(raw, "ALTERNATIVE_ROOT_CAUSE")

    if not why_failed or not alt_files_raw or not alt_root_cause:
        print(f"    [dir-recon] SKIP: incomplete response "
              f"(why={bool(why_failed)}, files={bool(alt_files_raw)}, cause={bool(alt_root_cause)})",
              flush=True)
        return None

    # Parse file list
    alt_files = [f.strip() for f in alt_files_raw.split(",") if f.strip()]
    # Filter out test files
    alt_files = [f for f in alt_files
                 if not (f.startswith("tests/") or "/tests/" in f
                         or re.match(r".*/test_[^/]*\.py$", f)
                         or f.endswith("_test.py"))]

    if not alt_files:
        print(f"    [dir-recon] SKIP: no valid alternative files after filtering", flush=True)
        return None

    # Material difference check
    direction_changed = _check_material_difference(prev_files, alt_files)

    print(f"    [dir-recon] result: why='{why_failed[:80]}' "
          f"alt_files={alt_files} alt_cause='{alt_root_cause[:80]}' "
          f"direction_changed={direction_changed} elapsed={elapsed_ms:.0f}ms", flush=True)

    return {
        "why_failed": why_failed,
        "alternative_files": alt_files,
        "alternative_root_cause": alt_root_cause,
        "direction_changed": direction_changed,
        "raw_response": raw,
        "elapsed_ms": elapsed_ms,
        "previous_files": prev_files,
    }


def _extract_section(text: str, header: str) -> str:
    """Extract content after a section header, up to the next header or end."""
    pattern = rf"{re.escape(header)}:\s*\n?(.*?)(?=\n[A-Z_]+:|$)"
    m = re.search(pattern, text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return ""


def _check_material_difference(prev_files: list[str], alt_files: list[str]) -> bool:
    """Check if alternative files differ materially from previous files."""
    if not prev_files or not alt_files:
        return True

    prev_set = set(prev_files)
    alt_set = set(alt_files)

    overlap = prev_set & alt_set
    if not overlap:
        return True  # Completely different files

    # Overlap ratio < 0.5 = material difference
    overlap_ratio = len(overlap) / max(len(prev_set | alt_set), 1)
    return overlap_ratio < 0.5


# ── Prompt injection ─────────────────────────────────────────────────────────

def build_recon_prompt_block(recon: dict) -> str:
    """Build the prompt block to inject into next attempt's context."""
    alt_files_str = ", ".join(recon["alternative_files"])
    prev_files_str = ", ".join(recon["previous_files"])

    block = (
        f"[DIRECTION RECONSIDERATION — your previous approach completely failed]\n\n"
        f"Previous attempt changed files: {prev_files_str}\n"
        f"That direction yielded f2p=0 (zero failing tests now pass).\n\n"
        f"Direction reconsideration analysis:\n"
        f"- Why previous direction failed: {recon['why_failed']}\n"
        f"- Alternative target files: {alt_files_str}\n"
        f"- Alternative root cause: {recon['alternative_root_cause']}\n\n"
        f"You MUST start from this reconsideration. "
        f"Do NOT repeat the previous approach. "
        f"Focus on the alternative target files and root cause above."
    )
    if not recon.get("direction_changed"):
        block += (
            "\n\nWARNING: The reconsideration still overlaps significantly with "
            "your previous approach. You MUST find a genuinely different direction."
        )
    return block


# ── Telemetry ────────────────────────────────────────────────────────────────

def build_recon_telemetry(recon: dict | None, triggered: bool) -> dict:
    """Build telemetry dict for run_report."""
    if not triggered:
        return {"direction_reconsideration_triggered": False}

    if recon is None:
        return {
            "direction_reconsideration_triggered": True,
            "direction_reconsideration_failed": True,
        }

    return {
        "direction_reconsideration_triggered": True,
        "previous_files": recon.get("previous_files", []),
        "alternative_target_files": recon.get("alternative_files", []),
        "direction_changed": recon.get("direction_changed", False),
        "why_failed": recon.get("why_failed", "")[:200],
        "alternative_root_cause": recon.get("alternative_root_cause", "")[:200],
        "elapsed_ms": recon.get("elapsed_ms", 0),
        "reconsideration_injected": True,
    }
