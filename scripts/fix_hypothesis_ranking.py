"""
fix_hypothesis_ranking.py — Same-File Fix Hypothesis Ranking v0.1

Before writing a patch in EXECUTE phase, force the agent to produce two
distinct fix hypotheses within the same target file, compare them on
behavioral mechanism grounds, and select one.

Integration: injected as a user message when agent first enters EXECUTE phase.
Feature gate: JINGU_FIX_HYPOTHESIS=1
"""
import os

FIX_HYPOTHESIS_ENABLED = os.environ.get("JINGU_FIX_HYPOTHESIS", "0") == "1"


# ── Prompt block ─────────────────────────────────────────────────────────────

_FIX_HYPOTHESIS_PROMPT = """\
[FIX HYPOTHESIS RANKING — MANDATORY before writing any patch]

You have identified the target file(s). Before writing the patch, you MUST:

1. Produce TWO different fix hypotheses within the SAME target file.
   Each hypothesis must specify:
   - EXACT function/method/code region to modify
   - WHAT behavior changes (current → new)
   - WHY the failing test(s) should pass after this change

2. The two hypotheses MUST differ in at least one of:
   - Different function/method being modified
   - Different branch/condition being changed
   - Different mechanism (e.g., regex fix vs structural refactor)
   Simply rewording the same fix does NOT count as a different hypothesis.

3. Compare the two hypotheses:
   - Which one more directly addresses the test failure?
   - Which one has fewer side effects?
   - Which one is more consistent with the codebase's existing patterns?

4. Select ONE hypothesis and implement it.

Format your response as:

FIX_HYPOTHESIS_1:
  region: <function/method name and line range>
  mechanism: <what you change and why>
  test_prediction: <why failing test passes>

FIX_HYPOTHESIS_2:
  region: <different function/method or different mechanism>
  mechanism: <what you change and why>
  test_prediction: <why failing test passes>

COMPARISON:
  <1-3 sentences comparing the two>

SELECTED: <1 or 2>
REASON: <why this one is better>

Then proceed to implement the selected hypothesis.
"""


def should_inject(phase: str, step_n: int, already_injected: bool) -> bool:
    """Check if fix hypothesis prompt should be injected."""
    if not FIX_HYPOTHESIS_ENABLED:
        return False
    if phase.upper() != "EXECUTE":
        return False
    if already_injected:
        return False
    return True


def get_prompt_block() -> str:
    """Return the fix hypothesis ranking prompt block."""
    return _FIX_HYPOTHESIS_PROMPT


# ── Telemetry ────────────────────────────────────────────────────────────────

def build_telemetry(injected: bool) -> dict:
    """Build telemetry dict for run_report."""
    return {
        "fix_hypothesis_enabled": FIX_HYPOTHESIS_ENABLED,
        "fix_hypothesis_injected": injected,
    }
