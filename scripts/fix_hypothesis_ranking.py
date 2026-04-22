"""
fix_hypothesis_ranking.py — Cross-Abstraction Fix Hypothesis Ranking v0.2

Before writing a patch in EXECUTE phase, force the agent to produce two
fix hypotheses at DIFFERENT ABSTRACTION LEVELS within the same target file:
  - Hypothesis 1: local/symptom-level fix (tweak condition, regex, branch)
  - Hypothesis 2: structural/representation-level fix (change extraction
    strategy, decomposition, data flow, or control flow)

v0.1 showed agent complies with format but both hypotheses stay at the same
abstraction level (e.g., two regex tweaks). v0.2 explicitly constrains
hypothesis 2 to be structural.

Integration: injected as a user message when agent first enters EXECUTE phase.
Feature gate: JINGU_FIX_HYPOTHESIS=1
"""
import os

FIX_HYPOTHESIS_ENABLED = os.environ.get("JINGU_FIX_HYPOTHESIS", "0") == "1"


# ── Prompt block ─────────────────────────────────────────────────────────────

_FIX_HYPOTHESIS_PROMPT = """\
[FIX HYPOTHESIS RANKING — MANDATORY before writing any patch]

You have identified the target file(s). Before writing the patch, you MUST
produce TWO fix hypotheses at DIFFERENT ABSTRACTION LEVELS, then compare
and select one.

HYPOTHESIS 1 — LOCAL / SYMPTOM-LEVEL FIX:
  A minimal, targeted change: tweak a condition, adjust a regex, flip a
  branch, add a guard clause. Operates within the existing code structure.

HYPOTHESIS 2 — STRUCTURAL / REPRESENTATION-LEVEL FIX:
  A deeper change: restructure how data is extracted, change the
  decomposition of a value, alter the control flow, modify the
  representation or data model. This hypothesis must NOT merely tweak the
  same condition/regex/branch as Hypothesis 1.

  Examples of structural changes:
  - Extract a component (e.g., sign, prefix) as a separate capture group
    instead of embedding it in multiple patterns
  - Change the parsing strategy (e.g., from regex to split-and-parse)
  - Restructure the function to handle a category of inputs differently
  - Change what data is passed between functions

The second hypothesis MUST operate at a different abstraction level from
the first. If both hypotheses only tweak the same regex/condition/branch,
you have NOT satisfied this requirement — step back and think about how
the code's structure or representation could be changed instead.

For each hypothesis, specify:
  - EXACT function/method/code region to modify
  - WHAT behavior changes (current → new)
  - WHY the failing test(s) should pass after this change

Then compare and select:

FIX_HYPOTHESIS_1 (local/symptom):
  region: <function/method name and line range>
  mechanism: <what you change and why>
  test_prediction: <why failing test passes>

FIX_HYPOTHESIS_2 (structural/representation):
  region: <function/method or data flow being restructured>
  mechanism: <what structural change you make and why>
  test_prediction: <why failing test passes>

COMPARISON:
  - Which addresses the root cause more completely?
  - Which has fewer side effects / regressions?
  - Which is more consistent with the codebase's patterns?

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
