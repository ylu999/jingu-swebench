"""
principal_field_mapping.py — Maps each principal to its proving PhaseRecord fields,
semantic check description, and check mode.

Wave 1: 10 STRUCTURAL principals (schema field presence/value checks)
Wave 3: 4 BEHAVIORAL principals (runtime signals: git diff, tool usage, cross-phase)

EF-1: initial implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class CheckMode(str, Enum):
    STRUCTURAL = "structural"    # Schema field presence/value check
    BEHAVIORAL = "behavioral"    # Runtime signal (git diff, tool usage, cross-phase)
    HYBRID = "hybrid"            # Some checks structural, some behavioral


@dataclass(frozen=True)
class PrincipalFieldMapping:
    """Maps a principal to its proving fields and validation method."""
    principal: str               # e.g. "causal_grounding"
    subtypes: list[str]          # e.g. ["analysis.root_cause"]
    required_fields: list[str]   # PhaseRecord fields that prove this principal
    semantic_check: str          # Description of what validates this principal
    check_mode: CheckMode
    inference_rule_exists: bool
    fake_check_eligible: bool


# ── Wave 1: 10 STRUCTURAL principals ─────────────────────────────────────────

WAVE_1_MAPPINGS: list[PrincipalFieldMapping] = [
    PrincipalFieldMapping(
        principal="causal_grounding",
        subtypes=["analysis.root_cause"],
        required_fields=["root_cause", "evidence_refs", "causal_chain"],
        semantic_check="root_cause non-empty + evidence_refs present + causal_chain explains cause",
        check_mode=CheckMode.STRUCTURAL,
        inference_rule_exists=True,
        fake_check_eligible=True,
    ),
    PrincipalFieldMapping(
        principal="evidence_linkage",
        subtypes=["analysis.root_cause"],
        required_fields=["evidence_refs"],
        semantic_check="evidence_refs contains file:line references linking claims to code",
        check_mode=CheckMode.STRUCTURAL,
        inference_rule_exists=True,
        fake_check_eligible=True,
    ),
    PrincipalFieldMapping(
        principal="alternative_hypothesis_check",
        subtypes=["analysis.root_cause"],
        required_fields=["alternative_hypotheses"],
        semantic_check="alternative_hypotheses lists at least one competing explanation",
        check_mode=CheckMode.STRUCTURAL,
        inference_rule_exists=True,
        fake_check_eligible=True,
    ),
    PrincipalFieldMapping(
        principal="ontology_alignment",
        subtypes=["all"],
        required_fields=["phase", "subtype", "principals"],
        semantic_check="phase maps to known subtype + principals declared from contract",
        check_mode=CheckMode.STRUCTURAL,
        inference_rule_exists=True,
        fake_check_eligible=False,
    ),
    PrincipalFieldMapping(
        principal="option_comparison",
        subtypes=["decision.fix_direction"],
        required_fields=["options", "chosen", "rationale"],
        semantic_check="options has >= 2 entries + chosen references one + rationale explains why",
        check_mode=CheckMode.STRUCTURAL,
        inference_rule_exists=True,
        fake_check_eligible=False,
    ),
    PrincipalFieldMapping(
        principal="result_verification",
        subtypes=["judge.verification"],
        required_fields=["test_results", "success_criteria_met"],
        semantic_check="test_results non-empty with pass/fail indicators + success_criteria_met is boolean",
        check_mode=CheckMode.STRUCTURAL,
        inference_rule_exists=True,
        fake_check_eligible=False,
    ),
    PrincipalFieldMapping(
        principal="evidence_completeness",
        subtypes=["analysis.root_cause"],
        required_fields=["evidence_refs"],
        semantic_check="evidence_refs covers all claims made in root_cause analysis",
        check_mode=CheckMode.STRUCTURAL,
        inference_rule_exists=False,
        fake_check_eligible=False,
    ),
    PrincipalFieldMapping(
        principal="scope_minimality",
        subtypes=["design.solution_shape"],
        required_fields=["scope_boundary", "files_to_modify"],
        semantic_check="scope_boundary defined + files_to_modify is minimal set",
        check_mode=CheckMode.STRUCTURAL,
        inference_rule_exists=False,
        fake_check_eligible=False,
    ),
    PrincipalFieldMapping(
        principal="residual_risk_detection",
        subtypes=["judge.verification"],
        required_fields=["residual_risks"],
        semantic_check="residual_risks lists remaining risks after patch applied",
        check_mode=CheckMode.STRUCTURAL,
        inference_rule_exists=False,
        fake_check_eligible=False,
    ),
    PrincipalFieldMapping(
        principal="invariant_capture",
        subtypes=["design.solution_shape"],
        required_fields=["invariant_capture"],
        semantic_check="invariant_capture states what must not change after the fix",
        check_mode=CheckMode.STRUCTURAL,
        inference_rule_exists=False,
        fake_check_eligible=False,
    ),
]


# ── Wave 3: 4 BEHAVIORAL principals ──────────────────────────────────────────

WAVE_3_MAPPINGS: list[PrincipalFieldMapping] = [
    PrincipalFieldMapping(
        principal="minimal_change",
        subtypes=["execution.code_patch"],
        required_fields=[],
        semantic_check="git diff line count <= threshold (requires runtime git diff)",
        check_mode=CheckMode.BEHAVIORAL,
        inference_rule_exists=True,
        fake_check_eligible=True,
    ),
    PrincipalFieldMapping(
        principal="action_grounding",
        subtypes=["execution.code_patch"],
        required_fields=[],
        semantic_check="PLAN references ROOT_CAUSE from ANALYZE phase (cross-phase linkage)",
        check_mode=CheckMode.BEHAVIORAL,
        inference_rule_exists=True,
        fake_check_eligible=False,
    ),
    PrincipalFieldMapping(
        principal="scope_completeness",
        subtypes=["execution.code_patch", "judge.verification"],
        required_fields=[],
        semantic_check="grep/search for callers before multi-file changes (tool usage history)",
        check_mode=CheckMode.BEHAVIORAL,
        inference_rule_exists=True,
        fake_check_eligible=False,
    ),
    PrincipalFieldMapping(
        principal="no_unnecessary_compat",
        subtypes=["execution.code_patch"],
        required_fields=[],
        semantic_check="patch does not contain backward-compat shims unless issue requires it",
        check_mode=CheckMode.BEHAVIORAL,
        inference_rule_exists=True,
        fake_check_eligible=False,
    ),
]


# ── Combined mapping index ────────────────────────────────────────────────────

ALL_MAPPINGS: list[PrincipalFieldMapping] = WAVE_1_MAPPINGS + WAVE_3_MAPPINGS

MAPPING_BY_PRINCIPAL: dict[str, PrincipalFieldMapping] = {
    m.principal: m for m in ALL_MAPPINGS
}
