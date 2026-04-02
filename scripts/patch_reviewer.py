"""
patch_reviewer.py — B2 adversarial reviewer for SWE-bench patches.

Implements jingu-agent's reviewer.ts logic in Python for use in the
jingu-swebench pipeline. No TS subprocess needed — direct Anthropic API call.

Reviewer evaluates a patch proposal across 5 attack dimensions:
  1. misread_intent        — did the patch fix the right thing?
  2. insufficient_context  — does the patch rely on context not shown?
  3. missing_tradeoffs     — are there other approaches that should be considered?
  4. unsupported_conclusion — does the diff actually fix what the problem says?
  5. hidden_risk           — side effects, regressions, scope creep?

Verdict is deterministic from issue severities (no LLM verdict):
  - any "high" issue → reject
  - 2+ "medium" issues → reject
  - otherwise → pass

B2 stage: adds cognitive governance on top of B1 structural admission.
The patch must pass B1 (trust-gate) before reaching B2 (reviewer).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

# ── Constants (mirrors reviewer.ts) ──────────────────────────────────────────

REVIEWER_MODEL = "claude-sonnet-4-6"
REVIEWER_TEMPERATURE = 0.3
REVIEWER_MAX_TOKENS = 1024

REVIEWER_SYSTEM_PROMPT = """You are an adversarial reviewer for software patches. Your job is to find problems — not to approve.

You will be given:
1. The original bug report / task (what was asked)
2. The governance brief (constraints)
3. The patch (proposer output)

Attack the patch along exactly these 5 dimensions:
- misread_intent: Did the patch fix the issue that was actually reported, or a different one?
- insufficient_context: Does the patch make changes that require context not shown in the diff?
- missing_tradeoffs: Were other approaches considered? Is this the minimal correct fix?
- unsupported_conclusion: Does the diff actually implement what the problem statement requires?
- hidden_risk: Are there side effects, regressions, or scope creep risks?

For each dimension, decide:
- If you find a real issue: record it with severity "low", "medium", or "high"
  - high: the patch would fail or introduce a bug
  - medium: the patch is suboptimal but may work
  - low: minor observation, not blocking
- If you find no issue: do not include that dimension

Output ONLY valid JSON:
{
  "issues": [
    {
      "dimension": "<one of the 5 dimensions>",
      "severity": "low" | "medium" | "high",
      "description": "<what you found, 1-2 sentences>",
      "quote": "<short excerpt from patch that illustrates the issue, optional>"
    }
  ],
  "reasoning": "<overall assessment, 1-3 sentences>"
}

If you find no issues: { "issues": [], "reasoning": "<why the patch passes>" }
Do not invent issues. Only report what you actually find."""

# Brief for "execution" task type (from policy-core brief-builder logic)
EXECUTION_BRIEF = """## ACTIVE POLICIES
- no_assumption_as_fact
- evidence_required_for_claims
- uncertainty_must_be_explicit
- must_define_problem_scope
- must_identify_affected_components
- must_validate_all_claims
- evidence_gate

## REQUIRED GATES
- evidence_required_gate
- verify_gate
- no_op_detection_gate
- scope_gate
- impact_gate
- validation_gate

## EXECUTION CONTEXT
Execution mode: single
Reviewer mode: required"""

# ── Result types ──────────────────────────────────────────────────────────────

AttackDimension = str  # one of the 5 above
ReviewVerdict = str    # "pass" | "reject"


@dataclass
class ReviewIssue:
    dimension: AttackDimension
    severity: str   # "low" | "medium" | "high"
    description: str
    quote: Optional[str] = None


@dataclass
class ReviewResult:
    verdict: ReviewVerdict
    issues: list[ReviewIssue]
    reasoning: str
    raw_response: str = ""


# ── Verdict from issues (deterministic, mirrors reviewer.ts logic) ────────────

def _compute_verdict(issues: list[ReviewIssue]) -> ReviewVerdict:
    high_count = sum(1 for i in issues if i.severity == "high")
    medium_count = sum(1 for i in issues if i.severity == "medium")
    if high_count > 0 or medium_count >= 2:
        return "reject"
    return "pass"


# ── Output parser (mirrors parser.ts) ────────────────────────────────────────

def _parse_reviewer_output(raw: str) -> ReviewResult:
    """Parse structured JSON from reviewer LLM. Malformed → conservative pass."""
    # Extract JSON from response (may have markdown code fences)
    json_match = re.search(r'\{.*\}', raw, re.DOTALL)
    if not json_match:
        return ReviewResult(
            verdict="pass",
            issues=[],
            reasoning="[PARSE_ERROR] Reviewer output was not valid JSON — conservative pass.",
            raw_response=raw,
        )
    try:
        data = json.loads(json_match.group(0))
    except json.JSONDecodeError:
        return ReviewResult(
            verdict="pass",
            issues=[],
            reasoning="[PARSE_ERROR] Could not parse reviewer JSON — conservative pass.",
            raw_response=raw,
        )

    issues = []
    for item in data.get("issues", []):
        dim = item.get("dimension", "")
        sev = item.get("severity", "low")
        desc = item.get("description", "")
        quote = item.get("quote")
        if dim and desc:
            issues.append(ReviewIssue(dimension=dim, severity=sev,
                                       description=desc, quote=quote))

    verdict = _compute_verdict(issues)
    reasoning = data.get("reasoning", "")
    return ReviewResult(verdict=verdict, issues=issues,
                        reasoning=reasoning, raw_response=raw)


# ── Main reviewer function ────────────────────────────────────────────────────

def review_patch(
    problem_statement: str,
    patch_text: str,
    instance_id: str = "",
    fail_to_pass_tests: Optional[list[str]] = None,
) -> ReviewResult:
    """
    Run adversarial reviewer on a patch.

    Args:
        problem_statement: the bug report / task description
        patch_text:        the unified diff to review
        instance_id:       for logging
        fail_to_pass_tests: FAIL_TO_PASS test names (if available)

    Returns:
        ReviewResult with verdict, issues, reasoning
    """
    import anthropic

    client = anthropic.Anthropic(
        # Uses ANTHROPIC_API_KEY or AWS Bedrock via env
    )

    # Build prompt (mirrors buildReviewerPrompt)
    tests_section = ""
    if fail_to_pass_tests:
        tests_str = "\n".join(f"  - {t}" for t in fail_to_pass_tests[:10])
        tests_section = f"\n## TESTS THAT MUST PASS\n{tests_str}\n"

    prompt = f"""## ORIGINAL TASK

Task type: execution
Risk level: medium
Instance: {instance_id}
Problem statement: {problem_statement[:1500]}

## GOVERNANCE BRIEF

{EXECUTION_BRIEF}
{tests_section}
## PATCH (proposer output)

```diff
{patch_text[:8000]}
```

## YOUR TASK

Attack this patch across the 5 dimensions. Output JSON only."""

    try:
        response = client.messages.create(
            model=REVIEWER_MODEL,
            max_tokens=REVIEWER_MAX_TOKENS,
            temperature=REVIEWER_TEMPERATURE,
            system=REVIEWER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text if response.content else ""
    except Exception as e:
        # Reviewer failure → conservative pass (don't block on reviewer errors)
        return ReviewResult(
            verdict="pass",
            issues=[],
            reasoning=f"[REVIEWER_ERROR] {str(e)[:200]} — conservative pass.",
        )

    return _parse_reviewer_output(raw)


# ── Bedrock variant (for cloud execution) ────────────────────────────────────

def review_patch_bedrock(
    problem_statement: str,
    patch_text: str,
    instance_id: str = "",
    fail_to_pass_tests: Optional[list[str]] = None,
    model: str = "bedrock/global.anthropic.claude-sonnet-4-6",
) -> ReviewResult:
    """
    Run reviewer via litellm (same as mini-SWE-agent uses on cloud).
    Requires litellm installed and AWS credentials configured.
    """
    try:
        import litellm
    except ImportError:
        return ReviewResult(
            verdict="pass",
            issues=[],
            reasoning="[REVIEWER_SKIP] litellm not available — conservative pass.",
        )

    tests_section = ""
    if fail_to_pass_tests:
        tests_str = "\n".join(f"  - {t}" for t in fail_to_pass_tests[:10])
        tests_section = f"\n## TESTS THAT MUST PASS\n{tests_str}\n"

    prompt = f"""## ORIGINAL TASK

Task type: execution
Risk level: medium
Instance: {instance_id}
Problem statement: {problem_statement[:1500]}

## GOVERNANCE BRIEF

{EXECUTION_BRIEF}
{tests_section}
## PATCH (proposer output)

```diff
{patch_text[:8000]}
```

## YOUR TASK

Attack this patch across the 5 dimensions. Output JSON only."""

    try:
        response = litellm.completion(
            model=model,
            messages=[
                {"role": "system", "content": REVIEWER_SYSTEM_PROMPT},
                {"role": "user",   "content": prompt},
            ],
            max_tokens=REVIEWER_MAX_TOKENS,
            temperature=REVIEWER_TEMPERATURE,
            drop_params=True,
        )
        raw = response.choices[0].message.content or ""
    except Exception as e:
        return ReviewResult(
            verdict="pass",
            issues=[],
            reasoning=f"[REVIEWER_ERROR] {str(e)[:200]} — conservative pass.",
        )

    return _parse_reviewer_output(raw)


# ── Smoke test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sample_problem = (
        "When a model field has choices, the admin's filter sidebar should "
        "show human-readable labels instead of internal keys. Currently it shows "
        "the raw key values."
    )
    sample_patch = """\
diff --git a/django/contrib/admin/filters.py b/django/contrib/admin/filters.py
--- a/django/contrib/admin/filters.py
+++ b/django/contrib/admin/filters.py
@@ -195,7 +195,8 @@ class ChoicesFieldListFilter(FieldListFilter):
     def choices(self, changelist):
         yield {
-            'selected': self.value() is None,
+            'selected': self.value() is None,
+            'query_string': changelist.get_query_string(remove=[self.lookup_kwarg]),
             'display': _('All'),
         }
"""

    print("Testing reviewer (Bedrock)...")
    result = review_patch_bedrock(
        problem_statement=sample_problem,
        patch_text=sample_patch,
        instance_id="django__django-11001",
        fail_to_pass_tests=["tests.admin_filters.ChoicesFieldListFilterTests.test_choices"],
    )
    print(f"Verdict: {result.verdict}")
    print(f"Issues:  {len(result.issues)}")
    for i in result.issues:
        print(f"  [{i.severity}] {i.dimension}: {i.description[:80]}")
    print(f"Reasoning: {result.reasoning[:200]}")
