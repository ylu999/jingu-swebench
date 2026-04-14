"""Tests for prediction_error.py — P2 Decision Prediction Error."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from prediction_error import compute_prediction_error, PredictionError


class TestPredictionError:
    def _make_decide_record(self, hypothesis="", tests=None, files=None):
        return {
            "phase": "DECIDE",
            "subtype": "decide.strategy_selection",
            "testable_hypothesis": hypothesis,
            "expected_tests_to_pass": tests or [],
            "expected_files_to_change": files or [],
        }

    def test_no_decide_record(self):
        result = compute_prediction_error([], {"f2p_passed": 1, "f2p_failed": 0})
        assert result.error_type == "prediction_no_data"
        assert result.repair_target == "DECIDE"

    def test_decide_record_no_predictions(self):
        records = [self._make_decide_record()]
        result = compute_prediction_error(records, {"f2p_passed": 1, "f2p_failed": 0})
        assert result.error_type == "prediction_no_data"

    def test_prediction_correct(self):
        records = [self._make_decide_record(
            hypothesis="Fix dateparse regex to handle negative offsets",
            tests=["test_parse_duration"],
        )]
        result = compute_prediction_error(records, {"f2p_passed": 2, "f2p_failed": 0})
        assert result.error_type == "prediction_correct"
        assert result.severity == 0.0
        assert result.repair_target == ""

    def test_prediction_wrong_direction_zero_pass(self):
        records = [self._make_decide_record(
            hypothesis="Fix union query ordering",
            tests=["test_union_with_values_list"],
        )]
        result = compute_prediction_error(records, {"f2p_passed": 0, "f2p_failed": 3})
        assert result.error_type == "prediction_wrong_direction"
        assert result.severity == 1.0
        assert result.repair_target == "ANALYZE"
        assert "PREDICTION WRONG" in result.feedback

    def test_prediction_partial_majority_pass(self):
        records = [self._make_decide_record(
            hypothesis="Fix model query combinator",
            tests=["test_a", "test_b"],
        )]
        result = compute_prediction_error(records, {"f2p_passed": 3, "f2p_failed": 1})
        assert result.error_type == "prediction_partial"
        assert result.repair_target == "EXECUTE"
        assert result.severity < 0.5

    def test_prediction_wrong_direction_minority_pass(self):
        records = [self._make_decide_record(
            hypothesis="Fix dateparse",
            tests=["test_parse"],
        )]
        result = compute_prediction_error(records, {"f2p_passed": 1, "f2p_failed": 4})
        assert result.error_type == "prediction_wrong_direction"
        assert result.repair_target == "DECIDE"
        assert result.severity > 0.5

    def test_no_f2p_tests_ran(self):
        records = [self._make_decide_record(
            hypothesis="Fix something",
            tests=["test_x"],
        )]
        result = compute_prediction_error(records, {"f2p_passed": 0, "f2p_failed": 0})
        assert result.error_type == "prediction_no_data"

    def test_to_dict(self):
        records = [self._make_decide_record(
            hypothesis="Test hypothesis",
            tests=["test_1"],
        )]
        result = compute_prediction_error(records, {"f2p_passed": 0, "f2p_failed": 2})
        d = result.to_dict()
        assert "error_type" in d
        assert "severity" in d
        assert "feedback" in d
        assert d["error_type"] == "prediction_wrong_direction"

    def test_finds_last_decide_record(self):
        """Should use the LAST DECIDE record if multiple exist."""
        records = [
            self._make_decide_record(hypothesis="Old wrong hypothesis"),
            {"phase": "EXECUTE", "subtype": "execute.code_patch"},
            self._make_decide_record(
                hypothesis="New correct hypothesis",
                tests=["test_fixed"],
            ),
        ]
        result = compute_prediction_error(records, {"f2p_passed": 2, "f2p_failed": 0})
        assert result.error_type == "prediction_correct"
        assert "New correct hypothesis" in result.hypothesis

    def test_predicted_tests_as_string(self):
        """Handle expected_tests_to_pass as comma-separated string."""
        records = [{
            "phase": "DECIDE",
            "testable_hypothesis": "Fix X",
            "expected_tests_to_pass": "test_a, test_b, test_c",
        }]
        result = compute_prediction_error(records, {"f2p_passed": 0, "f2p_failed": 1})
        assert len(result.predicted_tests) == 3

    def test_analyze_fallback_when_no_decide(self):
        """Fall back to ANALYZE root_cause when DECIDE record missing."""
        records = [{
            "phase": "ANALYZE",
            "root_cause": "The regex does not handle negative offsets",
        }]
        result = compute_prediction_error(records, {"f2p_passed": 2, "f2p_failed": 0})
        assert result.error_type == "prediction_correct"
        assert "[from ANALYZE]" in result.hypothesis

    def test_analyze_fallback_wrong_direction(self):
        """ANALYZE fallback still classifies wrong_direction correctly."""
        records = [{
            "phase": "ANALYZE",
            "root_cause": "Missing null check in validator",
        }]
        result = compute_prediction_error(records, {"f2p_passed": 0, "f2p_failed": 3})
        assert result.error_type == "prediction_wrong_direction"
        assert result.severity == 1.0

    def test_analyze_fallback_ignored_when_decide_exists(self):
        """DECIDE record takes priority over ANALYZE even when both exist."""
        records = [
            {"phase": "ANALYZE", "root_cause": "Old analysis"},
            {"phase": "DECIDE", "testable_hypothesis": "Fix the parser"},
        ]
        result = compute_prediction_error(records, {"f2p_passed": 1, "f2p_failed": 0})
        assert result.error_type == "prediction_correct"
        assert "Fix the parser" in result.hypothesis
        assert "[from ANALYZE]" not in result.hypothesis
