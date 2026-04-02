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
) -> RetryPlan:
    """
    Call LLM to diagnose attempt 1 and produce a RetryPlan for attempt 2.

    Args:
        problem_statement:  the bug report
        patch_text:         the patch from attempt 1
        jingu_body:         structured telemetry (exit_status, files_written, test_results)
        fail_to_pass_tests: tests that must pass
        gate_admitted:      whether gate admitted the patch
        gate_reason_codes:  gate reason codes (for rejected patches)
        instance_id:        for logging
    """
    try:
        import litellm
    except ImportError:
        return RetryPlan(
            root_causes=["[SKIP] litellm not available"],
            must_do=[], must_not_do=[],
            validation_requirement="Run the required tests",
            next_attempt_prompt="Previous attempt did not fully solve the problem. Re-read the failing tests and fix the root cause.",
        )

    # Build observable signal summary
    exit_status = jingu_body.get("exit_status", "unknown")
    files_written = jingu_body.get("files_written", [])
    test_results = jingu_body.get("test_results", {})
    tests_ran = test_results.get("ran_tests", False)
    test_passed = test_results.get("last_passed")
    test_excerpt = test_results.get("excerpt", "")
    patch_summary = jingu_body.get("patch_summary", {})

    gate_status = "ADMITTED (gate passed)" if gate_admitted else f"REJECTED ({', '.join(gate_reason_codes)})"

    tests_str = "\n".join(f"  - {t}" for t in fail_to_pass_tests[:8])
    files_str = "\n".join(f"  - {f}" for f in files_written) if files_written else "  (none recorded)"

    test_signal = ""
    if tests_ran:
        result_str = "PASSED" if test_passed else "FAILED"
        test_signal = f"Tests ran: YES — last result: {result_str}"
        if test_excerpt:
            # Trim to most useful part
            test_signal += f"\nTest output excerpt:\n{test_excerpt[:400]}"
    else:
        test_signal = "Tests ran: NO (agent did not run tests before submitting)"

    patch_preview = patch_text[:1200] if patch_text else "(no patch)"

    prompt = f"""## PROBLEM STATEMENT
{problem_statement[:800]}

## TESTS THAT MUST PASS
{tests_str}

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
Diagnose what went wrong in attempt 1 and produce a RetryPlan for attempt 2.
Output JSON only."""

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
        return RetryPlan(
            root_causes=[f"[CONTROLLER_ERROR] {str(e)[:100]}"],
            must_do=[], must_not_do=[],
            validation_requirement="Run the required tests",
            next_attempt_prompt="Previous attempt did not fully solve the problem. Re-read the failing tests and fix the root cause.",
        )

    return _parse_retry_plan(raw)


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
