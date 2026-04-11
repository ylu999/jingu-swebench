"""
jingu_onboard.py — Single entry point for all jingu governance onboarding.

One call to `onboard()` produces everything the runtime needs:
  1. phase_prompts       — per-phase prompt text for agent injection
  2. phase_schemas       — per-phase JSON Schema for structured output (constrained decoding)
  3. policy_gates        — per-phase required_fields + forbidden_moves for gate checks
  4. principal_specs     — per-phase principal requirements + semantic checks + repair hints
  5. repair_templates    — per-principal rejection feedback text
  6. routing_rules       — per-principal failure routing (next_phase + strategy)
  7. phase_transitions   — allowed_next_phases per phase
  8. cognition_specs     — success_criteria + required_evidence_kinds per phase

All derived from bundle.json (compiled by jingu-cognition TS).
Zero hardcoded strings. Zero feature flags.

Usage:
    from jingu_onboard import onboard

    gov = onboard()  # reads bundle.json, returns JinguGovernance

    # Agent prompt injection:
    prompt = gov.get_phase_prompt("ANALYZE")

    # Structured output schema:
    schema = gov.get_extraction_schema("ANALYZE")

    # Gate evaluation:
    gate = gov.get_gate("ANALYZE")
    gate.required_fields   # ["root_cause", "evidence", ...]
    gate.required_principals  # ["causal_grounding", ...]
    gate.forbidden_moves   # ["do not write code", ...]

    # Rejection feedback:
    hint = gov.get_repair_hint("ANALYZE", "causal_grounding")

    # Failure routing:
    route = gov.get_route("ANALYZE", "causal_grounding")
    route.next_phase       # "ANALYZE"
    route.strategy         # "strengthen causal chain"
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_BUNDLE_PATH = str(Path(__file__).parent.parent / "bundle.json")


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PrincipalSpec:
    """Everything known about one principal in one phase."""
    name: str
    requires_fields: list[str]
    semantic_checks: list[str]
    repair_hint: str
    inference_rule_exists: bool
    fake_check_eligible: bool


@dataclass(frozen=True)
class PhaseGate:
    """Gate evaluation config for one phase."""
    phase: str
    subtype: str
    required_fields: list[str]
    forbidden_moves: list[str]
    required_principals: list[str]
    forbidden_principals: list[str]
    principal_specs: dict[str, PrincipalSpec]  # name -> spec


@dataclass(frozen=True)
class Route:
    """Where to go after a principal violation."""
    next_phase: str
    strategy: str


@dataclass(frozen=True)
class CognitionSpec:
    """Cognition-level spec for a phase."""
    task_shape: str
    success_criteria: list[str]
    required_evidence_kinds: list[str]


@dataclass(frozen=True)
class PhaseConfig:
    """Complete runtime config for one phase, derived from bundle contract."""
    phase: str
    subtype: str
    prompt: str
    schema: dict[str, Any]           # JSON Schema for structured output
    gate: PhaseGate
    cognition: CognitionSpec
    repair_templates: dict[str, str]  # principal_name -> repair text
    routing: dict[str, Route]         # principal_name -> Route
    allowed_next_phases: list[str]


# ── JinguGovernance ───────────────────────────────────────────────────────────

class JinguGovernance:
    """Complete governance runtime, derived from a single bundle.json.

    All 13 capabilities are loaded from the bundle at construction time.
    No feature flags. No fallbacks. If the bundle is missing or invalid,
    construction fails fast.
    """

    def __init__(self, phases: dict[str, PhaseConfig], metadata: dict[str, Any]):
        self._phases = phases
        self._metadata = metadata
        self._phase_order = list(phases.keys())

    # ── Phase prompt (items 1, 3, 5, 7) ──────────────────────────────────

    def get_phase_prompt(self, phase: str) -> str:
        """Get compiled prompt for a phase (includes policy + principal guidance)."""
        cfg = self._phases.get(phase.upper())
        if cfg is None:
            return ""
        return cfg.prompt

    # ── Structured output schema (items 2, 4, 6, 8) ─────────────────────

    def get_extraction_schema(self, phase: str) -> dict[str, Any] | None:
        """Get JSON Schema for structured output extraction."""
        cfg = self._phases.get(phase.upper())
        if cfg is None or not cfg.schema:
            return None
        return cfg.schema

    # ── Gate config (items 9, 10, 11, 12) ────────────────────────────────

    def get_gate(self, phase: str) -> PhaseGate | None:
        """Get gate evaluation config for a phase."""
        cfg = self._phases.get(phase.upper())
        if cfg is None:
            return None
        return cfg.gate

    # ── Repair hints (item 13) ───────────────────────────────────────────

    def get_repair_hint(self, phase: str, principal: str) -> str:
        """Get rejection feedback text for a principal violation."""
        cfg = self._phases.get(phase.upper())
        if cfg is None:
            return ""
        return cfg.repair_templates.get(principal, "")

    # ── Routing (item 13 continued) ──────────────────────────────────────

    def get_route(self, phase: str, principal: str) -> Route | None:
        """Get failure routing for a principal violation."""
        cfg = self._phases.get(phase.upper())
        if cfg is None:
            return None
        return cfg.routing.get(principal)

    # ── Cognition ────────────────────────────────────────────────────────

    def get_cognition(self, phase: str) -> CognitionSpec | None:
        """Get cognition spec (success criteria, evidence kinds)."""
        cfg = self._phases.get(phase.upper())
        if cfg is None:
            return None
        return cfg.cognition

    # ── Phase transitions ────────────────────────────────────────────────

    def get_allowed_next_phases(self, phase: str) -> list[str]:
        """Get allowed next phases from current phase."""
        cfg = self._phases.get(phase.upper())
        if cfg is None:
            return []
        return cfg.allowed_next_phases

    # ── Enumeration ──────────────────────────────────────────────────────

    def list_phases(self) -> list[str]:
        """All onboarded phases."""
        return self._phase_order

    def get_phase_config(self, phase: str) -> PhaseConfig | None:
        """Full config for a phase."""
        return self._phases.get(phase.upper())

    @property
    def metadata(self) -> dict[str, Any]:
        return self._metadata

    # ── Schema adaptation (structured output constraints) ────────────────

    def get_constrained_schema(self, phase: str) -> dict[str, Any] | None:
        """Get schema adapted for grammar-constrained sampling.

        Removes unsupported constraints (minLength, minimum, maximum)
        and adds additionalProperties: false to all objects.

        Returns None (triggers regex fallback) if adapted schema fails validation.
        """
        raw = self.get_extraction_schema(phase)
        if raw is None:
            return None
        adapted = _adapt_schema_for_constrained_decoding(raw)
        errors = _validate_adapted_schema(adapted, phase.upper())
        if errors:
            logger.warning(
                "[schema_validation] phase=%s errors=%s", phase.upper(), errors
            )
            return None
        return adapted


# ── Schema adaptation ─────────────────────────────────────────────────────

def _adapt_schema_for_constrained_decoding(schema: dict[str, Any]) -> dict[str, Any]:
    """Recursively adapt a JSON Schema for structured output constraints.

    - Remove: minLength, maxLength, minimum, maximum, multipleOf
    - Add: additionalProperties=false on all objects
    - Keep: everything else
    """
    result = {}
    for k, v in schema.items():
        if k in ("minLength", "maxLength", "minimum", "maximum", "multipleOf", "minItems"):
            continue
        if k == "properties" and isinstance(v, dict):
            result[k] = {pk: _adapt_schema_for_constrained_decoding(pv) for pk, pv in v.items()}
        elif k == "items" and isinstance(v, dict):
            result[k] = _adapt_schema_for_constrained_decoding(v)
        else:
            result[k] = v

    # Add additionalProperties=false to object types
    if result.get("type") == "object" and "additionalProperties" not in result:
        result["additionalProperties"] = False

    return result


def _validate_adapted_schema(schema: dict[str, Any], phase: str, _depth: int = 0) -> list[str]:
    """Validate that an adapted schema satisfies Anthropic/Bedrock constrained decoding requirements.

    Returns a list of error strings. Empty list = valid.

    Checks:
    - Top-level type == "object"
    - additionalProperties: false on all objects (including nested)
    - required array present on all objects
    - No $ref keys anywhere
    - Depth limit: max 4 levels
    """
    errors: list[str] = []
    path = f"{phase}(depth={_depth})"

    # Depth limit
    if _depth > 4:
        errors.append(f"{path}: schema exceeds max depth 4")
        return errors

    # $ref check (at any level)
    if "$ref" in schema:
        errors.append(f"{path}: contains $ref (not supported)")

    schema_type = schema.get("type")

    if schema_type == "object":
        # additionalProperties: false required
        if schema.get("additionalProperties") is not False:
            errors.append(f"{path}: object missing additionalProperties: false")

        # required array must be present
        if "required" not in schema:
            errors.append(f"{path}: object missing required array")

        # Recurse into properties
        for prop_name, prop_schema in schema.get("properties", {}).items():
            if isinstance(prop_schema, dict):
                errors.extend(
                    _validate_adapted_schema(prop_schema, f"{phase}.{prop_name}", _depth + 1)
                )

    # Recurse into array items
    if schema_type == "array" and isinstance(schema.get("items"), dict):
        errors.extend(
            _validate_adapted_schema(schema["items"], f"{phase}.items", _depth + 1)
        )

    return errors


# ── Phase resolution ──────────────────────────────────────────────────────

_PHASE_TO_SUBTYPE: dict[str, str] = {
    "UNDERSTAND": "understanding.context_building",
    "OBSERVE":    "observation.fact_gathering",
    "ANALYZE":    "analysis.root_cause",
    "DECIDE":     "decision.fix_direction",
    "DESIGN":     "design.solution_shape",
    "EXECUTE":    "execution.code_patch",
    "JUDGE":      "judge.verification",
}


# ── Bundle parser ─────────────────────────────────────────────────────────

def _parse_contract(phase: str, subtype: str, contract: dict[str, Any]) -> PhaseConfig:
    """Parse one bundle contract into a PhaseConfig."""

    # Principals
    principal_specs: dict[str, PrincipalSpec] = {}
    for p in contract.get("principals", []):
        principal_specs[p["name"]] = PrincipalSpec(
            name=p["name"],
            requires_fields=p.get("requires_fields", []),
            semantic_checks=p.get("semantic_checks", []),
            repair_hint=p.get("repair_hint", ""),
            inference_rule_exists=p.get("inference_rule_exists", False),
            fake_check_eligible=p.get("fake_check_eligible", False),
        )

    # Policy / Gate
    policy = contract.get("policy", {})
    gate = PhaseGate(
        phase=phase,
        subtype=subtype,
        required_fields=policy.get("required_fields", []),
        forbidden_moves=policy.get("forbidden_moves", []),
        required_principals=policy.get("required_principals", []),
        forbidden_principals=policy.get("forbidden_principals", []),
        principal_specs=principal_specs,
    )

    # Cognition
    cog = contract.get("cognition_spec", {})
    cognition = CognitionSpec(
        task_shape=cog.get("task_shape", ""),
        success_criteria=cog.get("success_criteria", []),
        required_evidence_kinds=cog.get("required_evidence_kinds", []),
    )

    # Routing
    routing_raw = contract.get("routing", {}).get("principal_routes", {})
    routing: dict[str, Route] = {}
    for pname, rdata in routing_raw.items():
        routing[pname] = Route(
            next_phase=rdata.get("next_phase", phase),
            strategy=rdata.get("strategy", ""),
        )

    # Phase transitions
    phase_spec = contract.get("phase_spec", {})
    allowed_next = phase_spec.get("allowed_next_phases", [])

    # Schema (raw from bundle — caller uses get_constrained_schema for LLM)
    schema = contract.get("schema", {})

    return PhaseConfig(
        phase=phase,
        subtype=subtype,
        prompt=contract.get("prompt", ""),
        schema=schema,
        gate=gate,
        cognition=cognition,
        repair_templates=contract.get("repair_templates", {}),
        routing=routing,
        allowed_next_phases=allowed_next,
    )


# ── Helper for compiler integration (p224-09) ─────────────────────────────

def _build_governance_from_compiled(
    resolved: Any,
    validators: dict[str, Any],
) -> "JinguGovernance":
    """Build JinguGovernance from a pre-compiled ResolvedBundle + validators dict.

    This is the compiler-path counterpart to onboard().  It does NOT re-parse
    or re-validate the bundle — both resolved and validators are the outputs of
    the 8-stage compile_bundle() pipeline, which has already done all validation.

    The function assembles PhaseConfig objects from the already-resolved data
    and returns a JinguGovernance.  All PhaseConfig construction follows the
    same _parse_contract() logic used in onboard(), but fed from the resolved
    bundle rather than raw JSON.

    Args:
        resolved:   ResolvedBundle from bundle_compiler._resolve_refs()
        validators: dict[phase_str -> CompiledValidator] from _compile_validators()

    Returns:
        JinguGovernance ready for use as bundle.governance
    """
    phases: dict[str, PhaseConfig] = {}

    for subtype_key, contract in resolved.subtype_to_contract.items():
        phase = contract.get("phase", "").upper()
        if not phase:
            continue
        phases[phase] = _parse_contract(phase, subtype_key, contract)

    raw = resolved.raw
    metadata = {
        "version": raw.get("version", ""),
        "compiler_version": raw.get("compiler_version", ""),
        "generated_at": raw.get("generated_at", ""),
        "contract_count": len(phases),
        "phases_onboarded": list(phases.keys()),
    }

    return JinguGovernance(phases, metadata)


# ── The single entry point ────────────────────────────────────────────────

_cached_governance: JinguGovernance | None = None


def onboard(bundle_path: str | None = None, *, force_reload: bool = False) -> JinguGovernance:
    """Load bundle.json and produce the complete governance runtime.

    This is the SINGLE ENTRY POINT. One call loads all 13 items:
      1-2.   phase prompts + schemas
      3-4.   policy prompts + schemas (embedded in phase prompt/schema)
      5-6.   cognition prompts + schemas (embedded in phase prompt/schema)
      7-8.   principal prompts + schemas (embedded in phase prompt/schema)
      9-12.  phase/policy/cognition/principal gates (PhaseGate dataclass)
      13.    repair hints + routing (repair_templates + routing dicts)

    Returns:
        JinguGovernance instance (cached on first call).

    Raises:
        FileNotFoundError: If bundle.json does not exist.
        ValueError: If bundle version is incompatible.
    """
    global _cached_governance
    if _cached_governance is not None and not force_reload:
        return _cached_governance

    path = bundle_path or _DEFAULT_BUNDLE_PATH
    with open(path, "r", encoding="utf-8") as f:
        bundle = json.load(f)

    # Version check
    version = bundle.get("version", "")
    if not version or not version.startswith("1."):
        raise ValueError(f"Unsupported bundle version: {version}")

    # Parse all contracts
    contracts_raw = bundle.get("contracts", {})
    phases: dict[str, PhaseConfig] = {}

    for subtype_key, contract in contracts_raw.items():
        phase = contract.get("phase", "").upper()
        if not phase:
            logger.warning("Contract %s has no phase, skipping", subtype_key)
            continue
        phases[phase] = _parse_contract(phase, subtype_key, contract)

    metadata = {
        "version": version,
        "compiler_version": bundle.get("compiler_version", ""),
        "generated_at": bundle.get("generated_at", ""),
        "contract_count": len(phases),
        "phases_onboarded": list(phases.keys()),
    }

    gov = JinguGovernance(phases, metadata)

    # Startup schema validation — log per-phase constrained decoding readiness
    all_phase_names = ["UNDERSTAND", "OBSERVE", "ANALYZE", "DECIDE", "DESIGN", "EXECUTE", "JUDGE"]
    for p in all_phase_names:
        raw = gov.get_extraction_schema(p)
        if raw is None:
            logger.info("[onboard] schema_validation phase=%s: no schema (expected)", p)
            continue
        constrained = gov.get_constrained_schema(p)
        if constrained is not None:
            logger.info("[onboard] schema_validation phase=%s: OK", p)
        else:
            logger.warning("[onboard] schema_validation phase=%s: FAILED (regex fallback)", p)

    _cached_governance = gov

    logger.info(
        "jingu onboarded: %d phases from bundle v%s",
        len(phases), version,
    )

    return gov
