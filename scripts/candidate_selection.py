"""
candidate_selection.py — Multi-candidate direction selection.

After an attempt produces a patch that doesn't resolve (f2p not all pass),
generate an alternative patch targeting different files via LLM, cv-test it,
and return both candidates for comparison.

Integration point: run() in jingu_agent.py, after cv_result processing.
"""
import os
import re
import time
import json
import subprocess


# ── Config ────────────────────────────────────────────────────────────────────

CANDIDATE_SELECTION_ENABLED = os.environ.get("JINGU_CANDIDATE_SELECTION", "0") == "1"
CANDIDATE_MODEL = os.environ.get(
    "JINGU_CANDIDATE_MODEL",
    "bedrock/global.anthropic.claude-sonnet-4-6",
)
CANDIDATE_MAX_TOKENS = 4096
CANDIDATE_TEMPERATURE = 0.3


# ── LLM prompt ───────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are an expert software engineer fixing a bug in a Django codebase.
You will be given:
1. A problem statement
2. A previous fix attempt that targeted certain files but did NOT fully resolve the issue
3. Test failure information

Your job: produce an ALTERNATIVE fix that targets DIFFERENT source files.
Do NOT modify the same files as the previous attempt.
NEVER target test files (tests/, test_*, *_test.py), configuration files, or setup files.
Only modify regular source code files.

Output a unified diff (git diff format) only. No explanation."""

_USER_PROMPT_TEMPLATE = """\
## Problem Statement
{problem_statement}

## Previous Attempt (FAILED — do NOT repeat this approach)
Files modified: {prev_files}
Patch:
```diff
{prev_patch}
```

## Test Failures
{test_failures}

## Repository
{repo} version {version}
Working directory: /testbed

## Instructions
1. Analyze WHY the previous attempt failed
2. Identify DIFFERENT source files that could fix this issue
3. Generate a complete unified diff targeting those different files
4. Do NOT modify: {prev_files}
5. NEVER target test files (tests/, test_*, *_test.py) — SWE-bench prohibits test modifications

Output ONLY the diff (git diff format). No markdown fences, no explanation."""


def _extract_files_from_patch(patch: str) -> list[str]:
    """Extract file paths from a unified diff."""
    files = []
    for line in patch.split("\n"):
        m = re.match(r"^diff --git a/.* b/(.*)", line)
        if m:
            files.append(m.group(1))
    return files


# ── Main function ─────────────────────────────────────────────────────────────

def generate_alternative_candidate(
    instance: dict,
    container_id: str,
    current_patch: str,
    cv_result: dict,
) -> dict | None:
    """
    Generate an alternative patch targeting different files and cv-test it.

    Returns dict with:
      patch: str (the alternative patch)
      cv: dict (controlled_verify result for alternative)
      files: list[str] (files modified by alternative)
      model: str (model used)
      elapsed_ms: float

    Returns None if:
      - candidate selection is disabled
      - LLM call fails
      - alternative patch is empty
      - alternative targets same files as current
    """
    if not CANDIDATE_SELECTION_ENABLED:
        return None

    t0 = time.monotonic()

    try:
        import litellm
    except ImportError:
        print("    [candidate-sel] SKIP: litellm not available", flush=True)
        return None

    problem_statement = instance.get("problem_statement", "")
    repo = instance.get("repo", "unknown")
    version = instance.get("version", "unknown")

    prev_files = _extract_files_from_patch(current_patch)
    prev_files_str = ", ".join(prev_files) if prev_files else "(none)"

    # Build test failure summary from cv_result
    test_failures = ""
    f2p_failing = cv_result.get("f2p_failing_names", [])
    output_tail = cv_result.get("output_tail", "")
    if f2p_failing:
        test_failures += "Failing FAIL_TO_PASS tests:\n"
        for t in f2p_failing[:10]:
            test_failures += f"  - {t}\n"
    if output_tail:
        test_failures += f"\nTest output:\n{output_tail[:1000]}\n"
    if not test_failures:
        test_failures = "(no test failure details available)"

    prompt = _USER_PROMPT_TEMPLATE.format(
        problem_statement=problem_statement[:2000],
        prev_files=prev_files_str,
        prev_patch=current_patch[:4000],
        test_failures=test_failures,
        repo=repo,
        version=version,
    )

    print(f"    [candidate-sel] generating alternative (model={CANDIDATE_MODEL}, "
          f"prev_files={prev_files_str})", flush=True)

    try:
        response = litellm.completion(
            model=CANDIDATE_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=CANDIDATE_MAX_TOKENS,
            temperature=CANDIDATE_TEMPERATURE,
            drop_params=True,
        )
        alt_patch = response.choices[0].message.content or ""
    except Exception as e:
        print(f"    [candidate-sel] LLM error: {str(e)[:200]}", flush=True)
        return None

    # Clean up: strip markdown fences if present
    alt_patch = alt_patch.strip()
    if alt_patch.startswith("```"):
        lines = alt_patch.split("\n")
        # Remove first and last fence lines
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        alt_patch = "\n".join(lines)

    if not alt_patch.strip():
        print("    [candidate-sel] SKIP: empty alternative patch", flush=True)
        return None

    # Log patch summary for debugging
    patch_lines = alt_patch.strip().split("\n")
    print(f"    [candidate-sel] alternative patch: {len(patch_lines)} lines, "
          f"first={patch_lines[0][:80] if patch_lines else '(empty)'}", flush=True)

    # Verify alternative targets different files
    alt_files = _extract_files_from_patch(alt_patch)
    if not alt_files:
        print("    [candidate-sel] SKIP: no files in alternative patch", flush=True)
        return None

    # Reject alternatives that only target test files
    source_files = [f for f in alt_files
                    if not (f.startswith("tests/") or "/tests/" in f
                            or re.match(r".*/test_[^/]*\.py$", f)
                            or f.endswith("_test.py"))]
    if not source_files:
        print(f"    [candidate-sel] SKIP: alternative only targets test files "
              f"({sorted(alt_files)})", flush=True)
        return None

    overlap = set(alt_files) & set(prev_files)
    if overlap == set(prev_files) and overlap == set(alt_files):
        print(f"    [candidate-sel] SKIP: alternative targets same files "
              f"({sorted(overlap)})", flush=True)
        return None

    print(f"    [candidate-sel] alternative targets: {sorted(alt_files)} "
          f"(overlap={sorted(overlap) if overlap else 'none'})", flush=True)

    # Quick CV test the alternative
    from controlled_verify import run_controlled_verify

    alt_cv = run_controlled_verify(
        patch_text=alt_patch,
        instance=instance,
        container_id=container_id,
        timeout_s=60,  # quick test — 60s cap
        apply_test_patch=True,
        verify_scope="targeted",
    )

    elapsed_ms = round((time.monotonic() - t0) * 1000, 1)

    alt_f2p_passed = alt_cv.get("f2p_passed", 0) or 0
    alt_f2p_failed = alt_cv.get("f2p_failed", 0) or 0
    alt_resolved = alt_cv.get("eval_resolved", False)
    alt_cv_kind = alt_cv.get("verification_kind", "unknown")
    alt_cv_error = alt_cv.get("error", "")
    alt_cv_exit = alt_cv.get("exit_code", -1)

    print(f"    [candidate-sel] alternative CV: f2p={alt_f2p_passed}/{alt_f2p_passed + alt_f2p_failed} "
          f"resolved={alt_resolved} kind={alt_cv_kind} exit={alt_cv_exit} elapsed={elapsed_ms:.0f}ms", flush=True)
    if alt_cv_error:
        print(f"    [candidate-sel] CV error: {alt_cv_error[:200]}", flush=True)
    if alt_f2p_passed + alt_f2p_failed == 0:
        print(f"    [candidate-sel] WARNING: 0 f2p tests ran — patch may have failed to apply", flush=True)
        tail = alt_cv.get("output_tail", "")
        if tail:
            print(f"    [candidate-sel] CV output_tail: {tail[:300]}", flush=True)

    return {
        "patch": alt_patch,
        "cv": alt_cv,
        "files": alt_files,
        "model": CANDIDATE_MODEL,
        "elapsed_ms": elapsed_ms,
        "prev_files": prev_files,
    }


def select_better_candidate(
    current_patch: str,
    current_cv: dict,
    alternative: dict,
) -> tuple[str, dict, str]:
    """
    Compare current and alternative candidates, return the better one.

    Selection priority (same as _attempt_rank in run()):
      1. eval_resolved (True > False)
      2. no p2p regression (fewer p2p failures better)
      3. more f2p passed
      4. fewer total changes (tie-break)

    Returns (best_patch, best_cv, selection_reason).
    """
    cur_resolved = current_cv.get("eval_resolved", False)
    alt_resolved = alternative["cv"].get("eval_resolved", False)

    cur_f2p = current_cv.get("f2p_passed", 0) or 0
    alt_f2p = alternative["cv"].get("f2p_passed", 0) or 0

    cur_p2p_fail = current_cv.get("p2p_failed", 0) or 0
    alt_p2p_fail = alternative["cv"].get("p2p_failed", 0) or 0

    def _rank(resolved, p2p_fail, f2p_pass):
        return (
            1 if resolved else 0,
            0 if p2p_fail > 0 else 1,
            f2p_pass,
        )

    cur_rank = _rank(cur_resolved, cur_p2p_fail, cur_f2p)
    alt_rank = _rank(alt_resolved, alt_p2p_fail, alt_f2p)

    print(f"    [candidate-sel] COMPARE: current={cur_rank} alternative={alt_rank}", flush=True)

    if alt_rank > cur_rank:
        reason = (f"alternative better: alt_rank={alt_rank} > cur_rank={cur_rank} "
                  f"(files={sorted(alternative['files'])})")
        print(f"    [candidate-sel] SELECTED: alternative — {reason}", flush=True)
        return alternative["patch"], alternative["cv"], reason
    else:
        reason = (f"current better: cur_rank={cur_rank} >= alt_rank={alt_rank} "
                  f"(current_files={_extract_files_from_patch(current_patch)})")
        print(f"    [candidate-sel] SELECTED: current — {reason}", flush=True)
        return current_patch, current_cv, reason
