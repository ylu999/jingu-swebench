"""
test_strategy_learning.py — p178: unit tests for strategy learning v1.

Tests:
  1. log → load round-trip (strategy_logger)
  2. aggregate: win rate computation, MIN_SAMPLES threshold
  3. ε-greedy selection in build_retry_plan:
     - EPSILON=0.0 (always exploit) uses best known hint
     - EPSILON=1.0 (always explore) uses default intervention hint
     - unknown bucket → fallback to default hint
  4. make_bucket_key: failure_class × enforced_violations
  5. strategy_table cache: returns same dict on second call
"""

import sys
import os
import json
import tempfile
sys.path.insert(0, os.path.dirname(__file__))

import pytest
from pathlib import Path

from strategy_logger import (
    StrategyLogEntry,
    make_entry,
    log_strategy_entry,
    load_strategy_log,
    make_bucket_key,
)

# ── 1. Log round-trip ─────────────────────────────────────────────────────────

class TestStrategyLoggerRoundTrip:
    def test_make_entry_returns_entry(self):
        e = make_entry(
            instance_id="django__django-11019",
            attempt_id=1,
            failure_class="no_effect_patch",
            control_action="ADJUST",
            steps_since_last_signal=0,
            enforced_violation_codes=[],
            hint_used="Trace the failing test",
            next_attempt_admitted=True,
            instance_final_admitted=True,
        )
        assert isinstance(e, StrategyLogEntry)
        assert e.instance_id == "django__django-11019"
        assert e.failure_class == "no_effect_patch"
        assert e.next_attempt_admitted is True
        assert e.instance_final_admitted is True
        assert e.outcome == "solved"  # derived from instance_final_admitted

    def test_log_and_load(self, tmp_path):
        log_path = tmp_path / "strategy_log.jsonl"
        e = make_entry(
            instance_id="django__django-11019",
            attempt_id=1,
            failure_class="no_effect_patch",
            control_action="ADJUST",
            steps_since_last_signal=2,
            enforced_violation_codes=[],
            hint_used="Trace the failing test to exact line",
            next_attempt_admitted=True,
            next_attempt_has_patch=True,
            instance_final_admitted=True,
            tests_delta=3,
        )
        log_strategy_entry(e, log_path)
        entries = load_strategy_log(log_path)
        assert len(entries) == 1
        loaded = entries[0]
        assert loaded.instance_id == e.instance_id
        assert loaded.failure_class == e.failure_class
        assert loaded.next_attempt_admitted is True
        assert loaded.next_attempt_has_patch is True
        assert loaded.instance_final_admitted is True
        assert loaded.tests_delta == 3

    def test_multiple_entries_appended(self, tmp_path):
        log_path = tmp_path / "strategy_log.jsonl"
        for i in range(5):
            e = make_entry(
                instance_id=f"django__django-{i}",
                attempt_id=1,
                failure_class="no_effect_patch",
                control_action="ADJUST",
                steps_since_last_signal=0,
                enforced_violation_codes=[],
                hint_used=f"hint {i}",
                next_attempt_admitted=False,
                instance_final_admitted=False,
            )
            log_strategy_entry(e, log_path)
        entries = load_strategy_log(log_path)
        assert len(entries) == 5

    def test_missing_log_returns_empty(self, tmp_path):
        entries = load_strategy_log(tmp_path / "nonexistent.jsonl")
        assert entries == []

    def test_hint_truncated_to_300(self):
        e = make_entry(
            instance_id="x",
            attempt_id=1,
            failure_class="unknown",
            control_action="CONTINUE",
            steps_since_last_signal=0,
            enforced_violation_codes=[],
            hint_used="X" * 400,
        )
        assert len(e.hint_used) == 300


# ── 2. make_bucket_key ────────────────────────────────────────────────────────

class TestMakeBucketKey:
    def test_no_violations(self):
        assert make_bucket_key("no_effect_patch", []) == "no_effect_patch"

    def test_single_violation(self):
        key = make_bucket_key("no_effect_patch", ["ENV_LEAKAGE_HARDCODE_PATH"])
        assert key == "no_effect_patch|ENV_LEAKAGE_HARDCODE_PATH"

    def test_two_violations_sorted(self):
        key = make_bucket_key("unknown", ["PLAN_NO_FEEDBACK_LOOP", "ENV_LEAKAGE_HARDCODE_PATH"])
        assert key == "unknown|ENV_LEAKAGE_HARDCODE_PATH|PLAN_NO_FEEDBACK_LOOP"

    def test_exploration_loop(self):
        assert make_bucket_key("exploration_loop", []) == "exploration_loop"


# ── 3. aggregate (aggregate_strategies.py) ────────────────────────────────────

class TestAggregate:
    def _write_entries(self, tmp_path, entries: list[dict]) -> Path:
        log_path = tmp_path / "strategy_log.jsonl"
        for kw in entries:
            e = make_entry(**kw)
            log_strategy_entry(e, log_path)
        return log_path

    def _aggregate(self, log_path, out_path, min_samples=3):
        import importlib.util, sys as _sys
        spec = importlib.util.spec_from_file_location(
            "aggregate_strategies",
            os.path.join(os.path.dirname(__file__), "aggregate_strategies.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.aggregate(log_path, out_path)

    def test_win_rate_computed(self, tmp_path):
        """p178.1: win_rate based on next_attempt_admitted (retry-level reward)."""
        log_path = self._write_entries(tmp_path, [
            dict(instance_id="a", attempt_id=1, failure_class="no_effect_patch",
                 control_action="ADJUST", steps_since_last_signal=0, enforced_violation_codes=[],
                 hint_used="Trace the failing test",
                 next_attempt_admitted=True, instance_final_admitted=True),
            dict(instance_id="b", attempt_id=1, failure_class="no_effect_patch",
                 control_action="ADJUST", steps_since_last_signal=0, enforced_violation_codes=[],
                 hint_used="Trace the failing test",
                 next_attempt_admitted=True, instance_final_admitted=True),
            dict(instance_id="c", attempt_id=1, failure_class="no_effect_patch",
                 control_action="ADJUST", steps_since_last_signal=0, enforced_violation_codes=[],
                 hint_used="Trace the failing test",
                 next_attempt_admitted=False, instance_final_admitted=False),
        ])
        table = self._aggregate(log_path, tmp_path / "table.json")
        key = "no_effect_patch"
        assert key in table
        hint_data = table[key].get("Trace the failing test")
        assert hint_data is not None
        assert abs(hint_data["win_rate"] - 2/3) < 0.01
        assert hint_data["total"] == 3
        assert hint_data["solved"] == 2

    def test_trusted_flag_at_min_samples(self, tmp_path):
        """Exactly MIN_SAMPLES entries → trusted=True."""
        entries = [
            dict(instance_id=f"i{i}", attempt_id=1, failure_class="exploration_loop",
                 control_action="ADJUST", steps_since_last_signal=0, enforced_violation_codes=[],
                 hint_used="Go directly to fix",
                 next_attempt_admitted=True, instance_final_admitted=True)
            for i in range(3)
        ]
        log_path = self._write_entries(tmp_path, entries)
        table = self._aggregate(log_path, tmp_path / "table.json")
        hint_data = table["exploration_loop"].get("Go directly to fix")
        assert hint_data is not None
        assert hint_data["trusted"] is True

    def test_not_trusted_below_min_samples(self, tmp_path):
        """2 entries → trusted=False."""
        entries = [
            dict(instance_id=f"i{i}", attempt_id=1, failure_class="exploration_loop",
                 control_action="ADJUST", steps_since_last_signal=0, enforced_violation_codes=[],
                 hint_used="Go directly to fix",
                 next_attempt_admitted=True, instance_final_admitted=True)
            for i in range(2)
        ]
        log_path = self._write_entries(tmp_path, entries)
        table = self._aggregate(log_path, tmp_path / "table.json")
        hint_data = table["exploration_loop"].get("Go directly to fix")
        assert hint_data["trusted"] is False

    def test_multiple_hints_different_win_rates(self, tmp_path):
        """hint_A: all next_attempt_admitted=True → win=1.0; hint_B: all False → win=0.0."""
        entries = [
            dict(instance_id="a1", attempt_id=1, failure_class="no_effect_patch",
                 control_action="ADJUST", steps_since_last_signal=0, enforced_violation_codes=[],
                 hint_used="hint_A", next_attempt_admitted=True, instance_final_admitted=True),
            dict(instance_id="a2", attempt_id=1, failure_class="no_effect_patch",
                 control_action="ADJUST", steps_since_last_signal=0, enforced_violation_codes=[],
                 hint_used="hint_A", next_attempt_admitted=True, instance_final_admitted=True),
            dict(instance_id="a3", attempt_id=1, failure_class="no_effect_patch",
                 control_action="ADJUST", steps_since_last_signal=0, enforced_violation_codes=[],
                 hint_used="hint_A", next_attempt_admitted=True, instance_final_admitted=True),
            dict(instance_id="b1", attempt_id=1, failure_class="no_effect_patch",
                 control_action="ADJUST", steps_since_last_signal=0, enforced_violation_codes=[],
                 hint_used="hint_B", next_attempt_admitted=False, instance_final_admitted=False),
            dict(instance_id="b2", attempt_id=1, failure_class="no_effect_patch",
                 control_action="ADJUST", steps_since_last_signal=0, enforced_violation_codes=[],
                 hint_used="hint_B", next_attempt_admitted=False, instance_final_admitted=False),
            dict(instance_id="b3", attempt_id=1, failure_class="no_effect_patch",
                 control_action="ADJUST", steps_since_last_signal=0, enforced_violation_codes=[],
                 hint_used="hint_B", next_attempt_admitted=False, instance_final_admitted=False),
        ]
        log_path = self._write_entries(tmp_path, entries)
        table = self._aggregate(log_path, tmp_path / "table.json")
        bucket = table["no_effect_patch"]
        assert bucket["hint_A"]["win_rate"] == 1.0
        assert bucket["hint_B"]["win_rate"] == 0.0


# ── 4. ε-greedy in build_retry_plan ──────────────────────────────────────────

from retry_controller import build_retry_plan, EPSILON, _load_strategy_table

def _base_jb(ran_tests=True, last_passed=False, files_written=None):
    return {
        "exit_status": "Submitted",
        "files_written": files_written or ["django/forms/widgets.py"],
        "test_results": {"ran_tests": ran_tests, "last_passed": last_passed, "excerpt": "FAIL"},
    }

def _base_fp(lines_added=5, lines_removed=2):
    return {"files": ["django/forms/widgets.py"], "hunks": 1,
            "lines_added": lines_added, "lines_removed": lines_removed}


class TestEpsilonGreedy:
    def _make_table(self, tmp_path, bucket_key: str, hints: dict) -> Path:
        """Write a strategy_table.json with given hints for a bucket."""
        table = {bucket_key: hints}
        p = tmp_path / "strategy_table.json"
        p.write_text(json.dumps(table))
        return p

    def test_epsilon_zero_exploits_best_hint(self, tmp_path, monkeypatch):
        """EPSILON=0.0 → always exploit → uses highest win_rate hint."""
        monkeypatch.setattr("retry_controller.EPSILON", 0.0)
        table_path = self._make_table(tmp_path, "no_effect_patch", {
            "good_hint": {"win_rate": 0.9, "sample_count": 10, "solved": 9, "total": 10, "trusted": True},
            "bad_hint":  {"win_rate": 0.1, "sample_count": 10, "solved": 1, "total": 10, "trusted": True},
        })
        plan = build_retry_plan(
            problem_statement="fix it",
            patch_text="diff --git a/foo.py",
            jingu_body=_base_jb(),
            fail_to_pass_tests=["test_foo"],
            gate_admitted=True,
            gate_reason_codes=[],
            patch_fp=_base_fp(),
            strategy_table_path=str(table_path),
        )
        assert "good_hint" in plan.next_attempt_prompt

    def test_epsilon_one_explores_uses_default_hint(self, tmp_path, monkeypatch):
        """EPSILON=1.0 → always explore → uses default intervention hint (not table)."""
        monkeypatch.setattr("retry_controller.EPSILON", 1.0)
        table_path = self._make_table(tmp_path, "no_effect_patch", {
            "good_hint": {"win_rate": 0.9, "sample_count": 10, "solved": 9, "total": 10, "trusted": True},
        })
        plan = build_retry_plan(
            problem_statement="fix it",
            patch_text="diff --git a/foo.py",
            jingu_body=_base_jb(),
            fail_to_pass_tests=["test_foo"],
            gate_admitted=True,
            gate_reason_codes=[],
            patch_fp=_base_fp(),
            strategy_table_path=str(table_path),
        )
        # Default hint for no_effect_patch: "Previous attempt changed code but had no effect"
        assert "good_hint" not in plan.next_attempt_prompt
        # Should contain the default intervention hint text
        assert "no effect" in plan.next_attempt_prompt.lower() or \
               "failing test" in plan.next_attempt_prompt.lower()

    def test_unknown_bucket_falls_back_to_default(self, tmp_path, monkeypatch):
        """Unknown bucket key → cold-start → use default hint regardless of EPSILON."""
        monkeypatch.setattr("retry_controller.EPSILON", 0.0)
        # Table has a different bucket key
        table_path = self._make_table(tmp_path, "wrong_direction", {
            "some_hint": {"win_rate": 0.9, "sample_count": 5, "solved": 4, "total": 5, "trusted": True},
        })
        plan = build_retry_plan(
            problem_statement="fix it",
            patch_text="diff --git a/foo.py",
            jingu_body=_base_jb(),
            fail_to_pass_tests=["test_foo"],
            gate_admitted=True,
            gate_reason_codes=[],
            patch_fp=_base_fp(),
            strategy_table_path=str(table_path),
        )
        # no_effect_patch bucket not in table → falls back to default hint
        assert "some_hint" not in plan.next_attempt_prompt

    def test_no_table_path_uses_default(self):
        """No strategy_table_path → always use default hint."""
        plan = build_retry_plan(
            problem_statement="fix it",
            patch_text="diff --git a/foo.py",
            jingu_body=_base_jb(),
            fail_to_pass_tests=["test_foo"],
            gate_admitted=True,
            gate_reason_codes=[],
            patch_fp=_base_fp(),
            strategy_table_path=None,
        )
        # Should have some hint text from _INTERVENTIONS
        assert len(plan.next_attempt_prompt) > 0

    def test_untrusted_hints_not_exploited(self, tmp_path, monkeypatch):
        """Bucket with only untrusted hints → cold-start → use default hint."""
        monkeypatch.setattr("retry_controller.EPSILON", 0.0)
        table_path = self._make_table(tmp_path, "no_effect_patch", {
            "cold_hint": {"win_rate": 0.9, "sample_count": 2, "solved": 1, "total": 2, "trusted": False},
        })
        plan = build_retry_plan(
            problem_statement="fix it",
            patch_text="diff --git a/foo.py",
            jingu_body=_base_jb(),
            fail_to_pass_tests=["test_foo"],
            gate_admitted=True,
            gate_reason_codes=[],
            patch_fp=_base_fp(),
            strategy_table_path=str(table_path),
        )
        assert "cold_hint" not in plan.next_attempt_prompt

    def test_principal_hints_still_prepended_when_exploiting(self, tmp_path, monkeypatch):
        """Even when exploiting table hint, enforced principal hints are still prepended."""
        monkeypatch.setattr("retry_controller.EPSILON", 0.0)
        table_path = self._make_table(tmp_path, "no_effect_patch|ENV_LEAKAGE_HARDCODE_PATH", {
            "good_env_hint": {"win_rate": 0.8, "sample_count": 5, "solved": 4, "total": 5, "trusted": True},
        })
        plan = build_retry_plan(
            problem_statement="fix it",
            patch_text="diff --git a/foo.py",
            jingu_body=_base_jb(),
            fail_to_pass_tests=["test_foo"],
            gate_admitted=True,
            gate_reason_codes=[],
            patch_fp=_base_fp(),
            principal_violation_codes=["ENV_LEAKAGE_HARDCODE_PATH"],
            strategy_table_path=str(table_path),
        )
        # ENV violation hint must appear
        assert "ENVIRONMENT ASSUMPTION" in plan.next_attempt_prompt or \
               "environment" in plan.next_attempt_prompt.lower()


# ── 5. _load_strategy_table cache ─────────────────────────────────────────────

class TestStrategyTableCache:
    def test_load_nonexistent_returns_empty(self, tmp_path):
        table = _load_strategy_table(tmp_path / "nonexistent.json")
        assert table == {}

    def test_load_none_returns_empty(self):
        assert _load_strategy_table(None) == {}

    def test_load_valid_table(self, tmp_path):
        data = {"no_effect_patch": {"hint_A": {"win_rate": 0.7, "sample_count": 10,
                                               "solved": 7, "total": 10, "trusted": True}}}
        p = tmp_path / "table.json"
        p.write_text(json.dumps(data))
        table = _load_strategy_table(str(p))
        assert "no_effect_patch" in table

    def test_cache_returns_same_dict(self, tmp_path):
        import retry_controller as rc
        rc._strategy_table_cache = None  # clear cache
        data = {"k": {"h": {"win_rate": 0.5, "sample_count": 3, "solved": 1, "total": 3, "trusted": True}}}
        p = tmp_path / "table.json"
        p.write_text(json.dumps(data))
        t1 = _load_strategy_table(str(p))
        t2 = _load_strategy_table(str(p))
        assert t1 is t2  # same object from cache

    def test_cache_invalidated_on_mtime_change(self, tmp_path):
        import retry_controller as rc
        rc._strategy_table_cache = None
        p = tmp_path / "table.json"
        p.write_text(json.dumps({"k1": {}}))
        t1 = _load_strategy_table(str(p))
        # Modify file (force mtime change)
        import time as _time
        _time.sleep(0.01)
        p.write_text(json.dumps({"k2": {}}))
        t2 = _load_strategy_table(str(p))
        assert t1 is not t2
        assert "k2" in t2
