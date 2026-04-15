"""
GovernancePack — minimal interface for Jingu governance capability registration.

A GovernancePack bundles all 5 integration steps required for a governance
capability to be "truly enforced" in a consumer runtime:

  1. response_fields    — what fields this pack requires in response/state
  2. prompt_extensions  — what the pack injects into the agent prompt
  3. parse_failure()    — extracts structured FailureSignal from execution context
  4. recognize()        — maps FailureSignal → RecognitionResult (failure taxonomy)
  5. route()            — maps RecognitionResult → RouteDecision (routing consequence)

A pack that declares all 5 is "fully onboarded."
A pack that skips any step will log an onboarding warning at install time.

Architecture (p27 ADR):
  governance_pack.py       = interface definitions (this file)
  swebench_failure_reroute_pack.py = first pack (SWE-bench specialized)
  governance_runtime.py    = install + run (consumer entry point)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


# ── Core data structures ───────────────────────────────────────────────────────

@dataclass
class FailureSignal:
    """
    Structured failure signal extracted from execution context.
    System-generated fact — not LLM self-description.
    """
    failure_type: str  # "F2P_ALL_FAIL" | "F2P_PARTIAL" | "P2P_REGRESSION" | "unknown"
    controlled_passed: int  # official harness count (trust=100)
    controlled_failed: int  # official harness count (trust=100)
    failing_tests: list[str]  # FAIL_TO_PASS test names still failing
    raw_excerpt: str = ""    # raw pytest output excerpt (for hint building)


@dataclass
class RecognitionResult:
    """
    Failure classification produced by a pack's recognize() function.
    Maps failure_type → behavioral state (wrong_direction / insufficient_coverage / regression_risk).
    """
    state: str           # "wrong_direction" | "insufficient_coverage" | "regression_risk" | "pass"
    confidence: float    # 0.0–1.0
    next_phase: str      # "ANALYZE" | "DESIGN" | "EXECUTE"
    reason: str          # human-readable explanation (for logging)
    pack_name: str = ""  # set by governance_runtime after recognition


@dataclass
class RouteDecision:
    """
    Routing consequence produced by a pack's route() function.
    Drives the control loop: REROUTE means override retry_plan with phase-specific hint.
    """
    action: str              # "REROUTE" | "CONTINUE"
    target_phase: str = ""   # "ANALYZE" | "DESIGN" | "EXECUTE"
    hint: str = ""           # injected into retry_plan.next_attempt_prompt
    pack_name: str = ""      # set by governance_runtime


@dataclass
class ExecutionContext:
    """
    Context passed to pack functions.
    Contains all observable signals from a completed attempt.
    """
    jingu_body: dict
    fail_to_pass: list[str]   # list of FAIL_TO_PASS test IDs for this instance
    attempt: int
    instance_id: str = ""
    patch_text: str = ""

    # Convenience accessors (derived from jingu_body)
    @property
    def test_results(self) -> dict:
        return self.jingu_body.get("test_results", {})

    @property
    def controlled_passed(self) -> int:
        return self.test_results.get("controlled_passed", -1)

    @property
    def controlled_failed(self) -> int:
        return self.test_results.get("controlled_failed", -1)

    @property
    def excerpt(self) -> str:
        return self.test_results.get("excerpt", "")

    @property
    def controlled_verify_available(self) -> bool:
        return self.controlled_passed >= 0 and self.controlled_failed >= 0


# ── Pack interface ─────────────────────────────────────────────────────────────

@dataclass
class GovernancePack:
    """
    A governance capability pack — bundles all 5 integration steps.

    To register a new capability, create an instance of GovernancePack and
    call installGovernancePack(pack) in governance_runtime.py.

    Onboarding completeness:
      - All 5 fields should be non-None for a fully onboarded capability.
      - Missing fields log a warning at install time (not a hard error in v0).
      - Future: missing fields = OnboardingError at import time.
    """
    name: str

    # Step 1: response/state fields this pack requires
    required_state_fields: list[str] = field(default_factory=list)

    # Step 2: prompt extension text this pack injects
    prompt_extensions: list[str] = field(default_factory=list)

    # Step 3: parse structured failure signal from execution context
    parse_failure: Optional[Callable[[ExecutionContext], Optional[FailureSignal]]] = None

    # Step 4: recognize failure → behavioral state + next phase
    recognize: Optional[Callable[[FailureSignal], Optional[RecognitionResult]]] = None

    # Step 5: route recognition result → control decision
    route: Optional[Callable[[RecognitionResult, ExecutionContext], Optional[RouteDecision]]] = None

    def onboarding_status(self) -> dict:
        """
        Returns a dict describing which of the 5 integration steps are present.
        Used by governance_runtime at install time for completeness logging.
        """
        return {
            "response_fields": bool(self.required_state_fields),
            "prompt_extensions": bool(self.prompt_extensions),
            "parse_failure": self.parse_failure is not None,
            "recognize": self.recognize is not None,
            "route": self.route is not None,
        }

    def is_fully_onboarded(self) -> bool:
        status = self.onboarding_status()
        return all(status.values())
