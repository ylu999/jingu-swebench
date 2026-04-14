"""
prediction_error.py — P2: Decision Prediction Error Computation

Compares DECIDE phase record predictions against actual controlled_verify results.
Returns a typed PredictionError that drives retry routing.

Prediction error types:
  - prediction_correct: hypothesis confirmed, tests pass as expected
  - prediction_wrong_direction: hypothesis fundamentally wrong (0 F2P pass)
  - prediction_partial: some expected tests pass, others don't
  - prediction_no_data: DECIDE record missing or no predictions made
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class PredictionError:
    """Result of comparing DECIDE predictions against verify outcome."""
    error_type: str  # prediction_correct | prediction_wrong_direction | prediction_partial | prediction_no_data
    severity: float  # 0.0 = correct, 1.0 = completely wrong
    predicted_tests: list[str] = field(default_factory=list)
    actual_f2p_passed: int = 0
    actual_f2p_failed: int = 0
    hypothesis: str = ""
    feedback: str = ""  # human-readable feedback for retry
    repair_target: str = ""  # DECIDE | ANALYZE | EXECUTE

    def to_dict(self) -> dict:
        return {
            "error_type": self.error_type,
            "severity": self.severity,
            "predicted_tests": self.predicted_tests,
            "actual_f2p_passed": self.actual_f2p_passed,
            "actual_f2p_failed": self.actual_f2p_failed,
            "hypothesis": self.hypothesis[:300],
            "feedback": self.feedback,
            "repair_target": self.repair_target,
        }


def compute_prediction_error(
    phase_records: list,
    cv_result: dict,
) -> PredictionError:
    """Compare DECIDE phase record predictions against controlled_verify result.

    Args:
        phase_records: list of phase records from the attempt (monitor state)
        cv_result: flattened controlled_verify result dict

    Returns:
        PredictionError with typed error and routing suggestion.
    """
    # Find the DECIDE phase record
    decide_record = None
    for pr in reversed(phase_records):
        phase = ""
        if hasattr(pr, "phase"):
            phase = str(pr.phase).upper()
        elif isinstance(pr, dict):
            phase = str(pr.get("phase", "")).upper()
        if phase == "DECIDE":
            decide_record = pr
            break

    if decide_record is None:
        return PredictionError(
            error_type="prediction_no_data",
            severity=0.5,
            feedback="No DECIDE phase record found. Cannot compute prediction error.",
            repair_target="DECIDE",
        )

    # Extract predictions from DECIDE record
    if hasattr(decide_record, "as_dict"):
        dr = decide_record.as_dict()
    elif isinstance(decide_record, dict):
        dr = decide_record
    else:
        dr = vars(decide_record) if hasattr(decide_record, "__dict__") else {}

    hypothesis = dr.get("testable_hypothesis", "") or ""
    predicted_tests = dr.get("expected_tests_to_pass", []) or []
    if isinstance(predicted_tests, str):
        predicted_tests = [t.strip() for t in predicted_tests.split(",") if t.strip()]

    if not hypothesis and not predicted_tests:
        return PredictionError(
            error_type="prediction_no_data",
            severity=0.5,
            hypothesis=hypothesis,
            predicted_tests=predicted_tests,
            feedback="DECIDE record exists but contains no testable predictions.",
            repair_target="DECIDE",
        )

    # Extract actual results
    f2p_passed = cv_result.get("f2p_passed", 0) or 0
    f2p_failed = cv_result.get("f2p_failed", 0) or 0
    f2p_total = f2p_passed + f2p_failed

    # Classify prediction error
    if f2p_total == 0:
        # No F2P tests ran — can't evaluate prediction
        return PredictionError(
            error_type="prediction_no_data",
            severity=0.5,
            hypothesis=hypothesis,
            predicted_tests=predicted_tests,
            actual_f2p_passed=f2p_passed,
            actual_f2p_failed=f2p_failed,
            feedback="F2P tests did not run. Cannot evaluate prediction.",
            repair_target="EXECUTE",
        )

    if f2p_failed == 0 and f2p_passed > 0:
        # All F2P pass — prediction correct
        return PredictionError(
            error_type="prediction_correct",
            severity=0.0,
            hypothesis=hypothesis,
            predicted_tests=predicted_tests,
            actual_f2p_passed=f2p_passed,
            actual_f2p_failed=f2p_failed,
            feedback="Prediction confirmed. All F2P tests pass.",
            repair_target="",
        )

    if f2p_passed == 0:
        # Zero F2P pass — prediction completely wrong
        return PredictionError(
            error_type="prediction_wrong_direction",
            severity=1.0,
            hypothesis=hypothesis,
            predicted_tests=predicted_tests,
            actual_f2p_passed=f2p_passed,
            actual_f2p_failed=f2p_failed,
            feedback=(
                f"PREDICTION WRONG: Your hypothesis was \"{hypothesis[:200]}\" "
                f"but 0/{f2p_total} required tests pass. "
                f"Your root cause analysis is likely incorrect. "
                f"Return to ANALYZE with fresh evidence."
            ),
            repair_target="ANALYZE",
        )

    # Partial — some pass, some fail
    pass_rate = f2p_passed / f2p_total
    if pass_rate >= 0.5:
        # More than half pass — direction right, execution incomplete
        return PredictionError(
            error_type="prediction_partial",
            severity=round(1.0 - pass_rate, 2),
            hypothesis=hypothesis,
            predicted_tests=predicted_tests,
            actual_f2p_passed=f2p_passed,
            actual_f2p_failed=f2p_failed,
            feedback=(
                f"PREDICTION PARTIAL: {f2p_passed}/{f2p_total} required tests pass. "
                f"Your direction is correct but coverage is insufficient. "
                f"Identify which branches or edge cases are uncovered."
            ),
            repair_target="EXECUTE",
        )
    else:
        # Less than half pass — direction questionable
        return PredictionError(
            error_type="prediction_wrong_direction",
            severity=round(1.0 - pass_rate, 2),
            hypothesis=hypothesis,
            predicted_tests=predicted_tests,
            actual_f2p_passed=f2p_passed,
            actual_f2p_failed=f2p_failed,
            feedback=(
                f"PREDICTION MOSTLY WRONG: Only {f2p_passed}/{f2p_total} required tests pass. "
                f"Your hypothesis \"{hypothesis[:150]}\" may be partially correct "
                f"but the root cause is likely deeper. Re-examine your analysis."
            ),
            repair_target="DECIDE",
        )
