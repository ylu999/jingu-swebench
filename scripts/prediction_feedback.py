"""Prediction Feedback — compute error between DECIDE predictions and VERIFY results.

Decision Quality Upgrade v1: every DECIDE carries predictions (tests, files, hypothesis).
After VERIFY, compute how accurate the predictions were. Feed error back into retry.

Three metrics:
  - pass_hit: fraction of predicted tests that actually passed
  - pass_miss: fraction of predicted tests that actually failed
  - file_accuracy: fraction of predicted files that were actually changed
"""


def compute_prediction_error(
    decide_record: dict,
    verify_result: dict,
    actual_files_changed: list[str] | None = None,
) -> dict:
    """Compute prediction error between DECIDE predictions and VERIFY results.

    Args:
        decide_record: PhaseRecord dict from DECIDE phase (from jingu_body["phase_records"]).
            Expected keys: expected_tests_to_pass, expected_files_to_change, testable_hypothesis, risk_level
        verify_result: controlled_verify result dict.
            Expected keys: f2p_passed, f2p_failed, p2p_passed, p2p_failed, eval_resolved
        actual_files_changed: list of file paths from patch_fingerprint["files"]

    Returns:
        dict with:
            pass_hit: float (0-1) — fraction of predicted tests that passed
            pass_miss: float (0-1) — fraction of predicted tests that failed
            file_accuracy: float (0-1) — fraction of predicted files actually changed
            regression: bool — whether P2P regression occurred
            score: float — composite prediction quality score (-1 to 1)
            has_predictions: bool — whether DECIDE had prediction fields
            error_type: str — "accurate" | "overconfident" | "wrong_direction" | "no_predictions"
    """
    pred_tests = decide_record.get("expected_tests_to_pass", [])
    pred_files = decide_record.get("expected_files_to_change", [])
    hypothesis = decide_record.get("testable_hypothesis", "")
    risk = decide_record.get("risk_level", "")

    # No predictions = legacy DECIDE record
    if not pred_tests and not hypothesis:
        return {
            "pass_hit": 0.0,
            "pass_miss": 0.0,
            "file_accuracy": 0.0,
            "regression": False,
            "score": 0.0,
            "has_predictions": False,
            "error_type": "no_predictions",
        }

    # F2P results
    f2p_passed = verify_result.get("f2p_passed") or 0
    f2p_failed = verify_result.get("f2p_failed") or 0
    f2p_total = f2p_passed + f2p_failed

    # Regression
    p2p_failed = verify_result.get("p2p_failed") or 0
    regression = p2p_failed > 0

    # Pass hit/miss — use f2p ratio as proxy
    # (We don't have individual test name matching from controlled_verify,
    #  so we use the ratio of passed/total as prediction accuracy proxy)
    if f2p_total > 0:
        pass_hit = f2p_passed / f2p_total
        pass_miss = f2p_failed / f2p_total
    elif len(pred_tests) > 0:
        # No verify data but predictions exist
        pass_hit = 0.0
        pass_miss = 1.0
    else:
        pass_hit = 0.0
        pass_miss = 0.0

    # File accuracy
    actual_files = set(actual_files_changed or [])
    pred_file_set = set(pred_files)
    if pred_file_set and actual_files:
        file_accuracy = len(pred_file_set & actual_files) / len(pred_file_set)
    elif pred_file_set:
        file_accuracy = 0.0
    else:
        file_accuracy = 1.0  # No prediction = no error

    # Composite score: reward hits, penalize misses and regression
    score = pass_hit - pass_miss - (0.5 if regression else 0.0) + (0.2 * file_accuracy)
    score = max(-1.0, min(1.0, score))

    # Classify error type
    if score >= 0.5:
        error_type = "accurate"
    elif pass_miss > 0.5:
        error_type = "wrong_direction"
    elif regression:
        error_type = "overconfident"
    else:
        error_type = "overconfident"

    return {
        "pass_hit": round(pass_hit, 3),
        "pass_miss": round(pass_miss, 3),
        "file_accuracy": round(file_accuracy, 3),
        "regression": regression,
        "score": round(score, 3),
        "has_predictions": True,
        "error_type": error_type,
    }


def build_prediction_error_hint(
    prediction_error: dict,
    decide_record: dict,
) -> str:
    """Build a targeted retry hint from prediction error.

    Returns empty string if no predictions were made (legacy DECIDE).
    """
    if not prediction_error.get("has_predictions"):
        return ""

    score = prediction_error["score"]
    error_type = prediction_error["error_type"]
    hypothesis = decide_record.get("testable_hypothesis", "")

    if error_type == "accurate":
        return ""  # No correction needed

    parts = ["PREDICTION ERROR FEEDBACK:"]

    if error_type == "wrong_direction":
        parts.append(
            f"Your hypothesis was WRONG: \"{hypothesis[:150]}\"\n"
            f"Pass rate: {prediction_error['pass_hit']:.0%} hit, {prediction_error['pass_miss']:.0%} miss.\n"
            "Your fundamental assumption about the bug is incorrect.\n"
            "You MUST form a completely new hypothesis — do not refine the old one."
        )
    elif error_type == "overconfident":
        if prediction_error["regression"]:
            parts.append(
                f"REGRESSION detected. Your fix broke existing tests.\n"
                f"Hypothesis: \"{hypothesis[:150]}\"\n"
                "Your approach is directionally correct but damages invariants.\n"
                "Rethink: how can you fix the bug WITHOUT modifying shared behavior?"
            )
        else:
            parts.append(
                f"Partial success: {prediction_error['pass_hit']:.0%} of predicted tests pass.\n"
                f"Hypothesis: \"{hypothesis[:150]}\"\n"
                "Your direction may be right but coverage is incomplete.\n"
                "Identify which edge cases your fix missed."
            )

    # File accuracy feedback
    if prediction_error["file_accuracy"] < 0.5:
        pred_files = decide_record.get("expected_files_to_change", [])
        if pred_files:
            parts.append(
                f"FILE MISMATCH: You predicted changing {pred_files} "
                f"but file accuracy was {prediction_error['file_accuracy']:.0%}.\n"
                "Re-examine which files actually need modification."
            )

    parts.append(f"Prediction score: {score:.2f} (range: -1 to +1)")

    return "\n".join(parts)


def route_from_prediction_error(prediction_error: dict) -> str | None:
    """Suggest next phase based on prediction error severity.

    Returns:
        Phase name to route to, or None for default routing.
    """
    if not prediction_error.get("has_predictions"):
        return None

    score = prediction_error["score"]

    if score < -0.5:
        return "ANALYZE"   # Severe error → re-analyze from scratch
    elif score < 0.0:
        return "DECIDE"    # Moderate error → re-decide with feedback
    else:
        return None        # Acceptable → use default routing
