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
    plan: str = ""                      # EXECUTE: PLAN: section (must reference root_cause)

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
        if self.plan:
            d["plan"] = self.plan[:200]
        return d
