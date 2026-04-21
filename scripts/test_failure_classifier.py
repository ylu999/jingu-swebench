"""Tests for failure_classifier module (p208).

Covers all 4 failure types, success case, edge cases, and routing rules.
"""
import unittest
from failure_classifier import classify_failure, get_routing, FAILURE_ROUTING_RULES, FailureType


class TestClassifyFailure(unittest.TestCase):
    """Test classify_failure with all classification paths."""

    # --- Rule 1: execution_error ---

    def test_controlled_error_returns_execution_error(self):
        cv = {"verification_kind": "controlled_error", "f2p_passed": 0, "f2p_failed": 3}
        self.assertEqual(classify_failure(cv), "execution_error")

    def test_controlled_error_ignores_f2p_counts(self):
        """Even if f2p counts suggest incomplete_fix, controlled_error takes priority."""
        cv = {"verification_kind": "controlled_error", "f2p_passed": 2, "f2p_failed": 1}
        self.assertEqual(classify_failure(cv), "execution_error")

    # --- Rule 2: near_miss (high pass rate, few failures, no regression) ---

    def test_near_miss_high_pass_rate(self):
        """436/438 = 99.5% pass rate, 2 remaining, no regression -> near_miss."""
        cv = {
            "verification_kind": "controlled_fail_to_pass",
            "f2p_passed": 436, "f2p_failed": 2, "p2p_failed": 0,
        }
        self.assertEqual(classify_failure(cv), "near_miss")

    def test_near_miss_5_of_6(self):
        """5/6 = 83% pass rate, 1 remaining, no regression -> near_miss."""
        cv = {
            "verification_kind": "controlled_fail_to_pass",
            "f2p_passed": 5, "f2p_failed": 1, "p2p_failed": 0,
        }
        self.assertEqual(classify_failure(cv), "near_miss")

    def test_near_miss_boundary_80_percent(self):
        """4/5 = 80% exactly -> near_miss."""
        cv = {
            "verification_kind": "controlled_fail_to_pass",
            "f2p_passed": 4, "f2p_failed": 1, "p2p_failed": 0,
        }
        self.assertEqual(classify_failure(cv), "near_miss")

    def test_near_miss_with_regression_becomes_incomplete(self):
        """High pass rate but p2p regression -> incomplete_fix, not near_miss."""
        cv = {
            "verification_kind": "controlled_fail_to_pass",
            "f2p_passed": 5, "f2p_failed": 1, "p2p_failed": 1,
        }
        self.assertEqual(classify_failure(cv), "incomplete_fix")

    def test_below_80_percent_is_incomplete_fix(self):
        """3/5 = 60% -> incomplete_fix, not near_miss."""
        cv = {
            "verification_kind": "controlled_fail_to_pass",
            "f2p_passed": 3, "f2p_failed": 2,
        }
        self.assertEqual(classify_failure(cv), "incomplete_fix")

    # --- Rule 3: incomplete_fix ---

    def test_partial_f2p_one_each(self):
        cv = {
            "verification_kind": "controlled_fail_to_pass",
            "f2p_passed": 1, "f2p_failed": 1,
        }
        self.assertEqual(classify_failure(cv), "incomplete_fix")

    # --- Rule 3: wrong_direction ---

    def test_zero_pass_nonzero_fail_returns_wrong_direction(self):
        cv = {
            "verification_kind": "controlled_fail_to_pass",
            "f2p_passed": 0, "f2p_failed": 5,
        }
        self.assertEqual(classify_failure(cv), "wrong_direction")

    def test_none_pass_nonzero_fail_returns_wrong_direction(self):
        """f2p_passed=None should be treated as 0."""
        cv = {
            "verification_kind": "controlled_fail_to_pass",
            "f2p_passed": None, "f2p_failed": 3,
        }
        self.assertEqual(classify_failure(cv), "wrong_direction")

    # --- Rule 4: verify_gap ---

    def test_all_f2p_pass_not_resolved_returns_verify_gap(self):
        cv = {
            "verification_kind": "controlled_fail_to_pass",
            "f2p_passed": 5, "f2p_failed": 0,
            "eval_resolved": False,
        }
        self.assertEqual(classify_failure(cv), "verify_gap")

    def test_all_f2p_pass_no_eval_resolved_returns_verify_gap(self):
        """Missing eval_resolved field -> not resolved -> verify_gap."""
        cv = {
            "verification_kind": "controlled_fail_to_pass",
            "f2p_passed": 3, "f2p_failed": 0,
        }
        self.assertEqual(classify_failure(cv), "verify_gap")

    # --- Success (None) ---

    def test_all_f2p_pass_eval_resolved_returns_none(self):
        cv = {
            "verification_kind": "controlled_fail_to_pass",
            "f2p_passed": 5, "f2p_failed": 0,
            "eval_resolved": True,
        }
        self.assertIsNone(classify_failure(cv))

    def test_eval_resolved_zero_counts_returns_none(self):
        """Edge: both counts 0 but eval_resolved=True -> success."""
        cv = {
            "verification_kind": "controlled_fail_to_pass",
            "f2p_passed": 0, "f2p_failed": 0,
            "eval_resolved": True,
        }
        self.assertIsNone(classify_failure(cv))

    # --- Edge cases ---

    def test_empty_dict_returns_none(self):
        self.assertIsNone(classify_failure({}))

    def test_none_input_returns_none(self):
        self.assertIsNone(classify_failure(None))

    def test_non_dict_input_returns_none(self):
        self.assertIsNone(classify_failure("not a dict"))

    def test_controlled_no_tests_returns_none(self):
        cv = {"verification_kind": "controlled_no_tests", "f2p_passed": -1, "f2p_failed": -1}
        self.assertIsNone(classify_failure(cv))

    def test_all_zeros_not_resolved_returns_wrong_direction(self):
        """Both counts 0, not resolved -> fallback to wrong_direction."""
        cv = {
            "verification_kind": "controlled_fail_to_pass",
            "f2p_passed": 0, "f2p_failed": 0,
            "eval_resolved": False,
        }
        self.assertEqual(classify_failure(cv), "wrong_direction")

    def test_missing_f2p_fields_returns_wrong_direction(self):
        """Only verification_kind present, no f2p data -> fallback."""
        cv = {"verification_kind": "controlled_fail_to_pass"}
        self.assertEqual(classify_failure(cv), "wrong_direction")

    def test_f2p_failed_none_treated_as_zero(self):
        """f2p_failed=None with f2p_passed > 0 -> verify_gap."""
        cv = {
            "verification_kind": "controlled_fail_to_pass",
            "f2p_passed": 3, "f2p_failed": None,
        }
        self.assertEqual(classify_failure(cv), "verify_gap")


class TestGetRouting(unittest.TestCase):
    """Test get_routing returns correct routing rules."""

    def test_wrong_direction_routing(self):
        r = get_routing("wrong_direction")
        self.assertEqual(r["next_phase"], "ANALYZE")
        self.assertIn("causal_grounding", r["required_principals"])

    def test_incomplete_fix_routing(self):
        r = get_routing("incomplete_fix")
        self.assertEqual(r["next_phase"], "DESIGN")
        self.assertIn("ontology_alignment", r["required_principals"])

    def test_verify_gap_routing(self):
        r = get_routing("verify_gap")
        self.assertEqual(r["next_phase"], "DESIGN")
        self.assertIn("ontology_alignment", r["required_principals"])

    def test_execution_error_routing(self):
        r = get_routing("execution_error")
        self.assertEqual(r["next_phase"], "EXECUTE")
        self.assertIn("action_grounding", r["required_principals"])

    def test_near_miss_routing(self):
        r = get_routing("near_miss")
        self.assertEqual(r["next_phase"], "EXECUTE")
        self.assertIn("ALMOST correct", r["repair_goal"])

    def test_all_failure_types_have_routing(self):
        for ft in ("wrong_direction", "incomplete_fix", "verify_gap", "execution_error", "near_miss"):
            r = get_routing(ft)
            self.assertIn("next_phase", r)
            self.assertIn("repair_goal", r)
            self.assertIn("required_principals", r)

    def test_invalid_type_raises_keyerror(self):
        with self.assertRaises(KeyError):
            get_routing("nonexistent_type")


class TestRoutingRulesCompleteness(unittest.TestCase):
    """Verify FAILURE_ROUTING_RULES covers all expected types."""

    def test_five_types_covered(self):
        self.assertEqual(
            set(FAILURE_ROUTING_RULES.keys()),
            {"wrong_direction", "incomplete_fix", "verify_gap", "execution_error", "near_miss"},
        )

    def test_each_rule_has_required_fields(self):
        for ft, rule in FAILURE_ROUTING_RULES.items():
            self.assertIn("next_phase", rule, f"{ft} missing next_phase")
            self.assertIn("repair_goal", rule, f"{ft} missing repair_goal")
            self.assertIn("required_principals", rule, f"{ft} missing required_principals")
            self.assertIsInstance(rule["required_principals"], list,
                                 f"{ft} required_principals must be a list")


if __name__ == "__main__":
    unittest.main()
