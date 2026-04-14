"""
phase_record.py — PhaseRecord dataclass for per-phase structured output.

Each phase (OBSERVE / ANALYZE / EXECUTE / JUDGE) produces one PhaseRecord
when the control-plane emits a VerdictAdvance.

Events are system-generated facts derived from observed agent output.
Fields are parsed deterministically — no LLM call required.
"""

from dataclasses import dataclass, field


@dataclass
class PhaseRecord:
    """Structured record of what the agent produced in one reasoning phase.

    Collected at phase ADVANCE time (VerdictAdvance) from the last agent message.
    Stored per-attempt in StepMonitorState.phase_records.
    Written into jingu_body["phase_records"] at attempt end.
    """

    phase: str                          # "OBSERVE" / "ANALYZE" / "EXECUTE" / "JUDGE"
    subtype: str                        # e.g. "observation", "root_cause_analysis", "patch_writing", "verification"
    principals: list[str]               # principal atoms declared for this phase
    claims: list[str]                   # explicit claims the agent made (initial version: [])
    evidence_refs: list[str]            # file:line or test name references found in agent output
    from_steps: list[int]               # step indices this record was derived from (for gate provenance)
    content: str                        # raw agent output for this phase (truncated to 500 chars)
    # Structured output fields (p23: causal binding)
    root_cause: str = ""                # ANALYZE: ROOT_CAUSE: section (required for analysis.root_cause)
    causal_chain: str = ""              # ANALYZE: CAUSAL_CHAIN: section
    invariant_capture: dict = field(default_factory=dict)  # ANALYZE: structured invariant capture
    plan: str = ""                      # EXECUTE: PLAN: section (must reference root_cause)
    # DECIDE: prediction fields (decision quality upgrade)
    testable_hypothesis: str = ""           # DECIDE: "If X then Y because Z"
    expected_tests_to_pass: list[str] = field(default_factory=list)  # DECIDE: predicted passing tests
    expected_files_to_change: list[str] = field(default_factory=list)  # DECIDE: predicted changed files
    risk_level: str = ""                    # DECIDE: "low"/"medium"/"high"
    # OBSERVE
    observations: list[str] = field(default_factory=list)
    # ANALYZE
    alternative_hypotheses: list[dict] = field(default_factory=list)  # [{hypothesis, ruled_out_reason}]
    # DECIDE
    options: list[dict] = field(default_factory=list)  # [{name, pros: str[], cons: str[]}]
    chosen: str = ""
    rationale: str = ""
    # DESIGN
    files_to_modify: list[str] = field(default_factory=list)
    scope_boundary: str = ""
    invariants: list[str] = field(default_factory=list)
    design_comparison: dict = field(default_factory=dict)  # {options, chosen, reason}
    # EXECUTE
    patch_description: str = ""
    files_modified: list[str] = field(default_factory=list)
    # JUDGE
    test_results: dict = field(default_factory=dict)  # {passed: bool, details?: str}
    success_criteria_met: list[dict] = field(default_factory=list)  # [{criterion, met, evidence?}]
    residual_risks: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        """Serialise to a plain dict for JSON output."""
        d = {
            "phase": self.phase,
            "subtype": self.subtype,
            "principals": self.principals,
            "claims": self.claims,
            "evidence_refs": self.evidence_refs,
            "from_steps": self.from_steps,
            "content_preview": self.content[:100],
        }
        if self.root_cause:
            d["root_cause"] = self.root_cause[:200]
        if self.invariant_capture:
            d["invariant_capture"] = self.invariant_capture
        if self.plan:
            d["plan"] = self.plan[:200]
        if self.testable_hypothesis:
            d["testable_hypothesis"] = self.testable_hypothesis
        if self.expected_tests_to_pass:
            d["expected_tests_to_pass"] = self.expected_tests_to_pass[:5]
        if self.expected_files_to_change:
            d["expected_files_to_change"] = self.expected_files_to_change
        if self.risk_level:
            d["risk_level"] = self.risk_level
        # OBSERVE
        if self.observations:
            d["observations"] = self.observations
        # ANALYZE
        if self.alternative_hypotheses:
            d["alternative_hypotheses"] = self.alternative_hypotheses
        # DECIDE
        if self.options:
            d["options"] = self.options
        if self.chosen:
            d["chosen"] = self.chosen
        if self.rationale:
            d["rationale"] = self.rationale
        # DESIGN
        if self.files_to_modify:
            d["files_to_modify"] = self.files_to_modify
        if self.scope_boundary:
            d["scope_boundary"] = self.scope_boundary
        if self.invariants:
            d["invariants"] = self.invariants
        if self.design_comparison:
            d["design_comparison"] = self.design_comparison
        # EXECUTE
        if self.patch_description:
            d["patch_description"] = self.patch_description
        if self.files_modified:
            d["files_modified"] = self.files_modified
        # JUDGE
        if self.test_results:
            d["test_results"] = self.test_results
        if self.success_criteria_met:
            d["success_criteria_met"] = self.success_criteria_met
        if self.residual_risks:
            d["residual_risks"] = self.residual_risks
        return d
