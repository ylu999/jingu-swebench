"""Strategy Prompts for Failure Routing Engine (p216 — Wave 3, w3-04).

Maps strategy names to targeted repair instructions.
Strategies are selected by the failure routing engine (failure_routing.py)
based on the (phase, principal) failure pattern.

Each prompt provides specific, actionable guidance for the agent
to address the identified failure pattern. These replace generic
"try again" hints with data-informed repair instructions.
"""

from __future__ import annotations


# ── Strategy Prompts ──────────────────────────────────────────────────────
#
# Each strategy maps to a prompt that tells the agent what to do
# to address the specific failure pattern identified by the router.

STRATEGY_PROMPTS: dict[str, str] = {
    "complete_causal_chain": (
        "[ROUTING: COMPLETE CAUSAL CHAIN]\n"
        "Your analysis lacks a complete causal chain from symptom to root cause.\n"
        "Requirements:\n"
        "1. Trace the bug symptom to the exact code path that produces it\n"
        "2. Identify the specific function/method where behavior diverges from expectation\n"
        "3. Link your root cause to the failing test — explain WHY the test fails\n"
        "4. Do NOT propose a fix yet — establish the causal chain first"
    ),

    "gather_code_evidence": (
        "[ROUTING: GATHER CODE EVIDENCE]\n"
        "Your analysis or design lacks grounding in actual code.\n"
        "Requirements:\n"
        "1. Read the relevant source files — do not reason from memory\n"
        "2. Quote specific lines that demonstrate the problem\n"
        "3. Reference actual function signatures, class hierarchies, and call paths\n"
        "4. Link every claim to a file path and line number"
    ),

    "rethink_root_cause": (
        "[ROUTING: RETHINK ROOT CAUSE]\n"
        "Your previous root cause analysis was incorrect — the fix did not work.\n"
        "Requirements:\n"
        "1. Re-read the failing test carefully to understand expected behavior\n"
        "2. Consider a completely different hypothesis for the root cause\n"
        "3. Trace the test input through the code to find where behavior diverges\n"
        "4. Do NOT repeat your previous approach"
    ),

    "add_alternative": (
        "[ROUTING: ADD ALTERNATIVE HYPOTHESIS]\n"
        "Your analysis considered only one possible root cause.\n"
        "Requirements:\n"
        "1. Generate at least 2 distinct hypotheses for the root cause\n"
        "2. For each hypothesis, identify what evidence would confirm or refute it\n"
        "3. Test each hypothesis against the failing test behavior\n"
        "4. Select the hypothesis with the strongest evidence"
    ),

    "realign_with_analysis": (
        "[ROUTING: REALIGN WITH ANALYSIS]\n"
        "Your code change does not align with the analysis phase conclusions.\n"
        "Requirements:\n"
        "1. Re-read your analysis — what was the identified root cause?\n"
        "2. Verify your code change addresses that specific root cause\n"
        "3. If the root cause was wrong, go back to ANALYZE first\n"
        "4. Make the minimal change that addresses the root cause directly"
    ),

    "reidentify_scope": (
        "[ROUTING: REIDENTIFY CHANGE SCOPE]\n"
        "Your change is too broad — it touches code outside the problem scope.\n"
        "Requirements:\n"
        "1. Identify the minimal set of files/functions to change\n"
        "2. Remove any changes that are not directly related to the bug fix\n"
        "3. Verify that your change preserves existing behavior for all other tests\n"
        "4. If your change breaks passing tests, narrow the scope further"
    ),

    "fix_execution_errors": (
        "[ROUTING: FIX EXECUTION ERRORS]\n"
        "Your patch has execution-level issues (syntax error, import error, apply failure).\n"
        "Requirements:\n"
        "1. Fix only the mechanical issue — do NOT change your solution direction\n"
        "2. Ensure the patch applies cleanly (correct line numbers, no conflicts)\n"
        "3. Verify the code compiles and imports work\n"
        "4. Run the tests after fixing to confirm the approach works"
    ),

    "check_constraints": (
        "[ROUTING: CHECK CONSTRAINTS]\n"
        "Your solution violates one or more constraints.\n"
        "Requirements:\n"
        "1. Review all constraints mentioned in the issue description\n"
        "2. Check backward compatibility — does your change break the existing API?\n"
        "3. Verify edge cases that the constraint is designed to handle\n"
        "4. Ensure your solution satisfies ALL constraints, not just the primary one"
    ),

    "compare_alternatives": (
        "[ROUTING: COMPARE DESIGN ALTERNATIVES]\n"
        "Your design choice needs justification through comparison.\n"
        "Requirements:\n"
        "1. Identify at least 2 possible approaches to fix the issue\n"
        "2. Evaluate each approach against: correctness, scope, backward compatibility\n"
        "3. Choose the approach with the smallest footprint that fixes the issue\n"
        "4. Document why you chose this approach over alternatives"
    ),

    "verify_test_coverage": (
        "[ROUTING: VERIFY TEST COVERAGE]\n"
        "Your fix may be correct, but verification is incomplete.\n"
        "Requirements:\n"
        "1. Run all FAIL_TO_PASS tests explicitly and check they pass\n"
        "2. Check for PASS_TO_PASS regressions — ensure no existing tests broke\n"
        "3. If tests pass but the instance is not resolved, check for edge cases\n"
        "4. Narrow your change to avoid side effects on other test modules"
    ),

    "check_regressions": (
        "[ROUTING: CHECK FOR REGRESSIONS]\n"
        "Your fix introduced regressions — existing tests now fail.\n"
        "Requirements:\n"
        "1. Identify which tests regressed and why\n"
        "2. Revert or narrow your change to avoid the regression\n"
        "3. Find an approach that fixes the bug WITHOUT breaking existing behavior\n"
        "4. Run the full test suite after each change to catch regressions early"
    ),

    "realign_phase_ontology": (
        "[ROUTING: REALIGN PHASE ONTOLOGY]\n"
        "Your phase declaration does not match your actual work.\n"
        "Requirements:\n"
        "1. Declare the correct phase for what you are actually doing\n"
        "2. If analyzing: declare ANALYZE with analysis principals\n"
        "3. If coding: declare EXECUTE with execution principals\n"
        "4. Do not declare a phase just because it is expected — declare truthfully"
    ),

    "check_all_call_sites": (
        "[ROUTING: CHECK ALL CALL SITES]\n"
        "Your change modifies a function/method but you did not verify all callers.\n"
        "Requirements:\n"
        "1. Run `grep -rn 'function_name'` to find ALL call sites in the codebase\n"
        "2. For each caller, verify it still works with your change\n"
        "3. If you changed a signature, decorator, or return type, update ALL callers\n"
        "4. Pay special attention to cached references (.cache_clear(), lru_cache, etc.)"
    ),

    "remove_unnecessary_compat": (
        "[ROUTING: REMOVE UNNECESSARY BACKWARD COMPAT]\n"
        "Your patch adds backward-compatibility code that the issue does NOT require.\n"
        "Requirements:\n"
        "1. Re-read the issue — does it ask for backward compatibility?\n"
        "2. Remove any .replace() fallbacks, try/except compat wrappers, or deprecation shims\n"
        "3. Implement ONLY what the spec/issue asks for — nothing more\n"
        "4. Extra compat code often causes the official eval tests to fail"
    ),

    "acknowledge_gaps": (
        "[ROUTING: ACKNOWLEDGE UNCERTAINTY]\n"
        "Your analysis presents conclusions with insufficient evidence.\n"
        "Requirements:\n"
        "1. Identify which parts of your analysis are certain vs uncertain\n"
        "2. For uncertain parts, state what additional evidence would confirm them\n"
        "3. Do not present hypotheses as facts\n"
        "4. Prioritize investigating the most uncertain assumptions first"
    ),

    "submit_phase_record": (
        "[ROUTING: SUBMIT PHASE RECORD]\n"
        "You must call submit_phase_record before the phase can advance.\n"
        "Requirements:\n"
        "1. Summarize your findings for the current phase\n"
        "2. Call submit_phase_record with the structured JSON output\n"
        "3. Include all required fields for this phase\n"
        "4. Do NOT skip this step — the system cannot advance without it"
    ),

    "fix_cognition_errors": (
        "[ROUTING: FIX COGNITION ERRORS]\n"
        "Your phase record failed schema validation.\n"
        "Requirements:\n"
        "1. Check the error messages above for specific field violations\n"
        "2. Fix the identified fields in your phase record\n"
        "3. Resubmit with corrected values\n"
        "4. Ensure all required fields are present and non-empty"
    ),
}


def get_strategy_prompt(strategy: str) -> str:
    """Get the repair prompt for a routing strategy.

    Args:
        strategy: strategy name from failure routing engine

    Returns:
        Non-empty strategy prompt string.
        Returns a generic prompt if strategy is not found.
    """
    prompt = STRATEGY_PROMPTS.get(strategy)
    if prompt:
        return prompt

    # Generic fallback for unknown strategies
    return (
        f"[ROUTING: {strategy.upper().replace('_', ' ')}]\n"
        "Review your previous approach and try a different strategy.\n"
        "Focus on addressing the specific failure that was identified."
    )
