"""
fix_hypothesis_ranking.py — Structural-First Fix Hypothesis v0.3

Before writing a patch in EXECUTE phase, force the agent to produce two
fix hypotheses at DIFFERENT ABSTRACTION LEVELS, then IMPLEMENT THE
STRUCTURAL ONE FIRST (not select by reasoning).

Evolution:
  v0.1: agent produces 2 hypotheses, both at same abstraction level (generation fail)
  v0.2: agent generates gold-like structural fix BUT selects local due to
        minimal_change bias and PR description anchoring (selection fail)
  v0.3: remove selection step — mandate structural-first implementation

Integration: injected as a user message when agent first enters EXECUTE phase.
Feature gate: JINGU_FIX_HYPOTHESIS=1
"""
import os

FIX_HYPOTHESIS_ENABLED = os.environ.get("JINGU_FIX_HYPOTHESIS", "0") == "1"


# ── Prompt block ─────────────────────────────────────────────────────────────

_FIX_HYPOTHESIS_PROMPT = """\
[STRUCTURAL-FIRST FIX — MANDATORY before writing any patch]

You have identified the target file(s). Before writing the patch, you MUST
produce TWO fix hypotheses at DIFFERENT ABSTRACTION LEVELS, then implement
the STRUCTURAL one first.

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

IMPORTANT — STRUCTURAL FIRST, NOT MINIMAL FIRST:
  Do NOT select based on minimality alone. Local/symptom-level fixes
  (regex tweaks, condition adjustments) often pass the failing tests but
  cause regressions elsewhere because they patch the symptom, not the
  root cause.

  You MUST implement the STRUCTURAL hypothesis (Hypothesis 2) first.
  Only fall back to the local hypothesis if the structural fix cannot
  be made to work.

For each hypothesis, specify:
  - EXACT function/method/code region to modify
  - WHAT behavior changes (current → new)
  - WHY the failing test(s) should pass after this change

FIX_HYPOTHESIS_1 (local/symptom):
  region: <function/method name and line range>
  mechanism: <what you change and why>
  test_prediction: <why failing test passes>

FIX_HYPOTHESIS_2 (structural/representation):
  region: <function/method or data flow being restructured>
  mechanism: <what structural change you make and why>
  test_prediction: <why failing test passes>

Now implement Hypothesis 2 (the structural fix).
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
