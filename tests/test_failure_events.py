"""Tests for p216 failure event extraction pipeline.

Tests cover:
  - extract_failure_events: jingu_body path and messages-only path
  - compute_routing_stats: aggregation correctness
  - suggest_routing: matrix format and strategy selection
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Add scripts/ to sys.path so imports work
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from extract_failure_events import (
    FailureEvent,
    extract_failure_events,
    extract_failure_events_from_dict,
    extract_from_batch_dir,
    failure_events_to_dicts,
)
from compute_routing_stats import (
    compute_routing_stats,
    get_best_next_phase,
    get_top_failures,
)
from suggest_routing import (
    suggest_routing,
    suggest_routing_from_events,
)


# ── Sample traj data (jingu_body format) ──────────────────────────────────

SAMPLE_TRAJ_JINGU_BODY = {
    "instance_id": "django__django-11099",
    "messages": [],
    "info": {"exit_status": "Submitted"},
    "jingu_body": {
        "phase_records": [
            {"phase": "ANALYZE", "subtype": "analysis.root_cause"},
            {"phase": "EXECUTE", "subtype": "execution.code_patch"},
        ],
        "principal_inference": [
            {
                "phase": "ANALYZE",
                "subtype": "analysis.root_cause",
                "declared": ["causal_grounding", "evidence_linkage"],
                "inferred": {
                    "present": ["evidence_linkage"],
                    "absent": ["causal_grounding"],
                },
                "diff": {
                    "missing_required": ["ontology_alignment"],
                    "missing_expected": [],
                    "fake": ["causal_grounding"],
                },
            },
        ],
        "controlled_verify": {
            "f2p_passed": 0,
            "f2p_failed": 3,
            "p2p_passed": 10,
            "p2p_failed": 0,
            "eval_resolved": False,
        },
        "test_results": {"ran_tests": True},
    },
}


SAMPLE_TRAJ_RESOLVED = {
    "instance_id": "django__django-10914",
    "messages": [],
    "info": {"exit_status": "Submitted"},
    "jingu_body": {
        "phase_records": [
            {"phase": "ANALYZE", "subtype": "analysis.root_cause"},
            {"phase": "EXECUTE", "subtype": "execution.code_patch"},
        ],
        "principal_inference": [
            {
                "phase": "ANALYZE",
                "subtype": "analysis.root_cause",
                "declared": ["causal_grounding"],
                "inferred": {"present": ["causal_grounding"], "absent": []},
                "diff": {
                    "missing_required": [],
                    "missing_expected": [],
                    "fake": [],
                },
            },
        ],
        "controlled_verify": {
            "f2p_passed": 3,
            "f2p_failed": 0,
            "p2p_passed": 10,
            "p2p_failed": 0,
            "eval_resolved": True,
        },
        "test_results": {"ran_tests": True},
    },
}


SAMPLE_TRAJ_REGRESSION = {
    "instance_id": "django__django-11001",
    "messages": [],
    "info": {"exit_status": "Submitted"},
    "jingu_body": {
        "phase_records": [
            {"phase": "EXECUTE", "subtype": "execution.code_patch"},
        ],
        "principal_inference": [],
        "controlled_verify": {
            "f2p_passed": 2,
            "f2p_failed": 1,
            "p2p_passed": 8,
            "p2p_failed": 2,
            "eval_resolved": False,
        },
        "test_results": {"ran_tests": True},
    },
}


# ── Sample traj data (messages-only format, no jingu_body) ────────────────

SAMPLE_TRAJ_MESSAGES_ONLY = {
    "instance_id": "django__django-11039",
    "messages": [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "Fix the bug in django forms."},
        {
            "role": "assistant",
            "content": "PHASE: ANALYZE\nLet me analyze the problem.",
            "extra": {"actions": []},
        },
        {
            "role": "assistant",
            "content": "PHASE: EXECUTE\nI will fix this.",
            "extra": {
                "actions": [
                    {"tool": "str_replace_editor", "input": {"path": "/testbed/file.py"}},
                ]
            },
        },
        {
            "role": "tool",
            "content": "FAILED (failures=5)\ntest_merge FAILED",
        },
    ],
    "info": {"exit_status": "Submitted"},
}


SAMPLE_TRAJ_NO_PATCH = {
    "instance_id": "django__django-11049",
    "messages": [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "Fix the bug."},
        {
            "role": "assistant",
            "content": "PHASE: ANALYZE\nLet me look at the code.",
            "extra": {"actions": [{"tool": "view_file", "input": {"path": "/testbed/file.py"}}]},
        },
    ],
    "info": {"exit_status": "Submitted"},
}


# ── Tests: extract_failure_events ─────────────────────────────────────────


class TestExtractFailureEvents:

    def test_extract_from_jingu_body_unresolved(self):
        """Extract events from governance-enriched traj with failures."""
        events = extract_failure_events_from_dict(SAMPLE_TRAJ_JINGU_BODY)

        # Should have: missing_required(ontology_alignment) + fake(causal_grounding) + wrong_direction(test)
        assert len(events) >= 2

        # Check missing_required event
        missing_req = [e for e in events if e.reason == "missing_required"]
        assert len(missing_req) == 1
        assert missing_req[0].principal == "ontology_alignment"
        assert missing_req[0].phase == "ANALYZE"
        assert missing_req[0].outcome == "unresolved"

        # Check fake declaration event
        fake_events = [e for e in events if e.reason == "fake_declaration"]
        assert len(fake_events) == 1
        assert fake_events[0].principal == "causal_grounding"

        # Check test failure event
        test_events = [e for e in events if e.reason == "wrong_direction"]
        assert len(test_events) == 1

    def test_extract_from_jingu_body_resolved(self):
        """Resolved traj should produce no principal failure events."""
        events = extract_failure_events_from_dict(SAMPLE_TRAJ_RESOLVED)
        # No principal failures and no test failures
        assert len(events) == 0

    def test_extract_from_jingu_body_regression(self):
        """Regression should produce p2p_regression and test failure events."""
        events = extract_failure_events_from_dict(SAMPLE_TRAJ_REGRESSION)

        p2p_events = [e for e in events if e.reason == "p2p_regression"]
        assert len(p2p_events) == 1
        assert p2p_events[0].outcome == "regressed"
        assert p2p_events[0].principal == "minimal_change"

    def test_extract_from_messages_with_patch(self):
        """Messages-only traj with patch + test failure."""
        events = extract_failure_events_from_dict(SAMPLE_TRAJ_MESSAGES_ONLY)

        assert len(events) >= 1
        test_events = [e for e in events if e.reason == "test_failure"]
        assert len(test_events) == 1
        assert test_events[0].phase == "EXECUTE"

    def test_extract_from_messages_no_patch(self):
        """Messages-only traj with no patch produced."""
        events = extract_failure_events_from_dict(SAMPLE_TRAJ_NO_PATCH)

        assert len(events) >= 1
        no_patch = [e for e in events if e.reason == "no_patch_produced"]
        assert len(no_patch) == 1
        assert no_patch[0].principal == "action_grounding"

    def test_extract_from_file(self, tmp_path):
        """Test extraction from a file path."""
        traj_file = tmp_path / "test.traj.json"
        traj_file.write_text(json.dumps(SAMPLE_TRAJ_JINGU_BODY))

        events = extract_failure_events(str(traj_file))
        assert len(events) >= 2

    def test_attempt_override(self):
        """Attempt number should be correctly overridden."""
        events = extract_failure_events_from_dict(
            SAMPLE_TRAJ_JINGU_BODY,
            attempt=3,
        )
        for ev in events:
            assert ev.attempt == 3

    def test_failure_events_to_dicts(self):
        """FailureEvent list should serialize to dicts."""
        events = extract_failure_events_from_dict(SAMPLE_TRAJ_JINGU_BODY)
        dicts = failure_events_to_dicts(events)
        assert len(dicts) == len(events)
        for d in dicts:
            assert "phase" in d
            assert "principal" in d
            assert "reason" in d
            assert "outcome" in d

    def test_extract_from_batch_dir(self, tmp_path):
        """Test batch directory extraction."""
        # Create fake batch structure
        attempt_dir = tmp_path / "attempt_1" / "django__django-11099"
        attempt_dir.mkdir(parents=True)
        traj_file = attempt_dir / "django__django-11099.traj.json"
        traj_file.write_text(json.dumps(SAMPLE_TRAJ_JINGU_BODY))

        attempt_dir2 = tmp_path / "attempt_2" / "django__django-10914"
        attempt_dir2.mkdir(parents=True)
        traj_file2 = attempt_dir2 / "django__django-10914.traj.json"
        traj_file2.write_text(json.dumps(SAMPLE_TRAJ_RESOLVED))

        events = extract_from_batch_dir(str(tmp_path))
        # Attempt 1 should have failures, attempt 2 should have none
        att1_events = [e for e in events if e.attempt == 1]
        att2_events = [e for e in events if e.attempt == 2]
        assert len(att1_events) >= 2
        assert len(att2_events) == 0


# ── Tests: compute_routing_stats ──────────────────────────────────────────


class TestComputeRoutingStats:

    def _make_events(self) -> list[FailureEvent]:
        """Create a set of test events for aggregation."""
        return [
            FailureEvent("inst1", "ANALYZE", "principal_gate", "causal_grounding",
                          "missing_required", "unresolved", "ANALYZE", 1),
            FailureEvent("inst2", "ANALYZE", "principal_gate", "causal_grounding",
                          "missing_required", "resolved", "ANALYZE", 1),
            FailureEvent("inst3", "ANALYZE", "principal_gate", "causal_grounding",
                          "fake_declaration", "unresolved", "EXECUTE", 1),
            FailureEvent("inst1", "EXECUTE", "controlled_verify", "minimal_change",
                          "p2p_regression", "regressed", "EXECUTE", 1),
            FailureEvent("inst4", "ANALYZE", "principal_gate", "evidence_linkage",
                          "missing_required", "resolved", "ANALYZE", 2),
        ]

    def test_stats_keys(self):
        """Stats should have one key per (phase, principal) pair."""
        events = self._make_events()
        stats = compute_routing_stats(events)

        assert "ANALYZE:causal_grounding" in stats
        assert "EXECUTE:minimal_change" in stats
        assert "ANALYZE:evidence_linkage" in stats

    def test_stats_count(self):
        """Count should match number of events per group."""
        events = self._make_events()
        stats = compute_routing_stats(events)

        assert stats["ANALYZE:causal_grounding"]["count"] == 3
        assert stats["EXECUTE:minimal_change"]["count"] == 1
        assert stats["ANALYZE:evidence_linkage"]["count"] == 1

    def test_stats_outcomes(self):
        """Outcome distribution should be correct."""
        events = self._make_events()
        stats = compute_routing_stats(events)

        cg = stats["ANALYZE:causal_grounding"]
        assert cg["outcomes"]["unresolved"] == 2
        assert cg["outcomes"]["resolved"] == 1

    def test_stats_resolution_rate(self):
        """Resolution rate should be resolved / total."""
        events = self._make_events()
        stats = compute_routing_stats(events)

        cg = stats["ANALYZE:causal_grounding"]
        assert cg["resolution_rate"] == pytest.approx(1 / 3, abs=0.01)

    def test_stats_next_phase_stats(self):
        """Next phase stats should track per-next_phase resolution."""
        events = self._make_events()
        stats = compute_routing_stats(events)

        cg = stats["ANALYZE:causal_grounding"]
        nps = cg["next_phase_stats"]
        assert "ANALYZE" in nps
        assert "EXECUTE" in nps
        assert nps["ANALYZE"]["count"] == 2
        assert nps["ANALYZE"]["resolved"] == 1
        assert nps["EXECUTE"]["count"] == 1
        assert nps["EXECUTE"]["resolved"] == 0

    def test_stats_reasons(self):
        """Reason distribution should be correct."""
        events = self._make_events()
        stats = compute_routing_stats(events)

        cg = stats["ANALYZE:causal_grounding"]
        assert cg["reasons"]["missing_required"] == 2
        assert cg["reasons"]["fake_declaration"] == 1

    def test_stats_instances(self):
        """Instances should list unique affected instance IDs."""
        events = self._make_events()
        stats = compute_routing_stats(events)

        cg = stats["ANALYZE:causal_grounding"]
        assert "inst1" in cg["instances"]
        assert "inst2" in cg["instances"]
        assert "inst3" in cg["instances"]

    def test_get_top_failures(self):
        """Top failures should be sorted by count descending."""
        events = self._make_events()
        stats = compute_routing_stats(events)

        top = get_top_failures(stats, top_n=2)
        assert len(top) == 2
        assert top[0][0] == "ANALYZE:causal_grounding"  # count=3
        # Second could be either of the count=1 entries

    def test_get_best_next_phase(self):
        """Best next phase should be the one with highest resolution rate."""
        events = self._make_events()
        stats = compute_routing_stats(events)

        cg = stats["ANALYZE:causal_grounding"]
        best, rate = get_best_next_phase(cg)
        # ANALYZE has resolution_rate 0.5 (1/2), EXECUTE has 0.0 (0/1)
        assert best == "ANALYZE"
        assert rate == pytest.approx(0.5, abs=0.01)

    def test_empty_events(self):
        """Empty event list should produce empty stats."""
        stats = compute_routing_stats([])
        assert stats == {}


# ── Tests: suggest_routing ────────────────────────────────────────────────


class TestSuggestRouting:

    def _make_events(self) -> list[FailureEvent]:
        """Create events for routing suggestion tests."""
        events = []
        # 5 events for ANALYZE:causal_grounding (meets min_samples=3)
        for i in range(5):
            events.append(FailureEvent(
                f"inst{i}", "ANALYZE", "principal_gate", "causal_grounding",
                "missing_required", "resolved" if i < 2 else "unresolved",
                "ANALYZE", 1,
            ))
        # 2 events for EXECUTE:action_grounding (below min_samples=3)
        for i in range(2):
            events.append(FailureEvent(
                f"inst{i}", "EXECUTE", "principal_gate", "action_grounding",
                "missing_required", "unresolved", "ANALYZE", 1,
            ))
        return events

    def test_matrix_format(self):
        """Matrix entries should have required fields."""
        events = self._make_events()
        matrix = suggest_routing_from_events(events, min_samples=3)

        for key, entry in matrix.items():
            assert "next_phase" in entry
            assert "strategy" in entry
            assert "confidence" in entry
            assert "sample_count" in entry

    def test_matrix_has_top_patterns(self):
        """Matrix should include the top failure patterns."""
        events = self._make_events()
        matrix = suggest_routing_from_events(events)

        assert "ANALYZE:causal_grounding" in matrix

    def test_strategy_selection(self):
        """Known (phase, principal) pairs should get specific strategies."""
        events = self._make_events()
        matrix = suggest_routing_from_events(events)

        cg = matrix["ANALYZE:causal_grounding"]
        assert cg["strategy"] == "complete_causal_chain"

    def test_low_sample_confidence_cap(self):
        """Below min_samples, confidence should be capped at 0.5."""
        events = self._make_events()
        matrix = suggest_routing_from_events(events, min_samples=3)

        if "EXECUTE:action_grounding" in matrix:
            ag = matrix["EXECUTE:action_grounding"]
            assert ag["confidence"] <= 0.5
            assert ag["sample_count"] == 2

    def test_empty_events(self):
        """Empty events should produce empty matrix."""
        matrix = suggest_routing_from_events([])
        assert matrix == {}

    def test_sample_count(self):
        """Sample count should match event count for the pattern."""
        events = self._make_events()
        matrix = suggest_routing_from_events(events)

        cg = matrix["ANALYZE:causal_grounding"]
        assert cg["sample_count"] == 5
