"""
onboarding_audit.py — Build-time enforcement of field onboarding completeness.

Rule: "onboarding 不完整 = build fail"
No declared-but-not-wired contract may enter a runnable build.

A field is fully onboarded only when ALL of these hold:
  1. declared (FieldSpec exists)
  2. prompted (prompt builder injects it)
  3. schematized (bundle schema includes it)
  4. validated (gate checks it if fail_closed)
  5. stably produced (hard producer exists if control field)
  6. consumed (consumer wired if declared)

Seven build-fail rules:
  Rule 1: control field must have declared producer
  Rule 2: control field cannot rely solely on free-text extraction
  Rule 3: declared consumers must be wired in consumer registry
  Rule 4: required + fail_closed fields must have gate validation
  Rule 5: required fields need prompt + schema + validation + producer
  Rule 6: control field with consumers must have hard producer
  Rule 7: tool-submitted fields must appear in tool parameters (TOOL_PARAMETER_NOT_WIRED)

Exit code 0 = audit passed. Non-zero = onboarding gap detected.
"""

from __future__ import annotations

import inspect
import sys
import os
from dataclasses import dataclass, field
from typing import Any

# Path setup — same as other scripts
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mini-swe-agent"))


# ── Onboarding FieldSpec (extended from _base.FieldSpec) ─────────────────────

@dataclass
class OnboardingFieldSpec:
    """Extended FieldSpec for onboarding audit. Declares the full wiring contract."""
    name: str
    phase: str
    field_type: str = "string"              # string | enum | array | object
    required: bool = False
    is_control_field: bool = False           # affects routing/retry/gating/admission
    fail_closed: bool = False                # gate rejects if missing
    prompt_enabled: bool = True              # injected into prompt
    schema_enabled: bool = True              # in bundle schema
    producer: str | None = None              # "submit_phase_record" | "structured_extract"
    producer_stability: str = "best_effort"  # "hard" | "soft" | "best_effort"
    consumers: tuple[str, ...] = ()          # ("nprg", "retry_control", "telemetry")
    prompt_key: str | None = None            # e.g. "REPAIR_STRATEGY_TYPE"
    prompt_format: str | None = None         # e.g. "REPAIR_STRATEGY_TYPE: <ENUM>"
    extraction_required: bool = False
    deterministic_fallback_allowed: bool = False
    enum_values: tuple[str, ...] | None = None


# ── AuditError ───────────────────────────────────────────────────────────────

@dataclass
class AuditError:
    """Structured onboarding audit error."""
    code: str
    field_name: str
    phase: str
    message: str


# ── Registries ───────────────────────────────────────────────────────────────
# These are derived from actual system wiring, not from FieldSpec declarations.
# The audit compares declared intent (FieldSpec) against actual wiring (registries).


def _build_prompt_registry() -> dict[str, set[str]]:
    """Derive prompt registry from phase_prompt.py — which fields are actually injected."""
    try:
        from phase_prompt import build_phase_prefix
        from schema_field_guidance import render_schema_field_guidance
        from bundle_compiler import compile_bundle

        bundle = compile_bundle(force_reload=True)
        registry: dict[str, set[str]] = {}

        for phase in ["ANALYZE", "DECIDE", "DESIGN", "EXECUTE", "JUDGE", "OBSERVE"]:
            prefix = build_phase_prefix(phase)
            schema = bundle.governance.get_constrained_schema(phase)
            if not schema:
                continue
            # Extract field names from schema properties
            props = schema.get("properties", {})
            if "json_schema" in schema:
                props = schema["json_schema"]["schema"].get("properties", {})
            elif "schema" in schema and "properties" not in schema:
                props = schema["schema"].get("properties", {})

            prompted_fields = set()
            for field_name in props:
                # Check if field name appears in the prefix (renderer output)
                if field_name in prefix:
                    prompted_fields.add(field_name)
            registry[phase] = prompted_fields

        return registry
    except Exception:
        return {}


def _build_schema_registry() -> dict[str, set[str]]:
    """Derive schema registry from bundle.json — which fields are in the schema."""
    try:
        from bundle_compiler import compile_bundle
        bundle = compile_bundle(force_reload=True)
        registry: dict[str, set[str]] = {}

        for phase in ["ANALYZE", "DECIDE", "DESIGN", "EXECUTE", "JUDGE", "OBSERVE"]:
            schema = bundle.governance.get_constrained_schema(phase)
            if not schema:
                continue
            props = schema.get("properties", {})
            if "json_schema" in schema:
                props = schema["json_schema"]["schema"].get("properties", {})
            elif "schema" in schema and "properties" not in schema:
                props = schema["schema"].get("properties", {})
            registry[phase] = set(props.keys())

        return registry
    except Exception:
        return {}


def _build_gate_registry() -> dict[str, dict[str, set[str]]]:
    """Derive gate registry from gate modules — which fields are fail-closed checked."""
    registry: dict[str, dict[str, set[str]]] = {}

    # ANALYZE gate
    try:
        import analysis_gate
        gate_src = inspect.getsource(analysis_gate)
        fail_closed_fields: set[str] = set()
        # Fields that cause hard gate failure (appear in failed.append logic)
        for field_name in ["root_cause", "causal_chain", "repair_strategy_type",
                           "evidence_refs", "alternative_hypotheses", "invariant_capture"]:
            if field_name in gate_src:
                fail_closed_fields.add(field_name)
        registry["ANALYZE"] = {"fail_closed_fields": fail_closed_fields}
    except Exception:
        pass

    return registry


def _build_consumer_registry() -> dict[str, set[str]]:
    """Derive consumer registry — which consumers use which fields."""
    registry: dict[str, set[str]] = {}

    # NPRG consumer
    try:
        import jingu_agent
        agent_src = inspect.getsource(jingu_agent)
        nprg_fields: set[str] = set()
        if "_prev_strategy_type" in agent_src or "repair_strategy_type" in agent_src:
            nprg_fields.add("repair_strategy_type")
        if nprg_fields:
            registry["nprg"] = nprg_fields
    except Exception:
        pass

    # Retry control consumer
    try:
        from declaration_extractor import build_phase_record_from_structured
        extractor_src = inspect.getsource(build_phase_record_from_structured)
        retry_fields: set[str] = set()
        if "repair_strategy_type" in extractor_src:
            retry_fields.add("repair_strategy_type")
        if retry_fields:
            registry["retry_control"] = retry_fields
    except Exception:
        pass

    # Telemetry consumer (analysis_gate extracted dict)
    try:
        import analysis_gate
        gate_src = inspect.getsource(analysis_gate)
        telemetry_fields: set[str] = set()
        # Check the 'extracted' dict in evaluate_analysis — these fields are emitted as telemetry
        for field_name in ["repair_strategy_type", "root_cause", "causal_chain",
                           "invariant_capture", "evidence_refs", "alternative_hypotheses"]:
            if field_name in gate_src:
                telemetry_fields.add(field_name)
        if telemetry_fields:
            registry["telemetry"] = telemetry_fields
    except Exception:
        pass

    return registry


def _build_tool_parameter_registry() -> dict[str, set[str]]:
    """Derive tool parameter registry — which fields are in submit_phase_record tool.

    This is the critical check: if a field is not in the tool parameters,
    the agent CANNOT submit it via the structured tool path. It will be
    forced to use free-text, which Claude will always prefer (laziness exit).
    """
    registry: dict[str, set[str]] = {}
    try:
        from jingu_model import _build_phase_record_tool
        from bundle_compiler import compile_bundle
        bundle = compile_bundle(force_reload=True)

        for phase in ["ANALYZE", "DECIDE", "DESIGN", "EXECUTE", "JUDGE", "OBSERVE"]:
            schema = bundle.governance.get_constrained_schema(phase)
            if not schema:
                continue
            tool = _build_phase_record_tool(phase, schema)
            tool_params = tool.get("function", {}).get("parameters", {}).get("properties", {})
            registry[phase] = set(tool_params.keys())
    except Exception:
        pass
    return registry


def _build_producer_registry() -> dict[str, dict[str, dict[str, str]]]:
    """Derive producer registry — how each field is produced."""
    registry: dict[str, dict[str, dict[str, str]]] = {}

    # ANALYZE producers
    analyze_producers: dict[str, dict[str, str]] = {}

    # Check submit_phase_record tool (jingu_model._build_phase_record_tool)
    try:
        from jingu_model import _build_phase_record_tool
        from bundle_compiler import compile_bundle
        bundle = compile_bundle(force_reload=True)
        schema = bundle.governance.get_constrained_schema("ANALYZE")
        if schema:
            tool = _build_phase_record_tool("ANALYZE", schema)
            tool_params = tool.get("function", {}).get("parameters", {}).get("properties", {})
            for field_name in tool_params:
                analyze_producers[field_name] = {
                    "producer": "submit_phase_record",
                    "stability": "hard",
                }
    except Exception:
        pass

    # Check structured_extract fallback — only for fields NOT already covered by submit_phase_record
    try:
        from declaration_extractor import build_phase_record_from_structured
        extractor_src = inspect.getsource(build_phase_record_from_structured)
        for field_name in ["root_cause", "causal_chain", "evidence_refs",
                           "alternative_hypotheses", "invariant_capture", "repair_strategy_type"]:
            if field_name in extractor_src:
                if field_name not in analyze_producers:
                    # Only structured_extract, no submit_phase_record coverage
                    analyze_producers[field_name] = {
                        "producer": "structured_extract",
                        "stability": "soft",
                    }
                # If submit_phase_record already covers it, structured_extract is a fallback
                # — do not downgrade the producer
    except Exception:
        pass

    if analyze_producers:
        registry["ANALYZE"] = analyze_producers

    return registry


# ── Field Spec Definitions ───────────────────────────────────────────────────
# Start with repair_strategy_type, then expand to other fields.

def _get_all_field_specs() -> list[OnboardingFieldSpec]:
    """Return all field specs subject to onboarding audit."""
    from cognition_contracts.analysis_root_cause import REPAIR_STRATEGY_TYPES

    return [
        OnboardingFieldSpec(
            name="repair_strategy_type",
            phase="ANALYZE",
            field_type="enum",
            required=True,
            is_control_field=True,
            fail_closed=True,
            prompt_enabled=True,
            schema_enabled=True,
            producer="submit_phase_record",
            producer_stability="hard",
            consumers=("nprg", "retry_control", "telemetry"),
            prompt_key="REPAIR_STRATEGY_TYPE",
            prompt_format="REPAIR_STRATEGY_TYPE: <ENUM>",
            extraction_required=False,
            deterministic_fallback_allowed=False,
            enum_values=tuple(REPAIR_STRATEGY_TYPES),
        ),
        OnboardingFieldSpec(
            name="root_cause",
            phase="ANALYZE",
            field_type="string",
            required=True,
            is_control_field=False,
            fail_closed=True,
            prompt_enabled=True,
            schema_enabled=True,
            producer="submit_phase_record",
            producer_stability="hard",
            consumers=("telemetry",),
        ),
        OnboardingFieldSpec(
            name="causal_chain",
            phase="ANALYZE",
            field_type="string",
            required=True,
            is_control_field=False,
            fail_closed=True,
            prompt_enabled=True,
            schema_enabled=True,
            producer="submit_phase_record",
            producer_stability="hard",
            consumers=("telemetry",),
        ),
    ]


# ── Core Audit Logic ─────────────────────────────────────────────────────────

def audit_fields(
    field_specs: list[OnboardingFieldSpec],
    prompt_registry: dict[str, set[str]],
    schema_registry: dict[str, set[str]],
    gate_registry: dict[str, dict[str, set[str]]],
    consumer_registry: dict[str, set[str]],
    producer_registry: dict[str, dict[str, dict[str, str]]],
    tool_param_registry: dict[str, set[str]] | None = None,
) -> list[AuditError]:
    """
    Run onboarding audit against all registries.

    Returns list of AuditError. Empty = all fields fully onboarded.
    """
    errors: list[AuditError] = []
    if tool_param_registry is None:
        tool_param_registry = {}

    for spec in field_specs:
        phase = spec.phase
        f = spec.name

        prompt_fields = prompt_registry.get(phase, set())
        schema_fields = schema_registry.get(phase, set())
        gate_fields = gate_registry.get(phase, {}).get("fail_closed_fields", set())
        tool_params = tool_param_registry.get(phase, set())
        producer_info = producer_registry.get(phase, {}).get(f)

        # ── Rule 7: tool parameter must be wired for tool-submitted fields ──
        # This is THE critical check. If a field declares producer=submit_phase_record
        # but the tool doesn't have it as a parameter, the agent CANNOT submit it.
        # Claude will always bypass the tool and use free-text instead.
        if spec.producer == "submit_phase_record" and f not in tool_params:
            errors.append(AuditError(
                code="TOOL_PARAMETER_NOT_WIRED",
                field_name=f, phase=phase,
                message=(
                    f"{f} declares producer=submit_phase_record but is missing from "
                    f"submit_phase_record tool parameters — agent cannot submit this field"
                ),
            ))

        # ── Rule 1: control field must declare producer ──────────────────
        if spec.is_control_field and spec.producer is None:
            errors.append(AuditError(
                code="CONTROL_FIELD_NO_PRODUCER",
                field_name=f, phase=phase,
                message=f"{f} is control field but producer is not declared",
            ))

        # ── Rule 2: control field cannot rely solely on free-text extraction
        if spec.is_control_field and spec.producer == "structured_extract":
            if not spec.fail_closed and not spec.deterministic_fallback_allowed:
                errors.append(AuditError(
                    code="CONTROL_FIELD_SOFT_PRODUCER",
                    field_name=f, phase=phase,
                    message=(
                        f"{f} is control field with producer=structured_extract "
                        f"but neither fail_closed nor deterministic_fallback_allowed"
                    ),
                ))

        # ── Rule 3: declared consumers must be wired ─────────────────────
        for consumer in spec.consumers:
            consumer_fields = consumer_registry.get(consumer, set())
            if f not in consumer_fields:
                errors.append(AuditError(
                    code="DECLARED_CONSUMER_NOT_WIRED",
                    field_name=f, phase=phase,
                    message=f"{f} declares consumer={consumer} but consumer registry does not include it",
                ))

        # ── Rule 4: required + fail_closed must have gate validation ─────
        if spec.required and spec.fail_closed and f not in gate_fields:
            errors.append(AuditError(
                code="REQUIRED_FIELD_MISSING_GATE_VALIDATION",
                field_name=f, phase=phase,
                message=f"{f} is required fail_closed but not registered in gate validation",
            ))

        # ── Rule 5: required fields need prompt + schema + validation + producer
        if spec.required:
            if spec.prompt_enabled and f not in prompt_fields:
                errors.append(AuditError(
                    code="PROMPT_NOT_WIRED",
                    field_name=f, phase=phase,
                    message=f"{f} is enabled for prompt but not found in prompt registry",
                ))
            if spec.schema_enabled and f not in schema_fields:
                errors.append(AuditError(
                    code="SCHEMA_NOT_WIRED",
                    field_name=f, phase=phase,
                    message=f"{f} is enabled for schema but not found in schema registry",
                ))
            if producer_info is None and spec.producer is not None:
                errors.append(AuditError(
                    code="PRODUCER_NOT_WIRED",
                    field_name=f, phase=phase,
                    message=f"{f} declares producer={spec.producer} but producer registry has no entry",
                ))
            elif producer_info is not None:
                actual_producer = producer_info.get("producer")
                stability = producer_info.get("stability")
                if actual_producer != spec.producer:
                    errors.append(AuditError(
                        code="PRODUCER_MISMATCH",
                        field_name=f, phase=phase,
                        message=f"declared producer={spec.producer} but wired producer={actual_producer}",
                    ))

        # ── Rule 6: control field with consumers must have hard producer ──
        if spec.is_control_field:
            if not spec.fail_closed:
                errors.append(AuditError(
                    code="CONTROL_FIELD_NOT_FAIL_CLOSED",
                    field_name=f, phase=phase,
                    message=f"{f} is control field and must be fail_closed",
                ))
            if not spec.consumers:
                errors.append(AuditError(
                    code="CONTROL_FIELD_WITHOUT_CONSUMER",
                    field_name=f, phase=phase,
                    message=f"{f} is control field but has no declared consumers",
                ))
            if producer_info is not None:
                stability = producer_info.get("stability")
                if stability != "hard":
                    errors.append(AuditError(
                        code="CONTROL_FIELD_NO_HARD_PRODUCER",
                        field_name=f, phase=phase,
                        message=(
                            f"{f} is control field but producer stability is "
                            f"{stability!r}, expected 'hard'"
                        ),
                    ))

        # ── Consumer-before-truth check ──────────────────────────────────
        if spec.is_control_field and spec.consumers:
            if producer_info is None or producer_info.get("stability") != "hard":
                if f not in gate_fields:
                    errors.append(AuditError(
                        code="CONSUMER_BEFORE_TRUTH",
                        field_name=f, phase=phase,
                        message=(
                            f"{f} is consumed by {','.join(spec.consumers)} "
                            f"before stable truth path is established"
                        ),
                    ))

    return errors


# ── CLI Entry Point ──────────────────────────────────────────────────────────

def run_audit() -> list[AuditError]:
    """Build registries and run audit. Returns errors."""
    field_specs = _get_all_field_specs()
    prompt_reg = _build_prompt_registry()
    schema_reg = _build_schema_registry()
    gate_reg = _build_gate_registry()
    consumer_reg = _build_consumer_registry()
    producer_reg = _build_producer_registry()
    tool_param_reg = _build_tool_parameter_registry()

    return audit_fields(
        field_specs=field_specs,
        prompt_registry=prompt_reg,
        schema_registry=schema_reg,
        gate_registry=gate_reg,
        consumer_registry=consumer_reg,
        producer_registry=producer_reg,
        tool_param_registry=tool_param_reg,
    )


def main() -> int:
    errors = run_audit()

    if errors:
        print("ONBOARDING AUDIT FAILED")
        for err in errors:
            print(
                f"  [ONBOARDING_ERROR] code={err.code} "
                f"field={err.field_name} phase={err.phase} "
                f"message={err.message}"
            )
        return 1

    print("ONBOARDING AUDIT PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
