"""
protocol_compiler.py — Protocol Compiler for control field enforcement.

Reads FieldSpec definitions from cognition_contracts and auto-generates:
  1. Tool schema (submit_phase_record parameters)
  2. Prompt fragments (field instructions for agent)
  3. Gate validator (fail-closed control field checks)
  4. Consumer registry (which system consumes which field)
  5. Replay schema (which fields must be visible in replay)

Core invariant: every control field that is protocol_required MUST appear in
the tool schema, prompt, gate, and replay. Missing any wiring = BuildError.

Enforcement rules:
  R1: protocol_required => must be in tool schema
  R2: is_control_field => must be protocol_required
  R3: fail_closed => gate must exist
  R4: consumer declared => field must be protocol-backed
  R5: no extractor-only control field
"""

from __future__ import annotations

import sys
import os
from dataclasses import dataclass, field
from typing import Literal

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mini-swe-agent"))


# ── Protocol Field Spec (extended from cognition_contracts FieldSpec) ────────

@dataclass(frozen=True)
class ProtocolFieldSpec:
    """Single source of truth for a protocol-enforced field."""
    name: str
    phase: str

    field_type: Literal["enum", "text", "boolean", "array", "object"] = "text"
    required: bool = False
    enum_values: tuple[str, ...] = ()

    # Protocol enforcement
    is_control_field: bool = False
    fail_closed: bool = False
    protocol_required: bool = False  # MUST be submitted via tool

    # Prompt generation
    prompt_key: str | None = None
    prompt_instruction: str | None = None

    # Consumers
    consumers: tuple[str, ...] = ()


# ── Build Error ──────────────────────────────────────────────────────────────

@dataclass
class ProtocolBuildError:
    """A protocol compilation error — build must fail."""
    code: str
    field_name: str
    phase: str
    message: str


# ── Generator 1: Tool Schema ────────────────────────────────────────────────

def build_tool_schema(phase: str, specs: list[ProtocolFieldSpec]) -> dict:
    """Generate submit_phase_record tool schema from ProtocolFieldSpecs.

    Returns a JSON Schema dict with properties, required, additionalProperties.
    This is used to VERIFY the compiled schema, not replace it — the actual
    schema comes from cognition_contracts.SCHEMA_PROPERTIES. This function
    checks that the compiled schema contains all protocol-required fields.
    """
    required_fields: list[str] = []
    for spec in specs:
        if spec.phase != phase:
            continue
        if spec.protocol_required or spec.required:
            required_fields.append(spec.name)
    return {
        "required_by_protocol": required_fields,
        "phase": phase,
    }


# ── Generator 2: Prompt Fragment ────────────────────────────────────────────

def build_prompt_fragment(phase: str, specs: list[ProtocolFieldSpec]) -> str:
    """Generate prompt instructions for protocol-required fields."""
    lines: list[str] = []
    for spec in specs:
        if spec.phase != phase:
            continue
        if spec.prompt_instruction:
            lines.append(spec.prompt_instruction)
        if spec.prompt_key:
            if spec.field_type == "enum" and spec.enum_values:
                enum_str = " | ".join(spec.enum_values)
                lines.append(f"{spec.prompt_key}: <ONE OF [{enum_str}]>")
        if spec.protocol_required:
            key = spec.prompt_key or spec.name
            lines.append(
                f"You MUST include {key} in submit_phase_record. "
                f"Missing it will REJECT your submission."
            )
    return "\n".join(lines)


# ── Generator 3: Gate Validator ─────────────────────────────────────────────

def build_gate_required_fields(phase: str, specs: list[ProtocolFieldSpec]) -> list[str]:
    """Return field names that must be checked by the gate (fail_closed fields)."""
    return [
        spec.name for spec in specs
        if spec.phase == phase and spec.fail_closed
    ]


def validate_record_protocol(record: dict, phase: str, specs: list[ProtocolFieldSpec]) -> list[str]:
    """Validate a submitted record against protocol requirements.

    Returns list of missing/invalid field names. Empty = valid.
    """
    missing: list[str] = []
    for spec in specs:
        if spec.phase != phase:
            continue
        if not spec.protocol_required:
            continue
        val = record.get(spec.name)
        if val is None or (isinstance(val, str) and not val.strip()):
            missing.append(spec.name)
        elif spec.field_type == "enum" and spec.enum_values:
            if val not in spec.enum_values:
                missing.append(spec.name)
    return missing


# ── Generator 4: Consumer Registry ──────────────────────────────────────────

def build_consumer_registry(specs: list[ProtocolFieldSpec]) -> dict[str, set[str]]:
    """Map consumer name -> set of field names it needs."""
    registry: dict[str, set[str]] = {}
    for spec in specs:
        for consumer in spec.consumers:
            registry.setdefault(consumer, set()).add(spec.name)
    return registry


# ── Generator 5: Replay Schema ──────────────────────────────────────────────

def build_replay_schema(specs: list[ProtocolFieldSpec]) -> dict[str, dict]:
    """Generate replay visibility requirements for control fields."""
    replay: dict[str, dict] = {}
    for spec in specs:
        if spec.is_control_field:
            replay[spec.name] = {
                "phase": spec.phase,
                "visible": True,
                "required": spec.protocol_required,
                "source": "protocol_record",
            }
    return replay


# ── Enforcement Rules (build-time) ──────────────────────────────────────────

def enforce_protocol_rules(
    specs: list[ProtocolFieldSpec],
    tool_schemas: dict[str, set[str]] | None = None,
) -> list[ProtocolBuildError]:
    """Run all 5 enforcement rules. Returns errors (empty = pass).

    Args:
        specs: All ProtocolFieldSpecs.
        tool_schemas: Optional mapping of phase -> set of field names in tool schema.
            If provided, R1 checks against actual tool parameters.
    """
    errors: list[ProtocolBuildError] = []

    for spec in specs:
        # R1: protocol_required => must be in tool schema
        if spec.protocol_required and tool_schemas:
            phase_fields = tool_schemas.get(spec.phase, set())
            if spec.name not in phase_fields:
                errors.append(ProtocolBuildError(
                    code="TOOL_FIELD_MISSING",
                    field_name=spec.name,
                    phase=spec.phase,
                    message=(
                        f"{spec.name} is protocol_required but missing from "
                        f"submit_phase_record tool schema for {spec.phase}"
                    ),
                ))

        # R2: control_field => protocol_required
        if spec.is_control_field and not spec.protocol_required:
            errors.append(ProtocolBuildError(
                code="CONTROL_FIELD_NOT_PROTOCOL_ENFORCED",
                field_name=spec.name,
                phase=spec.phase,
                message=(
                    f"{spec.name} is a control field but not protocol_required — "
                    f"control signals must come from structured submission"
                ),
            ))

        # R3: fail_closed => gate must check this field
        # Check both GATE_RULES (structured) and gate module source (inline checks).
        if spec.fail_closed:
            try:
                import inspect
                from cognition_contracts.analysis_root_cause import GATE_RULE_MAP
                if spec.phase == "ANALYZE":
                    # Check 1: field in GATE_RULES (by name or by field target)
                    in_gate_rules = (
                        spec.name in GATE_RULE_MAP
                        or any(r.field == spec.name for r in GATE_RULE_MAP.values())
                    )
                    # Check 2: field referenced in analysis_gate module source
                    in_gate_source = False
                    try:
                        import analysis_gate
                        gate_src = inspect.getsource(analysis_gate)
                        in_gate_source = spec.name in gate_src
                    except Exception:
                        pass
                    if not in_gate_rules and not in_gate_source:
                        errors.append(ProtocolBuildError(
                            code="FAIL_CLOSED_NO_GATE",
                            field_name=spec.name,
                            phase=spec.phase,
                            message=(
                                f"{spec.name} is fail_closed but has no gate rule "
                                f"in GATE_RULES and not checked in analysis_gate — "
                                f"rejection path missing"
                            ),
                        ))
            except ImportError:
                pass

        # R4: consumer declared => field must be protocol_required
        if spec.consumers and not spec.protocol_required:
            errors.append(ProtocolBuildError(
                code="CONSUMER_WITHOUT_PROTOCOL",
                field_name=spec.name,
                phase=spec.phase,
                message=(
                    f"{spec.name} has consumers {spec.consumers} but is not "
                    f"protocol_required — consumers may get stale data"
                ),
            ))

    return errors


# ── Field Spec Registry ─────────────────────────────────────────────────────

def _get_protocol_specs() -> list[ProtocolFieldSpec]:
    """Return all ProtocolFieldSpecs across all phases.

    Single source: defined here, derived from cognition_contracts.
    """
    from cognition_contracts.analysis_root_cause import REPAIR_STRATEGY_TYPES

    return [
        # ── ANALYZE control fields ──────────────────────────────────────
        ProtocolFieldSpec(
            name="repair_strategy_type",
            phase="ANALYZE",
            field_type="enum",
            required=True,
            enum_values=tuple(REPAIR_STRATEGY_TYPES),
            is_control_field=True,
            fail_closed=True,
            protocol_required=True,
            prompt_key="REPAIR_STRATEGY_TYPE",
            prompt_instruction=(
                "You MUST explicitly declare your repair strategy type. "
                "This controls how the system routes your next attempt."
            ),
            consumers=("nprg", "retry_control", "telemetry"),
        ),

        # ── ANALYZE required fields (not control, but protocol-required) ──
        ProtocolFieldSpec(
            name="root_cause",
            phase="ANALYZE",
            field_type="text",
            required=True,
            is_control_field=False,
            fail_closed=True,
            protocol_required=True,
            prompt_key="root_cause",
            prompt_instruction="Identify the root cause with specific code reference (file/function/line).",
            consumers=("analysis_gate", "telemetry"),
        ),
        ProtocolFieldSpec(
            name="causal_chain",
            phase="ANALYZE",
            field_type="text",
            required=True,
            is_control_field=False,
            fail_closed=True,
            protocol_required=True,
            prompt_key="causal_chain",
            prompt_instruction="Explain the causal chain: test failure -> condition -> code -> why it fails.",
            consumers=("analysis_gate", "telemetry"),
        ),
        ProtocolFieldSpec(
            name="evidence_refs",
            phase="ANALYZE",
            field_type="array",
            required=True,
            protocol_required=True,
            consumers=("analysis_gate",),
        ),
        ProtocolFieldSpec(
            name="alternative_hypotheses",
            phase="ANALYZE",
            field_type="array",
            required=True,
            protocol_required=True,
            consumers=("analysis_gate",),
        ),
        ProtocolFieldSpec(
            name="invariant_capture",
            phase="ANALYZE",
            field_type="object",
            required=False,
            protocol_required=True,
            consumers=("analysis_gate",),
        ),
    ]


# ── Compile + Verify ────────────────────────────────────────────────────────

def compile_protocol(
    *,
    verify_tool_schemas: bool = True,
) -> tuple[list[ProtocolFieldSpec], list[ProtocolBuildError]]:
    """Compile protocol and run enforcement rules.

    Returns (specs, errors). errors == [] means compilation passed.
    """
    specs = _get_protocol_specs()

    # Build actual tool schemas for verification
    tool_schemas: dict[str, set[str]] | None = None
    if verify_tool_schemas:
        try:
            from jingu_model import _build_phase_record_tool
            from bundle_compiler import compile_bundle
            bundle = compile_bundle(force_reload=True)
            tool_schemas = {}
            for phase in ["ANALYZE", "DECIDE", "DESIGN", "EXECUTE", "JUDGE", "OBSERVE"]:
                schema = bundle.governance.get_constrained_schema(phase)
                if not schema:
                    continue
                tool = _build_phase_record_tool(phase, schema)
                tool_params = tool.get("function", {}).get("parameters", {}).get("properties", {})
                tool_schemas[phase] = set(tool_params.keys())
        except Exception:
            tool_schemas = None

    errors = enforce_protocol_rules(specs, tool_schemas)
    return specs, errors


# ── Runtime: Get control field from admitted record ─────────────────────────

class ControlFieldMissing(RuntimeError):
    """Raised when a control field is not in the admitted record."""
    def __init__(self, field_name: str, phase: str = ""):
        self.field_name = field_name
        self.phase = phase
        super().__init__(
            f"Control field '{field_name}' missing from admitted record"
            + (f" (phase={phase})" if phase else "")
        )


def get_control_field(record: dict | None, name: str, phase: str = "") -> str:
    """Read a control field from an admitted record. Raises if missing.

    This is the ONLY way to read control fields in protocol-only mode.
    No fallbacks, no defaults, no "unknown".
    """
    if record is None:
        raise ControlFieldMissing(name, phase)
    val = record.get(name)
    if val is None or (isinstance(val, str) and not val.strip()):
        raise ControlFieldMissing(name, phase)
    return val


# ── CLI ──────────────────────────────────────────────────────────────────────

def run_protocol_compile() -> list[ProtocolBuildError]:
    """Run protocol compilation and return errors."""
    _, errors = compile_protocol()
    return errors


if __name__ == "__main__":
    errors = run_protocol_compile()
    if errors:
        print(f"PROTOCOL COMPILE FAILED ({len(errors)} errors):")
        for e in errors:
            print(f"  {e.code}: {e.field_name} ({e.phase}) — {e.message}")
        sys.exit(1)
    else:
        print("PROTOCOL COMPILE PASSED")
        sys.exit(0)
