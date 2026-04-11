"""
gate_rejection.py -- Self-Describing Gate (SDG) types for p217.

Structured gate rejection with full contract visibility and field-level
failure detail. Replaces vague "analysis needs improvement" with
actionable repair instructions.

GateRejection carries:
  - contract: what the gate expected (required_fields + field_specs)
  - failures: what specifically failed (per-field, with hints)
  - extracted: what the system actually saw in the agent's output

Consumer: retry_controller.py, repair_prompts.py, run_with_jingu_gate.py
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone


# Feature flag: toggle SDG feedback on/off
SDG_ENABLED = os.environ.get("SDG_ENABLED", "true").lower() == "true"


@dataclass
class FieldSpec:
    """Specification for a single field in a gate contract."""
    description: str
    required: bool
    min_length: int | None = None
    semantic_check: str | None = None


@dataclass
class ContractView:
    """What the gate expects -- the full contract for a phase/subtype."""
    required_fields: list[str]
    field_specs: dict[str, FieldSpec] = field(default_factory=dict)


@dataclass
class FieldFailure:
    """One specific field-level failure with targeted repair hint.

    reason values:
      "missing"              -- required field not present
      "too_short"            -- field present but below min_length
      "semantic_fail"        -- field present but fails semantic check
      "format_invalid"       -- field present but wrong format
      "principal_violation"  -- principal requirement not met
    """
    field: str
    reason: str
    hint: str
    expected: str
    actual: str | None = None


@dataclass
class GateRejection:
    """Structured rejection from a gate with full contract + field failures.

    Every rejection carries enough information for targeted repair:
      - contract: what was expected
      - failures: what specifically failed (with hints)
      - extracted: what the system actually saw
    """
    gate_name: str
    contract: ContractView
    failures: list[FieldFailure]
    extracted: dict = field(default_factory=dict)
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


def build_gate_rejection(
    gate_name: str,
    contract: ContractView,
    extracted: dict,
    failures: list[FieldFailure],
) -> GateRejection:
    """Build a GateRejection from contract + evaluation results.

    Convenience constructor that ensures all fields are populated.
    """
    return GateRejection(
        gate_name=gate_name,
        contract=contract,
        failures=failures,
        extracted=extracted,
    )


def build_repair_from_rejection(rejection: GateRejection) -> str:
    """Convert a GateRejection to an agent-readable repair prompt.

    Output format:
      [GATE REJECT: <gate_name>]
      Contract requires: <required_fields>

      Failures:
      - Field '<field>': <reason> -- <hint>. Expected: <expected>. Got: <actual>.

      Extracted values:
      - <key>: <value_preview>
    """
    parts = []

    # Header
    parts.append(f"[GATE REJECT: {rejection.gate_name}]")

    # Contract summary
    if rejection.contract.required_fields:
        parts.append(
            f"Contract requires: {', '.join(rejection.contract.required_fields)}"
        )

    # Field-level failures
    if rejection.failures:
        failure_lines = []
        for f in rejection.failures:
            line = f"- Field '{f.field}': {f.reason} -- {f.hint}"
            if f.expected:
                line += f". Expected: {f.expected}"
            if f.actual is not None:
                actual_preview = f.actual[:80] if len(f.actual) > 80 else f.actual
                line += f". Got: {actual_preview}"
            failure_lines.append(line)
        parts.append("Failures:\n" + "\n".join(failure_lines))

    # Extracted values summary (what the system saw)
    if rejection.extracted:
        extracted_lines = []
        for k, v in rejection.extracted.items():
            val_str = str(v)
            if len(val_str) > 80:
                val_str = val_str[:80] + "..."
            extracted_lines.append(f"- {k}: {val_str}")
        parts.append("Extracted values:\n" + "\n".join(extracted_lines))

    return "\n\n".join(parts)
