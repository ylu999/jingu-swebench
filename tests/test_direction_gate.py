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

from jingu_agent import check_direction_change, build_recovery_escalation_prompt


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


# ── Recovery escalation prompt ──────────────────────────────────────────


class TestRecoveryEscalationPrompt:
    """Test build_recovery_escalation_prompt produces structured search guidance."""

    def test_contains_banned_files(self):
        """Prompt should list the banned files."""
        prompt = build_recovery_escalation_prompt(
            banned_files={"django/utils/dateparse.py", "django/db/models/query.py"},
            violation_count=2,
        )
        assert "django/db/models/query.py" in prompt
        assert "django/utils/dateparse.py" in prompt

    def test_contains_violation_count(self):
        """Prompt should reference the violation count."""
        prompt = build_recovery_escalation_prompt(
            banned_files={"a.py"},
            violation_count=3,
        )
        assert "3" in prompt
        assert "violation" in prompt.lower()

    def test_contains_direction_search_protocol(self):
        """Prompt should contain the structured hypothesis requirement."""
        prompt = build_recovery_escalation_prompt(
            banned_files={"a.py"},
            violation_count=1,
        )
        assert "alternative hypotheses" in prompt.lower() or "2 alternative" in prompt
        assert "candidate files" in prompt.lower()
        assert "evidence" in prompt.lower()

    def test_non_empty(self):
        """Prompt should never be empty."""
        prompt = build_recovery_escalation_prompt(
            banned_files=set(),
            violation_count=0,
        )
        assert len(prompt.strip()) > 0


# ── Enhanced wrong_direction repair prompt ──────────────────────────────


class TestEnhancedWrongDirectionPrompt:
    """Test that the wrong_direction repair prompt includes direction search protocol."""

    def test_prompt_contains_hypothesis_requirement(self):
        """Wrong direction prompt should require ≥2 hypotheses."""
        from repair_prompts import build_repair_prompt
        prompt = build_repair_prompt(
            failure_type="wrong_direction",
            cv_result={"f2p_passed": 0, "f2p_failed": 2},
            routing={"next_phase": "ANALYZE", "repair_goal": "Change direction", "required_principals": []},
            patch_context={"files_written": ["django/utils/dateparse.py"], "patch_summary": {}},
        )
        assert "2 ALTERNATIVE HYPOTHESES" in prompt.upper() or "AT LEAST 2" in prompt.upper()

    def test_prompt_contains_banned_files(self):
        """Wrong direction prompt should list banned files."""
        from repair_prompts import build_repair_prompt
        prompt = build_repair_prompt(
            failure_type="wrong_direction",
            cv_result={"f2p_passed": 0, "f2p_failed": 1},
            routing={"next_phase": "ANALYZE", "repair_goal": "Change direction", "required_principals": []},
            patch_context={"files_written": ["django/db/models/deletion.py"], "patch_summary": {}},
        )
        assert "BANNED" in prompt.upper()
        assert "django/db/models/deletion.py" in prompt

    def test_prompt_contains_direction_search_steps(self):
        """Prompt should contain the 3 steps: reject, generate, select."""
        from repair_prompts import build_repair_prompt
        prompt = build_repair_prompt(
            failure_type="wrong_direction",
            cv_result={"f2p_passed": 0, "f2p_failed": 1},
            routing={"next_phase": "ANALYZE", "repair_goal": "Change direction", "required_principals": []},
        )
        assert "REJECT PREVIOUS HYPOTHESIS" in prompt.upper() or "STEP 1" in prompt
        assert "SELECT" in prompt.upper()

    def test_incomplete_fix_prompt_unchanged(self):
        """incomplete_fix prompt should NOT contain direction search protocol."""
        from repair_prompts import build_repair_prompt
        prompt = build_repair_prompt(
            failure_type="incomplete_fix",
            cv_result={"f2p_passed": 3, "f2p_failed": 1},
            routing={"next_phase": "DESIGN", "repair_goal": "Extend fix", "required_principals": []},
        )
        assert "DIRECTION SEARCH" not in prompt.upper()
        assert "ALTERNATIVE HYPOTHESES" not in prompt.upper()


# ── WDRG v0.2: validate_direction_search_record ──────────────────────────


class TestValidateDirectionSearchRecord:
    """Test the hard validation rules for direction-search hypothesis records."""

    def _good_record(self, banned=None):
        if banned is None:
            banned = {"django/utils/dateparse.py"}
        return {
            "why_not_previous": "The dateparse.py fix was wrong because the issue is actually in the duration parser",
            "alternative_hypotheses": [
                {
                    "root_cause": "Duration parsing doesn't handle ISO 8601",
                    "candidate_files": ["django/utils/duration.py"],
                    "evidence": "The test creates a duration string in ISO format",
                },
                {
                    "root_cause": "Timezone handling in timeparse",
                    "candidate_files": ["django/utils/timeparse.py"],
                    "evidence": "The timezone offset is parsed incorrectly",
                },
            ],
            "chosen_hypothesis_index": 0,
            "chosen_reason": "Duration hypothesis is more likely because the test explicitly creates an ISO duration",
        }

    def test_good_record_admitted(self):
        """A well-formed record should be admitted."""
        from jingu_agent import validate_direction_search_record
        result = validate_direction_search_record(
            self._good_record(), {"django/utils/dateparse.py"},
        )
        assert result["admitted"] is True
        assert result["failures"] == []

    def test_too_few_hypotheses_rejected(self):
        """Record with < 2 hypotheses should be rejected."""
        from jingu_agent import validate_direction_search_record
        record = self._good_record()
        record["alternative_hypotheses"] = record["alternative_hypotheses"][:1]
        result = validate_direction_search_record(record, {"a.py"})
        assert result["admitted"] is False
        assert any(">= 2" in f for f in result["failures"])

    def test_empty_hypotheses_rejected(self):
        """Record with empty hypotheses list should be rejected."""
        from jingu_agent import validate_direction_search_record
        record = self._good_record()
        record["alternative_hypotheses"] = []
        result = validate_direction_search_record(record, {"a.py"})
        assert result["admitted"] is False

    def test_missing_why_not_previous_rejected(self):
        """Record without why_not_previous should be rejected."""
        from jingu_agent import validate_direction_search_record
        record = self._good_record()
        record["why_not_previous"] = ""
        result = validate_direction_search_record(record, {"a.py"})
        assert result["admitted"] is False
        assert any("why_not_previous" in f for f in result["failures"])

    def test_chosen_points_to_banned_file_rejected(self):
        """Chosen hypothesis pointing to banned file should be rejected."""
        from jingu_agent import validate_direction_search_record
        banned = {"django/utils/dateparse.py"}
        record = self._good_record(banned)
        record["alternative_hypotheses"][0]["candidate_files"] = ["django/utils/dateparse.py"]
        record["chosen_hypothesis_index"] = 0
        result = validate_direction_search_record(record, banned)
        assert result["admitted"] is False
        assert any("banned" in f.lower() for f in result["failures"])

    def test_non_chosen_banned_is_ok(self):
        """Non-chosen hypothesis pointing to banned file is OK (only chosen matters)."""
        from jingu_agent import validate_direction_search_record
        banned = {"django/utils/dateparse.py"}
        record = self._good_record(banned)
        # Hypothesis 1 (not chosen) points to banned file — should be OK
        record["alternative_hypotheses"][1]["candidate_files"] = ["django/utils/dateparse.py"]
        record["chosen_hypothesis_index"] = 0  # choosing hypothesis 0 which is fine
        result = validate_direction_search_record(record, banned)
        assert result["admitted"] is True

    def test_missing_chosen_index_rejected(self):
        """Record without chosen_hypothesis_index should be rejected."""
        from jingu_agent import validate_direction_search_record
        record = self._good_record()
        del record["chosen_hypothesis_index"]
        result = validate_direction_search_record(record, {"a.py"})
        assert result["admitted"] is False
        assert any("chosen_hypothesis_index" in f for f in result["failures"])

    def test_missing_chosen_reason_rejected(self):
        """Record without chosen_reason should be rejected."""
        from jingu_agent import validate_direction_search_record
        record = self._good_record()
        record["chosen_reason"] = ""
        result = validate_direction_search_record(record, {"a.py"})
        assert result["admitted"] is False
        assert any("chosen_reason" in f for f in result["failures"])

    def test_hypothesis_missing_fields_rejected(self):
        """Hypothesis with missing required fields should be rejected."""
        from jingu_agent import validate_direction_search_record
        record = self._good_record()
        record["alternative_hypotheses"][0] = {"root_cause": "something"}  # missing candidate_files, evidence
        result = validate_direction_search_record(record, {"a.py"})
        assert result["admitted"] is False
        assert any("candidate_files" in f for f in result["failures"])
        assert any("evidence" in f for f in result["failures"])

    def test_chosen_index_out_of_range_rejected(self):
        """chosen_hypothesis_index out of range should be rejected."""
        from jingu_agent import validate_direction_search_record
        record = self._good_record()
        record["chosen_hypothesis_index"] = 5
        result = validate_direction_search_record(record, {"a.py"})
        assert result["admitted"] is False
        assert any("out of range" in f for f in result["failures"])


# ── WDRG v0.2: build_pre_write_guard_prompt ──────────────────────────────


class TestPreWriteGuardPrompt:
    """Test the pre-write guard message for v0.2."""

    def test_contains_banned_files(self):
        """Prompt should list banned files."""
        from jingu_agent import build_pre_write_guard_prompt
        prompt = build_pre_write_guard_prompt({"a.py", "b.py"})
        assert "a.py" in prompt
        assert "b.py" in prompt

    def test_contains_write_blocked(self):
        """Prompt should indicate writes are blocked."""
        from jingu_agent import build_pre_write_guard_prompt
        prompt = build_pre_write_guard_prompt({"a.py"})
        assert "WRITE BLOCKED" in prompt or "BLOCKED" in prompt

    def test_contains_rejection_feedback(self):
        """Prompt should include rejection reasons when provided."""
        from jingu_agent import build_pre_write_guard_prompt
        prompt = build_pre_write_guard_prompt(
            {"a.py"}, reject_failures=["too few hypotheses", "missing why_not"],
        )
        assert "too few hypotheses" in prompt
        assert "missing why_not" in prompt

    def test_no_rejection_feedback_when_none(self):
        """Prompt should not have REJECTED section when no failures."""
        from jingu_agent import build_pre_write_guard_prompt
        prompt = build_pre_write_guard_prompt({"a.py"})
        assert "REJECTED" not in prompt


# ── WDRG v0.2: direction search schema ──────────────────────────────────


class TestDirectionSearchSchema:
    """Test the JSON schema for direction-search records."""

    def test_schema_has_required_fields(self):
        """Schema should require the 4 key fields."""
        from jingu_agent import DIRECTION_SEARCH_SCHEMA
        assert "why_not_previous" in DIRECTION_SEARCH_SCHEMA["required"]
        assert "alternative_hypotheses" in DIRECTION_SEARCH_SCHEMA["required"]
        assert "chosen_hypothesis_index" in DIRECTION_SEARCH_SCHEMA["required"]
        assert "chosen_reason" in DIRECTION_SEARCH_SCHEMA["required"]

    def test_schema_hypotheses_no_min_items(self):
        """alternative_hypotheses must NOT have minItems (Bedrock rejects >1)."""
        from jingu_agent import DIRECTION_SEARCH_SCHEMA
        hyp_schema = DIRECTION_SEARCH_SCHEMA["properties"]["alternative_hypotheses"]
        assert "minItems" not in hyp_schema, "Bedrock rejects minItems>1; enforce in validate_direction_search_record instead"


# ── WDRG v0.2: direction search state ──────────────────────────────────


class TestDirectionSearchState:
    """Test the v0.2 state management for direction-search contract."""

    def _make_jingu_agent(self):
        """Create a minimal JinguAgent for testing v0.2 state."""
        from jingu_agent import JinguAgent
        agent = JinguAgent.__new__(JinguAgent)
        agent._file_ban_active = False
        agent._file_ban_files = set()
        agent._file_ban_violations = 0
        agent._file_ban_max_violations = 2
        agent._prev_files_written = set()
        agent._direction_search_required = False
        agent._direction_search_admitted = False
        agent._direction_search_attempts = 0
        agent._direction_search_record = None
        agent._direction_search_last_failures = []
        return agent

    def test_v02_activates_on_wrong_direction(self):
        """Direction search should activate alongside file-ban on wrong_direction."""
        agent = self._make_jingu_agent()
        agent._prev_files_written = {"django/utils/dateparse.py"}
        ft = "wrong_direction"
        if ft.startswith("wrong_direction") and agent._prev_files_written:
            agent._file_ban_active = True
            agent._file_ban_files = set(agent._prev_files_written)
            agent._direction_search_required = True
            agent._direction_search_admitted = False
        assert agent._direction_search_required is True
        assert agent._direction_search_admitted is False
        assert agent._file_ban_active is True

    def test_v02_does_not_activate_on_incomplete_fix(self):
        """Direction search should NOT activate for incomplete_fix."""
        agent = self._make_jingu_agent()
        agent._prev_files_written = {"a.py"}
        ft = "incomplete_fix"
        if ft.startswith("wrong_direction") and agent._prev_files_written:
            agent._direction_search_required = True
        assert agent._direction_search_required is False

    def test_v02_admitted_unblocks_writes(self):
        """After hypothesis admitted, writes should be unblocked (file-ban still active)."""
        agent = self._make_jingu_agent()
        agent._file_ban_active = True
        agent._file_ban_files = {"a.py"}
        agent._direction_search_required = True
        agent._direction_search_admitted = True
        # When admitted, the pre-write guard is bypassed — only file-ban applies
        # Simulate: write to non-banned file should be allowed
        fb_changed = {"b.py"}
        banned_hit = fb_changed & agent._file_ban_files
        assert banned_hit == set()  # b.py not banned, so no violation
