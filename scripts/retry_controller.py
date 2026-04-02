"""
retry_controller.py — B3 retry controller for SWE-bench pipeline.

Given attempt 1's patch + telemetry, produces a structured RetryPlan
that gives the agent specific diagnostic guidance for attempt 2.

This is the minimal "cognition" layer:
  failure → diagnosis → next strategy

NOT a judge of patch correctness (that is the benchmark's job).
Produces targeted prompts based on observable signal:
  - Which files were changed
  - Whether tests ran and what they produced
  - Whether the patch was admitted or rejected by gate
  - The problem statement and required tests

RetryPlan fields:
  root_causes:           what likely went wrong in attempt 1
  must_do:               concrete actions for attempt 2
  must_not_do:           things to avoid (based on attempt 1 failure pattern)
  validation_requirement: how to know the fix is correct
  next_attempt_prompt:   ready-to-inject hint string for the agent
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

RETRY_MODEL = "bedrock/global.anthropic.claude-sonnet-4-6"
RETRY_TEMPERATURE = 0.2
RETRY_MAX_TOKENS = 512

# ── Failure taxonomy (mirrors FAILURE_TAXONOMY.md in jingu-policy-core) ──────

FAILURE_TYPES = (
    "wrong_direction",      # FT1: right file, growing patch, no test improvement
    "exploration_loop",     # FT2: no patch produced, no tests ran
    "no_effect_patch",      # FT3: patch written, tests ran, failure unchanged
    "test_not_triggered",   # FT4: patch written, tests not run
    "environment_failure",  # FT5: non-zero exit, no test signal
    "unknown",
)


def classify_failure(
    jingu_body: dict,
    patch_fp: dict,
    prev_patch_fp: Optional[dict],
    exec_feedback: str,
) -> str:
    """
    Deterministic failure classification from observable signals.
    Detection priority: environment → test_not_triggered → exploration_loop
                        → wrong_direction → no_effect_patch → unknown.
    See FAILURE_TAXONOMY.md for full definitions.
    """
    exit_status = jingu_body.get("exit_status", "")
    test_results = jingu_body.get("test_results", {})
    tests_ran = test_results.get("ran_tests", False)
    test_passed = test_results.get("last_passed")
    files_written = jingu_body.get("files_written", [])

    patch_size = patch_fp.get("lines_added", 0) + patch_fp.get("lines_removed", 0)

    # FT5: environment failure — exit error, no test signal
    if exit_status not in ("Submitted", "") and not tests_ran and not files_written:
        return "environment_failure"

    # FT4: test not triggered — patch produced but tests not run
    if patch_size > 0 and not tests_ran:
        return "test_not_triggered"

    # FT2: exploration loop — no meaningful patch
    if patch_size == 0 or not files_written:
        return "exploration_loop"

    # FT1: wrong direction — same file, patch growing, tests still failing
    if (
        prev_patch_fp is not None
        and not test_passed
        and tests_ran
    ):
        prev_files = set(prev_patch_fp.get("files", []))
        curr_files = set(patch_fp.get("files", []))
        prev_size = prev_patch_fp.get("lines_added", 0) + prev_patch_fp.get("lines_removed", 0)
        if curr_files == prev_files and patch_size > prev_size + 10:
            return "wrong_direction"

    # FT3: no effect patch — patch written, tests ran, still failing
    if patch_size > 0 and tests_ran and not test_passed:
        return "no_effect_patch"

    return "unknown"


# Deterministic intervention mapping (no LLM needed for these)
_INTERVENTIONS: dict[str, dict] = {
    "wrong_direction": {
        "must_not_do": [
            "Do not expand the patch further in the same direction",
            "Do not add warning suppression or workarounds — fix the root algorithm",
        ],
        "must_do": [
            "Read the failing test carefully to understand what behavior is expected",
            "Fix the underlying logic, not the symptoms",
        ],
        "hint_prefix": (
            "Previous attempt modified the right file but the approach is incorrect — "
            "the patch kept growing without fixing the core logic. "
        ),
    },
    "exploration_loop": {
        "must_not_do": [
            "Do not continue reading files without committing to a fix",
        ],
        "must_do": [
            "Immediately identify the target file and function from the failing tests",
            "Make the minimal change and submit — do not over-explore",
        ],
        "hint_prefix": (
            "Previous attempt explored without producing a patch. "
            "Go directly to the fix: identify file → make minimal change → submit. "
        ),
    },
    "no_effect_patch": {
        "must_not_do": [
            "Do not modify code that is not exercised by the failing tests",
        ],
        "must_do": [
            "Trace the failing test to the exact function/line it exercises",
            "Modify only the code that directly affects the test outcome",
        ],
        "hint_prefix": (
            "Previous attempt changed code but had no effect on the failing tests. "
            "Find the exact code path the failing test exercises. "
        ),
    },
    "test_not_triggered": {
        "must_not_do": [
            "Do not submit without running the required tests first",
        ],
        "must_do": [
            "Run the FAIL_TO_PASS tests explicitly after your fix",
            "Only submit once you have seen the tests pass",
        ],
        "hint_prefix": (
            "Previous attempt did not run the required tests before submitting. "
        ),
    },
    "environment_failure": {
        "must_not_do": [
            "Do not modify application code before the environment is working",
        ],
        "must_do": [
            "Fix the environment or execution error first",
        ],
        "hint_prefix": "Previous attempt hit an environment error. Fix the execution environment first. ",
    },
    "unknown": {
        "must_not_do": [],
        "must_do": ["Re-read the failing tests and fix the root cause"],
        "hint_prefix": "Previous attempt did not fully solve the problem. ",
    },
}


RETRY_SYSTEM_PROMPT = """You are a debugging assistant for a software patch pipeline.

You will be given:
1. A problem statement (what needs to be fixed)
2. The tests that must pass
3. A patch that was attempted but did not fully solve the problem
4. Observable signals: which files were changed, whether tests ran, exit status

Your job: diagnose what went wrong and produce a targeted retry plan.

Be specific and concrete. Reference actual file names and test names from the input.
Do not invent information not present in the input.

Output ONLY valid JSON:
{
  "root_causes": ["<1-2 specific reasons why attempt 1 likely failed>"],
  "must_do": ["<concrete action 1>", "<concrete action 2>"],
  "must_not_do": ["<thing to avoid based on attempt 1>"],
  "validation_requirement": "<how the agent knows the fix is correct>",
  "next_attempt_prompt": "<2-4 sentence prompt to inject before attempt 2, referencing specific files/tests>"
}

Keep next_attempt_prompt under 300 characters. Be direct, not generic."""


@dataclass
class RetryPlan:
    root_causes: list[str]
    must_do: list[str]
    must_not_do: list[str]
    validation_requirement: str
    next_attempt_prompt: str
    raw_response: str = ""


def _parse_retry_plan(raw: str) -> RetryPlan:
    """Parse RetryPlan from LLM JSON output. Returns fallback on parse error."""
    json_match = re.search(r'\{.*\}', raw, re.DOTALL)
    if not json_match:
        return RetryPlan(
            root_causes=["[PARSE_ERROR] Could not parse retry plan"],
            must_do=[], must_not_do=[],
            validation_requirement="Run the required tests",
            next_attempt_prompt="Previous attempt failed. Review the failing tests carefully and fix the root cause.",
            raw_response=raw,
        )
    try:
        data = json.loads(json_match.group(0))
    except json.JSONDecodeError:
        return RetryPlan(
            root_causes=["[PARSE_ERROR] Invalid JSON"],
            must_do=[], must_not_do=[],
            validation_requirement="Run the required tests",
            next_attempt_prompt="Previous attempt failed. Review the failing tests carefully and fix the root cause.",
            raw_response=raw,
        )
    return RetryPlan(
        root_causes=data.get("root_causes", []),
        must_do=data.get("must_do", []),
        must_not_do=data.get("must_not_do", []),
        validation_requirement=data.get("validation_requirement", ""),
        next_attempt_prompt=data.get("next_attempt_prompt", ""),
        raw_response=raw,
    )


def build_retry_plan(
    problem_statement: str,
    patch_text: str,
    jingu_body: dict,
    fail_to_pass_tests: list[str],
    gate_admitted: bool,
    gate_reason_codes: list[str],
    instance_id: str = "",
    patch_fp: Optional[dict] = None,
    prev_patch_fp: Optional[dict] = None,
    exec_feedback: str = "",
) -> RetryPlan:
    """
    Phase 2A (deterministic): classify failure type → apply intervention mapping.
    Phase 2B (LLM): refine with instance-specific context.

    Args:
        problem_statement:  the bug report
        patch_text:         the patch from attempt 1
        jingu_body:         structured telemetry (exit_status, files_written, test_results)
        fail_to_pass_tests: tests that must pass
        gate_admitted:      whether gate admitted the patch
        gate_reason_codes:  gate reason codes (for rejected patches)
        instance_id:        for logging
        patch_fp:           current attempt fingerprint (files, hunks, lines)
        prev_patch_fp:      previous attempt fingerprint (for wrong_direction detection)
        exec_feedback:      structured execution feedback string (Phase 2A output)
    """
    # Phase 2A: deterministic failure classification + intervention mapping
    fp = patch_fp or {}
    failure_type = classify_failure(jingu_body, fp, prev_patch_fp, exec_feedback)
    intervention = _INTERVENTIONS.get(failure_type, _INTERVENTIONS["unknown"])
    try:
        import litellm
    except ImportError:
        # litellm unavailable: return deterministic intervention only
        prompt_hint = intervention["hint_prefix"] + exec_feedback[:300]
        return RetryPlan(
            root_causes=[f"[SKIP] litellm unavailable — failure_type={failure_type}"],
            must_do=intervention["must_do"],
            must_not_do=intervention["must_not_do"],
            validation_requirement="Run the required FAIL_TO_PASS tests",
            next_attempt_prompt=prompt_hint[:400],
        )

    # Build observable signal summary
    exit_status = jingu_body.get("exit_status", "unknown")
    files_written = jingu_body.get("files_written", [])
    test_results = jingu_body.get("test_results", {})
    tests_ran = test_results.get("ran_tests", False)
    test_passed = test_results.get("last_passed")
    patch_summary = jingu_body.get("patch_summary", {})

    gate_status = "ADMITTED (gate passed)" if gate_admitted else f"REJECTED ({', '.join(gate_reason_codes)})"

    tests_str = "\n".join(f"  - {t}" for t in fail_to_pass_tests[:8])
    files_str = "\n".join(f"  - {f}" for f in files_written) if files_written else "  (none recorded)"
    test_signal = f"Tests ran: {'YES — FAILED' if tests_ran and not test_passed else 'YES — PASSED' if tests_ran else 'NO'}"

    patch_preview = patch_text[:1200] if patch_text else "(no patch)"

    # Phase 2A classification already done above — include in prompt for LLM context
    must_not_str = "\n".join(f"  - {x}" for x in intervention["must_not_do"]) or "  (none)"
    must_do_str  = "\n".join(f"  - {x}" for x in intervention["must_do"])     or "  (none)"

    prompt = f"""## PROBLEM STATEMENT
{problem_statement[:800]}

## TESTS THAT MUST PASS
{tests_str}

## FAILURE CLASSIFICATION (deterministic)
Failure type: {failure_type}
Intervention must_not_do:
{must_not_str}
Intervention must_do:
{must_do_str}

## EXECUTION FEEDBACK
{exec_feedback[:600] if exec_feedback else "(none)"}

## ATTEMPT 1 OBSERVABLE SIGNAL
- Exit status: {exit_status}
- Gate result: {gate_status}
- Files changed:
{files_str}
- Patch size: {patch_summary.get('hunks', 0)} hunks, +{patch_summary.get('lines_added', 0)}/-{patch_summary.get('lines_removed', 0)} lines
- {test_signal}

## ATTEMPT 1 PATCH (first 1200 chars)
```diff
{patch_preview}
```

## YOUR TASK
The failure type and interventions above are deterministic facts. Use them.
Refine with instance-specific context. Output JSON only."""

    try:
        response = litellm.completion(
            model=RETRY_MODEL,
            messages=[
                {"role": "system", "content": RETRY_SYSTEM_PROMPT},
                {"role": "user",   "content": prompt},
            ],
            max_tokens=RETRY_MAX_TOKENS,
            temperature=RETRY_TEMPERATURE,
            drop_params=True,
        )
        raw = response.choices[0].message.content or ""
    except Exception as e:
        # LLM failed: fall back to deterministic intervention only
        prompt_hint = intervention["hint_prefix"] + exec_feedback[:300]
        return RetryPlan(
            root_causes=[f"[CONTROLLER_ERROR] {str(e)[:100]} — failure_type={failure_type}"],
            must_do=intervention["must_do"],
            must_not_do=intervention["must_not_do"],
            validation_requirement="Run the required FAIL_TO_PASS tests",
            next_attempt_prompt=prompt_hint[:400],
        )

    # Phase 2B: LLM output parsed; merge with deterministic intervention
    # Deterministic must_not_do/must_do are prepended (higher priority than LLM additions)
    llm_plan = _parse_retry_plan(raw)
    merged_must_not = intervention["must_not_do"] + [
        x for x in llm_plan.must_not_do if x not in intervention["must_not_do"]
    ]
    merged_must_do = intervention["must_do"] + [
        x for x in llm_plan.must_do if x not in intervention["must_do"]
    ]
    # next_attempt_prompt: deterministic prefix + LLM refinement
    hint_prefix = intervention["hint_prefix"]
    llm_hint = llm_plan.next_attempt_prompt.strip()
    merged_hint = (hint_prefix + llm_hint)[:500]

    return RetryPlan(
        root_causes=llm_plan.root_causes,
        must_do=merged_must_do,
        must_not_do=merged_must_not,
        validation_requirement=llm_plan.validation_requirement,
        next_attempt_prompt=merged_hint,
        raw_response=llm_plan.raw_response,
    )


if __name__ == "__main__":
    # Smoke test
    plan = build_retry_plan(
        problem_statement="Merging 3 or more media objects throws unnecessary MediaOrderConflictWarnings",
        patch_text="diff --git a/django/forms/widgets.py b/django/forms/widgets.py\n+++ b/django/forms/widgets.py\n@@ -140,6 +140,8 @@\n+    # added logging\n+    print('merging')\n",
        jingu_body={
            "exit_status": "Submitted",
            "files_written": ["django/forms/widgets.py"],
            "test_results": {"ran_tests": True, "last_passed": False, "excerpt": "FAILED (failures=16)"},
            "patch_summary": {"hunks": 1, "lines_added": 2, "lines_removed": 0},
        },
        fail_to_pass_tests=["test_merge (forms_tests.tests.test_media.FormsMediaTestCase)"],
        gate_admitted=True,
        gate_reason_codes=[],
        instance_id="django__django-11019",
    )
    print(f"root_causes: {plan.root_causes}")
    print(f"must_do: {plan.must_do}")
    print(f"must_not_do: {plan.must_not_do}")
    print(f"next_attempt_prompt: {plan.next_attempt_prompt}")
