"""
verification_evidence.py — p201: trust hierarchy for verification signals.

VerificationEvidence + verify_evidence_hierarchy():
  Collects evidence items from multiple sources (trust=100 to trust=20)
  and routes to the correct verdict based on trust hierarchy.

Trust levels:
  100 — official_test   (controlled_verify FAIL_TO_PASS — ground truth)
   80 — repo_test       (existing repo tests not touched by patch)
   30 — agent_test      (agent-generated / agent-run test — hypothesis, not ground truth)
   20 — heuristic       (in_loop_judge structural checks — pattern matching)

Key rule:
  High-trust (>=80) PASS  → SUBMIT
  High-trust (>=80) FAIL  → HARD_FAIL (real failure, block retry)
  Only low-trust (<80)    → SOFT_FAIL if any fail (do NOT block — agent hypothesis may be wrong)
  No evidence at all      → OBSERVATION_MISSING
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


TrustLevel = Literal[100, 80, 30, 20]

Verdict = Literal[
    "SUBMIT",               # high-trust evidence passes → submit
    "SUBMIT_WITH_WARNING",  # only low-trust evidence, all pass → submit with caveat
    "SOFT_FAIL",            # low-trust evidence fails → continue, do NOT block
    "HARD_FAIL",            # high-trust evidence fails → block, retry with failure signal
    "OBSERVATION_MISSING",  # no evidence collected → cannot judge
]


@dataclass
class VerificationEvidence:
    source: Literal["official_test", "repo_test", "agent_test", "heuristic"]
    trust_level: TrustLevel
    passed: bool
    details: str  # human-readable, used in retry hints


def collect_evidence_from_jingu_body(jingu_body: dict) -> list[VerificationEvidence]:
    """
    Extract VerificationEvidence list from a jingu_body dict.

    Reads fields already present in jingu_body — no new I/O needed.
    Called after run_agent() returns, before build_execution_feedback().

    Sources consumed:
      trust=100: test_results.controlled_passed / controlled_failed (official FAIL_TO_PASS)
      trust=30:  test_results.last_passed (agent-run test, trajectory scan heuristic)
    """
    evidences: list[VerificationEvidence] = []
    tr = jingu_body.get("test_results", {})

    # trust=100: controlled_verify (official FAIL_TO_PASS harness result)
    controlled_passed = tr.get("controlled_passed")
    controlled_failed = tr.get("controlled_failed")
    if controlled_passed is not None and controlled_failed is not None:
        ok = (controlled_failed == 0) and (controlled_passed > 0 or controlled_failed == 0)
        # Simpler: passed if no failures
        ok = controlled_failed == 0
        evidences.append(VerificationEvidence(
            source="official_test",
            trust_level=100,
            passed=ok,
            details=(
                f"official FAIL_TO_PASS: {controlled_passed} passed, "
                f"{controlled_failed} failed"
            ),
        ))

    # trust=30: agent_test (last_passed from trajectory scan)
    last_passed = tr.get("last_passed")
    ran_tests = tr.get("ran_tests", False)
    if ran_tests and last_passed is not None:
        evidences.append(VerificationEvidence(
            source="agent_test",
            trust_level=30,
            passed=bool(last_passed),
            details="agent-run test: " + ("passed" if last_passed else "failed"),
        ))

    return evidences


def verify_evidence_hierarchy(
    evidences: list[VerificationEvidence],
) -> tuple[Verdict, str]:
    """
    Apply trust hierarchy to evidence list.
    Returns (verdict, reason_string_for_hint).

    Priority order:
    1. High-trust (>=80) PASSES  → SUBMIT
    2. High-trust (>=80) FAILS   → HARD_FAIL
    3. Only low-trust present:
       - All pass                → SUBMIT_WITH_WARNING
       - Any fail                → SOFT_FAIL (agent hypothesis wrong, not a ground truth failure)
    4. No evidence               → OBSERVATION_MISSING
    """
    if not evidences:
        return "OBSERVATION_MISSING", "no verification evidence collected"

    high_trust = [e for e in evidences if e.trust_level >= 80]
    low_trust = [e for e in evidences if e.trust_level < 80]

    if high_trust:
        failing_high = [e for e in high_trust if not e.passed]
        passing_high = [e for e in high_trust if e.passed]
        if passing_high and not failing_high:
            return "SUBMIT", passing_high[0].details
        if failing_high:
            return "HARD_FAIL", failing_high[0].details

    # No high-trust evidence — evaluate low-trust only
    if not low_trust:
        return "OBSERVATION_MISSING", "no verification evidence collected"

    failing_low = [e for e in low_trust if not e.passed]
    if failing_low:
        return (
            "SOFT_FAIL",
            f"low-trust test failed (agent-generated hypothesis): {failing_low[0].details}. "
            f"This is NOT a ground truth failure — official tests have not run or not failed.",
        )

    return "SUBMIT_WITH_WARNING", f"low-trust evidence only: {low_trust[0].details}"
