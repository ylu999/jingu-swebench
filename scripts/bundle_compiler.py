"""Bundle compiler — 8-stage pipeline. compile_bundle() is the single entry point."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Error code registry
# ---------------------------------------------------------------------------
# Stage: PARSE
#   BC-P001  invalid_bundle_format    — bundle JSON does not match expected schema
#   BC-P002  missing_version          — bundle missing version field
#   BC-P003  unsupported_version      — bundle version not supported by this compiler
#
# Stage: RESOLVE
#   BC-R001  missing_phase_mapping    — phase has no subtype mapping
#   BC-R002  missing_contract         — subtype referenced but contract not found
#   BC-R003  orphan_principal         — principal in registry but not in any contract
#
# Stage: VALIDATE
#   BC-V001  empty_required_fields    — contract has no required fields
#   BC-V002  principal_not_in_registry — required principal not found in registry
#   BC-V003  duplicate_principal      — principal appears in both required and forbidden
#
# Stage: COMPILE_VALIDATORS
#   BC-CV001 inference_without_rule   — principal marked inference_eligible but no rule
#   BC-CV002 fake_without_inference   — principal marked fake_check_eligible but not inference_eligible
#
# Stage: COMPILE_ROUTES
#   BC-CR001 missing_next_phase       — route references non-existent phase
#   BC-CR002 duplicate_route          — same (phase, principal) route defined twice
#
# Stage: LINK
#   BC-L001  validator_route_mismatch — validator references principal with no route
#
# Stage: ACTIVATE
#   BC-A001  zero_validators          — no validators compiled
#   BC-A002  zero_routes              — no routes compiled
#
# Stage: EMIT
#   BC-E001  serialization_failure    — compiled bundle cannot be serialized
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Error / Warning types
# ---------------------------------------------------------------------------

class CompilationError(Exception):
    """Error raised during bundle compilation."""

    def __init__(
        self,
        stage: str,
        code: str,
        message: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.stage = stage
        self.code = code
        self.message = message
        self.context: dict[str, Any] = context if context is not None else {}
        super().__init__(str(self))

    def __str__(self) -> str:
        return f"[{self.stage}:{self.code}] {self.message} {self.context}"


@dataclass(frozen=True)
class CompilationWarning:
    """Non-fatal warning emitted during bundle compilation."""

    stage: str
    code: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Intermediate result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ParseResult:
    """Output of the PARSE stage."""

    bundle: dict[str, Any]
    version: str
    compiler_version: str
    generated_at: str
    generator_commit: str
    capabilities: list[str]


@dataclass(frozen=True)
class ResolvedBundle:
    """Output of the RESOLVE stage — bundle with all references resolved."""

    raw: dict[str, Any]
    phase_to_subtype: dict[str, str]
    subtype_to_contract: dict[str, dict]
    schema_registry: dict[str, dict]
    principal_registry: dict[str, dict]
    phases_with_contracts: frozenset[str]
    phases_without_contracts: frozenset[str]


# ---------------------------------------------------------------------------
# Compiled output types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CompiledValidator:
    """Per-phase/subtype validator compiled from contract + registry."""

    phase: str
    subtype: str
    required_fields: tuple[str, ...]
    required_principals: tuple[str, ...]
    forbidden_principals: tuple[str, ...]
    forbidden_moves: tuple[str, ...]
    semantic_checks: dict[str, tuple[str, ...]]
    requires_fields_per_principal: dict[str, tuple[str, ...]]
    inference_eligible: frozenset[str]
    fake_check_eligible: frozenset[str]


@dataclass(frozen=True)
class CompiledRoute:
    """Single retry route: on failure of a principal, where to go next."""

    failure_principal: str
    next_phase: str
    strategy: str
    repair_template: str


@dataclass(frozen=True)
class CompiledRetryRouter:
    """All retry routes compiled from bundle."""

    routes: dict[tuple[str, str], CompiledRoute]
    default_routes: dict[str, CompiledRoute]


@dataclass(frozen=True)
class ActivationReport:
    """Report emitted by the ACTIVATE stage — summary of compilation."""

    bundle_version: str
    compiler_version: str
    compiled_at: str
    phases_covered: frozenset[str]
    phases_missing: frozenset[str]
    principals_total: int
    principals_fake_eligible: int
    validators_compiled: int
    routes_compiled: int
    warnings: list[CompilationWarning]
    errors: list[CompilationError]
    is_valid: bool


@dataclass(frozen=True)
class CompiledBundle:
    """Final output of compile_bundle() — the fully compiled governance bundle."""

    governance: Any
    activation_report: ActivationReport
    validators: dict[str, CompiledValidator]
    retry_router: CompiledRetryRouter


# ---------------------------------------------------------------------------
# DEFAULT_BUNDLE_PATH
# ---------------------------------------------------------------------------

DEFAULT_BUNDLE_PATH: str = os.environ.get("JINGU_BUNDLE_PATH", "bundle.json")


# ---------------------------------------------------------------------------
# S1 — PARSE
# ---------------------------------------------------------------------------

_REQUIRED_TOP_LEVEL_KEYS = ("version", "phases", "contracts", "cognition")


def _parse_bundle(path: str) -> ParseResult:
    """S1: Load and validate bundle JSON, extract metadata."""
    with open(path) as f:
        bundle: dict[str, Any] = json.load(f)

    # Version check
    version = bundle.get("version", "")
    if not version:
        raise CompilationError("S1", "MISSING_VERSION", "Bundle has no version field")

    major = version.split(".")[0]
    if major != "1":
        raise CompilationError(
            "S1",
            "INCOMPATIBLE_VERSION",
            f"Unsupported bundle version: {version} (expected major version 1)",
            {"version": version},
        )

    # Required top-level keys
    for key in _REQUIRED_TOP_LEVEL_KEYS:
        if key not in bundle:
            raise CompilationError(
                "S1",
                "MISSING_TOP_LEVEL_KEY",
                f"Missing required top-level key: {key}",
                {"key": key},
            )

    return ParseResult(
        bundle=bundle,
        version=version,
        compiler_version=bundle.get("compiler_version", ""),
        generated_at=bundle.get("generated_at", ""),
        generator_commit=bundle.get("generator_commit", ""),
        capabilities=bundle.get("capabilities", []),
    )


# ---------------------------------------------------------------------------
# S2 — RESOLVE
# ---------------------------------------------------------------------------

def _resolve_refs(parsed: ParseResult) -> ResolvedBundle:
    """S2: Resolve cross-references — derive phase_to_subtype, build registries, validate refs."""
    bundle = parsed.bundle
    contracts = bundle.get("contracts", {})
    all_phases = set(bundle.get("phases", []))

    # --- Derive phase_to_subtype from contracts (NOT hardcoded) ---
    phase_to_subtype: dict[str, str] = {}
    subtype_to_contract: dict[str, dict] = {}

    for subtype_key, contract in contracts.items():
        phase = contract.get("phase", "").upper()
        if not phase:
            raise CompilationError(
                "S2",
                "PHASE_NO_SUBTYPE",
                f"Contract '{subtype_key}' has no phase field",
                {"subtype": subtype_key},
            )
        if phase in phase_to_subtype:
            raise CompilationError(
                "S2",
                "DUPLICATE_PHASE_MAPPING",
                f"Phase '{phase}' already mapped to '{phase_to_subtype[phase]}', "
                f"cannot also map to '{subtype_key}'",
                {"phase": phase, "existing": phase_to_subtype[phase], "new": subtype_key},
            )
        phase_to_subtype[phase] = subtype_key
        subtype_to_contract[subtype_key] = contract

    # --- Build schema_registry from contract inline schemas ---
    schema_registry: dict[str, dict] = {}
    for subtype_key, contract in contracts.items():
        inline_schema = contract.get("schema")
        if inline_schema:
            schema_registry[subtype_key] = inline_schema

    # Also include top-level schemas if present
    top_schemas = bundle.get("schemas", {})
    if isinstance(top_schemas, dict):
        for schema_name, schema_def in top_schemas.items():
            if schema_name not in schema_registry:
                schema_registry[schema_name] = schema_def

    # --- Validate schema_refs ---
    for subtype_key, contract in contracts.items():
        cognition_spec = contract.get("cognition_spec", {})
        schema_ref = cognition_spec.get("schema_ref", "")
        if schema_ref and schema_ref not in schema_registry:
            raise CompilationError(
                "S2",
                "DANGLING_SCHEMA_REF",
                f"Contract '{subtype_key}' references schema '{schema_ref}' which does not exist",
                {"subtype": subtype_key, "schema_ref": schema_ref},
            )

    # --- Build principal_registry (deduped by name) ---
    principal_registry: dict[str, dict] = {}
    for _subtype_key, contract in contracts.items():
        for principal in contract.get("principals", []):
            name = principal.get("name", "")
            if name and name not in principal_registry:
                principal_registry[name] = principal

    # --- Validate principal refs in policy.required_principals ---
    for subtype_key, contract in contracts.items():
        policy = contract.get("policy", {})
        for req_principal in policy.get("required_principals", []):
            if req_principal not in principal_registry:
                raise CompilationError(
                    "S2",
                    "DANGLING_PRINCIPAL_REF",
                    f"Contract '{subtype_key}' policy requires principal "
                    f"'{req_principal}' which is not in any contract's principals list",
                    {"subtype": subtype_key, "principal": req_principal},
                )

    # --- Compute phases_with_contracts / phases_without_contracts ---
    phases_with = frozenset(phase_to_subtype.keys())
    phases_without = frozenset(all_phases - phases_with)

    return ResolvedBundle(
        raw=bundle,
        phase_to_subtype=phase_to_subtype,
        subtype_to_contract=subtype_to_contract,
        schema_registry=schema_registry,
        principal_registry=principal_registry,
        phases_with_contracts=phases_with,
        phases_without_contracts=phases_without,
    )
