"""Typed gate failure codes — replaces ad-hoc string concatenation."""
from enum import Enum
from dataclasses import dataclass


class GateFailureCategory(str, Enum):
    MISSING_PRINCIPAL = "MISSING_PRINCIPAL"
    FORBIDDEN_PRINCIPAL = "FORBIDDEN_PRINCIPAL"
    MISSING_FIELD = "MISSING_FIELD"
    SEMANTIC_FAIL = "SEMANTIC_FAIL"
    MISSING_EVIDENCE_BASIS = "MISSING_EVIDENCE_BASIS"
    FORBIDDEN_TRANSITION = "FORBIDDEN_TRANSITION"
    FAKE_PRINCIPAL = "FAKE_PRINCIPAL"
    MISSING_REQUIRED_INFERENCE = "MISSING_REQUIRED_INFERENCE"


class GateFailureSeverity(str, Enum):
    RETRYABLE = "retryable"
    REJECTED = "rejected"


_SEVERITY_MAP: dict[GateFailureCategory, GateFailureSeverity] = {
    GateFailureCategory.MISSING_PRINCIPAL: GateFailureSeverity.RETRYABLE,
    GateFailureCategory.FORBIDDEN_PRINCIPAL: GateFailureSeverity.REJECTED,
    GateFailureCategory.MISSING_FIELD: GateFailureSeverity.RETRYABLE,
    GateFailureCategory.SEMANTIC_FAIL: GateFailureSeverity.RETRYABLE,
    GateFailureCategory.MISSING_EVIDENCE_BASIS: GateFailureSeverity.RETRYABLE,
    GateFailureCategory.FORBIDDEN_TRANSITION: GateFailureSeverity.REJECTED,
    GateFailureCategory.FAKE_PRINCIPAL: GateFailureSeverity.RETRYABLE,
    GateFailureCategory.MISSING_REQUIRED_INFERENCE: GateFailureSeverity.RETRYABLE,
}


@dataclass(frozen=True)
class GateFailureCode:
    category: GateFailureCategory
    subcode: str
    severity: GateFailureSeverity
    gate_rule: str
    phase: str
    subtype: str

    @property
    def code(self) -> str:
        """Backward-compatible string code: CATEGORY:subcode"""
        return f"{self.category.value}:{self.subcode}"

    def repair_target(self, bundle: dict | None = None) -> str:
        """3-level priority: principal_route > default_route > phase fallback."""
        if bundle:
            contracts = bundle.get("contracts", {})
            contract = contracts.get(self.subtype, {})
            routing = contract.get("routing", {})
            principal_routes = routing.get("principal_routes", {})
            if self.gate_rule in principal_routes:
                return principal_routes[self.gate_rule]
            if "default_route" in routing:
                return routing["default_route"]
        return self.phase

    def repair_hint(self, bundle: dict | None = None) -> str:
        """Lookup repair hint from bundle repair_templates."""
        if bundle:
            contracts = bundle.get("contracts", {})
            contract = contracts.get(self.subtype, {})
            templates = contract.get("repair_templates", {})
            if self.gate_rule in templates:
                template = templates[self.gate_rule]
                if isinstance(template, dict):
                    return template.get("hint", "")
                return str(template)
        return ""


# --- Failure code registry: documents all valid category+subcode patterns ---

FAILURE_CODE_REGISTRY: dict[GateFailureCategory, dict[str, str]] = {
    GateFailureCategory.MISSING_PRINCIPAL: {
        "pattern": "<principal_name>",
        "description": "Required principal not declared by agent",
        "example_subcode": "causal_grounding",
    },
    GateFailureCategory.FORBIDDEN_PRINCIPAL: {
        "pattern": "<principal_name>",
        "description": "Principal declared but forbidden for this phase/subtype",
        "example_subcode": "minimal_change",
    },
    GateFailureCategory.MISSING_FIELD: {
        "pattern": "<field_name>",
        "description": "Required schema field missing or empty in phase record",
        "example_subcode": "root_cause",
    },
    GateFailureCategory.SEMANTIC_FAIL: {
        "pattern": "<field_name>",
        "description": "Field present but fails semantic validation (min_length, structure)",
        "example_subcode": "causal_chain",
    },
    GateFailureCategory.MISSING_EVIDENCE_BASIS: {
        "pattern": "<field_name>",
        "description": "Field lacks required evidence references or grounding",
        "example_subcode": "evidence_refs",
    },
    GateFailureCategory.FORBIDDEN_TRANSITION: {
        "pattern": "<from_phase>-><to_phase>",
        "description": "Phase transition not allowed by transition policy",
        "example_subcode": "ANALYZE->EXECUTE",
    },
    GateFailureCategory.FAKE_PRINCIPAL: {
        "pattern": "<principal_name>",
        "description": "Principal declared but inference signals absent (behavioral fake)",
        "example_subcode": "evidence_linkage",
    },
    GateFailureCategory.MISSING_REQUIRED_INFERENCE: {
        "pattern": "<principal_name>",
        "description": "Inference rule required but not registered for this subtype",
        "example_subcode": "ontology_alignment",
    },
}


# --- Factory functions for common failure codes ---


def missing_principal(principal: str, phase: str, subtype: str) -> GateFailureCode:
    return GateFailureCode(
        category=GateFailureCategory.MISSING_PRINCIPAL,
        subcode=principal,
        severity=_SEVERITY_MAP[GateFailureCategory.MISSING_PRINCIPAL],
        gate_rule="check_required_principals",
        phase=phase,
        subtype=subtype,
    )


def forbidden_principal(principal: str, phase: str, subtype: str) -> GateFailureCode:
    return GateFailureCode(
        category=GateFailureCategory.FORBIDDEN_PRINCIPAL,
        subcode=principal,
        severity=_SEVERITY_MAP[GateFailureCategory.FORBIDDEN_PRINCIPAL],
        gate_rule="check_forbidden_principals",
        phase=phase,
        subtype=subtype,
    )


def missing_field(field_name: str, phase: str, subtype: str, gate_rule: str = "check_required_fields") -> GateFailureCode:
    return GateFailureCode(
        category=GateFailureCategory.MISSING_FIELD,
        subcode=field_name,
        severity=_SEVERITY_MAP[GateFailureCategory.MISSING_FIELD],
        gate_rule=gate_rule,
        phase=phase,
        subtype=subtype,
    )


def semantic_fail(field_name: str, phase: str, subtype: str, gate_rule: str = "") -> GateFailureCode:
    return GateFailureCode(
        category=GateFailureCategory.SEMANTIC_FAIL,
        subcode=field_name,
        severity=_SEVERITY_MAP[GateFailureCategory.SEMANTIC_FAIL],
        gate_rule=gate_rule,
        phase=phase,
        subtype=subtype,
    )


def forbidden_transition(from_phase: str, to_phase: str, subtype: str) -> GateFailureCode:
    return GateFailureCode(
        category=GateFailureCategory.FORBIDDEN_TRANSITION,
        subcode=f"{from_phase}->{to_phase}",
        severity=_SEVERITY_MAP[GateFailureCategory.FORBIDDEN_TRANSITION],
        gate_rule="check_phase_transition",
        phase=from_phase,
        subtype=subtype,
    )


def get_repair_hint(failure: GateFailureCode, bundle: dict) -> str:
    """Unified repair hint lookup: bundle repair_templates > empty.

    Single source for all repair hints — replaces _FEEDBACK dict in principal_gate.py.
    Priority:
      1. Bundle repair_templates (keyed by gate_rule)
      2. Empty string (caller provides its own fallback if needed)
    """
    return failure.repair_hint(bundle)


def fake_principal(principal: str, phase: str, subtype: str) -> GateFailureCode:
    return GateFailureCode(
        category=GateFailureCategory.FAKE_PRINCIPAL,
        subcode=principal,
        severity=_SEVERITY_MAP[GateFailureCategory.FAKE_PRINCIPAL],
        gate_rule="check_fake_principals",
        phase=phase,
        subtype=subtype,
    )
