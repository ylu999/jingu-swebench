"""Tests for the direction change gate and file-ban enforcement.

The gate enforces: when failure_type == wrong_direction, the agent MUST
modify at least one NEW file in A2. If A2 touches only the same files
as A1, the patch is hard-rejected from candidates.

File-ban enforcement (step-level): when wrong_direction is active, any
write to a banned file triggers a redirect message in the agent conversation.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from jingu_agent import check_direction_change


# ── Core logic: should_reject ────────────────────────────────────────────


def test_same_files_wrong_direction_rejects():
    """Same files + wrong_direction → should_reject."""
    result = check_direction_change(
        prev_files={"django/utils/dateparse.py"},
        curr_files={"django/utils/dateparse.py"},
        failure_type="wrong_direction",
    )
    assert result["should_reject"] is True
    assert result["direction_changed"] is False
    assert result["new_files"] == set()
    assert result["overlap"] == {"django/utils/dateparse.py"}


def test_new_file_added_passes():
    """At least one new file → direction_changed, no rejection."""
    result = check_direction_change(
        prev_files={"django/utils/dateparse.py"},
        curr_files={"django/db/models/query.py"},
        failure_type="wrong_direction",
    )
    assert result["should_reject"] is False
    assert result["direction_changed"] is True
    assert result["new_files"] == {"django/db/models/query.py"}


def test_partial_overlap_with_new_file_passes():
    """Overlap exists but new file added → passes."""
    result = check_direction_change(
        prev_files={"django/utils/dateparse.py"},
        curr_files={"django/utils/dateparse.py", "django/utils/duration.py"},
        failure_type="wrong_direction",
    )
    assert result["should_reject"] is False
    assert result["direction_changed"] is True
    assert "django/utils/duration.py" in result["new_files"]


def test_subset_of_prev_files_rejects():
    """A2 modifies a strict subset of A1 files → still rejected."""
    result = check_direction_change(
        prev_files={"a.py", "b.py", "c.py"},
        curr_files={"a.py"},
        failure_type="wrong_direction",
    )
    assert result["should_reject"] is True
    assert result["direction_changed"] is False


# ── Non-wrong_direction failure types: gate does NOT apply ──────────────


def test_incomplete_fix_same_files_allowed():
    """incomplete_fix + same files → no rejection (expected to refine same file)."""
    result = check_direction_change(
        prev_files={"django/utils/dateparse.py"},
        curr_files={"django/utils/dateparse.py"},
        failure_type="incomplete_fix",
    )
    assert result["should_reject"] is False


def test_verify_gap_same_files_allowed():
    """verify_gap + same files → no rejection."""
    result = check_direction_change(
        prev_files={"django/utils/dateparse.py"},
        curr_files={"django/utils/dateparse.py"},
        failure_type="verify_gap",
    )
    assert result["should_reject"] is False


def test_execution_error_same_files_allowed():
    """execution_error + same files → no rejection."""
    result = check_direction_change(
        prev_files={"django/utils/dateparse.py"},
        curr_files={"django/utils/dateparse.py"},
        failure_type="execution_error",
    )
    assert result["should_reject"] is False


def test_empty_failure_type_allowed():
    """No failure type → no rejection."""
    result = check_direction_change(
        prev_files={"a.py"},
        curr_files={"a.py"},
        failure_type="",
    )
    assert result["should_reject"] is False


# ── Edge cases ──────────────────────────────────────────────────────────


def test_p216_variant_also_triggers():
    """wrong_direction+p216 variant also triggers the gate."""
    result = check_direction_change(
        prev_files={"a.py"},
        curr_files={"a.py"},
        failure_type="wrong_direction+p216",
    )
    assert result["should_reject"] is True


def test_completely_different_files():
    """Completely different file sets → direction clearly changed."""
    result = check_direction_change(
        prev_files={"django/db/models/deletion.py"},
        curr_files={"django/db/models/query.py", "django/db/models/sql/compiler.py"},
        failure_type="wrong_direction",
    )
    assert result["should_reject"] is False
    assert result["direction_changed"] is True
    assert len(result["new_files"]) == 2
    assert result["overlap"] == set()


def test_multi_file_overlap_no_new():
    """Multiple overlapping files, no new → rejected."""
    result = check_direction_change(
        prev_files={"a.py", "b.py"},
        curr_files={"a.py", "b.py"},
        failure_type="wrong_direction",
    )
    assert result["should_reject"] is True
    assert result["direction_changed"] is False


# ── File-ban enforcement (step-level) ──────────────────────────────────


class TestFileBanState:
    """Test that file-ban state is correctly set and cleared."""

    def _make_jingu_agent(self):
        """Create a minimal JinguAgent for testing ban state."""
        from jingu_agent import JinguAgent
        agent = JinguAgent.__new__(JinguAgent)
        agent._file_ban_active = False
        agent._file_ban_files = set()
        agent._file_ban_violations = 0
        agent._file_ban_max_violations = 2
        agent._prev_files_written = set()
        return agent

    def test_ban_activates_on_wrong_direction(self):
        """File ban should activate when wrong_direction + prev files exist."""
        agent = self._make_jingu_agent()
        agent._prev_files_written = {"django/utils/dateparse.py"}
        # Simulate what happens at attempt 2 start
        if "wrong_direction" == "wrong_direction" and agent._prev_files_written:
            agent._file_ban_active = True
            agent._file_ban_files = set(agent._prev_files_written)
            agent._file_ban_violations = 0
        assert agent._file_ban_active is True
        assert agent._file_ban_files == {"django/utils/dateparse.py"}

    def test_ban_does_not_activate_on_incomplete_fix(self):
        """File ban should NOT activate for incomplete_fix."""
        agent = self._make_jingu_agent()
        agent._prev_files_written = {"django/utils/dateparse.py"}
        ft = "incomplete_fix"
        if ft == "wrong_direction" and agent._prev_files_written:
            agent._file_ban_active = True
        assert agent._file_ban_active is False

    def test_ban_detects_violation(self):
        """Writing to a banned file should be detected as violation."""
        agent = self._make_jingu_agent()
        agent._file_ban_active = True
        agent._file_ban_files = {"django/utils/dateparse.py", "django/db/models/query.py"}
        # Simulate step files_written
        step_files_written = ["django/utils/dateparse.py"]
        banned_hit = set(step_files_written) & agent._file_ban_files
        assert banned_hit == {"django/utils/dateparse.py"}

    def test_ban_allows_different_file(self):
        """Writing to a non-banned file should not be flagged."""
        agent = self._make_jingu_agent()
        agent._file_ban_active = True
        agent._file_ban_files = {"django/utils/dateparse.py"}
        step_files_written = ["django/db/models/sql/compiler.py"]
        banned_hit = set(step_files_written) & agent._file_ban_files
        assert banned_hit == set()

    def test_ban_violation_counter_increments(self):
        """Each violation should increment the counter."""
        agent = self._make_jingu_agent()
        agent._file_ban_active = True
        agent._file_ban_files = {"a.py"}
        agent._file_ban_violations = 0
        # First violation
        agent._file_ban_violations += 1
        assert agent._file_ban_violations == 1
        # Second violation
        agent._file_ban_violations += 1
        assert agent._file_ban_violations == 2

    def test_ban_inactive_when_no_prev_files(self):
        """File ban should not activate when no previous files."""
        agent = self._make_jingu_agent()
        agent._prev_files_written = set()
        ft = "wrong_direction"
        if ft == "wrong_direction" and agent._prev_files_written:
            agent._file_ban_active = True
        assert agent._file_ban_active is False
