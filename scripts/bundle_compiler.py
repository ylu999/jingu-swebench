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
# Stage: COMPILE_PROMPTS
#   BC-CP001 prompt_missing_field_mention     — prompt does not mention a required field
#   BC-CP002 prompt_missing_principal_mention — prompt does not mention a required principal
#   BC-CP003 prompt_missing_forbidden_mention — prompt does not mention a forbidden move
#   BC-CP004 prompt_missing_criteria_mention  — prompt does not mention a success criterion
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
    """Report emitted by the ACTIVATE stage (S8) — activation proof for RT4."""

    # Activation status
    activation_ok: bool

    # Bundle metadata
    bundle_version: str
    compiler_version: str
    generated_at: str
    generator_commit: str

    # Phase coverage
    phases_compiled: list[str]
    phases_missing: list[str]
    phases_allowed_missing: list[str]

    # Counts
    contracts_compiled: int
    principals_total: int
    principals_inference_eligible: int
    principals_fake_check_eligible: int

    # Per-stage diagnostics
    completeness_errors: list[str]
    consistency_errors: list[str]
    prompt_warnings: list[str]

    # Completeness flags
    transition_matrix_complete: bool
    routing_coverage_complete: bool


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


# ---------------------------------------------------------------------------
# S3 — COMPLETENESS CHECK
# ---------------------------------------------------------------------------

def _check_completeness(
    resolved: ResolvedBundle,
    allowed_no_contract_phases: frozenset[str] = frozenset({"UNDERSTAND"}),
) -> list[CompilationError]:
    """S3: Verify every phase has a contract (or is explicitly allowed) and each contract is complete.

    NEVER raises — always returns a (possibly empty) list of errors.
    """
    errors: list[CompilationError] = []

    # --- Phase coverage check ---
    for phase in sorted(resolved.phases_without_contracts):
        if phase not in allowed_no_contract_phases:
            errors.append(CompilationError(
                "S3",
                "PHASE_NO_CONTRACT_NOT_ALLOWLISTED",
                f"Phase '{phase}' has no contract and is not in the allowed list",
                {"phase": phase},
            ))

    # --- Per-contract checks ---
    for phase, subtype in sorted(resolved.phase_to_subtype.items()):
        contract = resolved.subtype_to_contract.get(subtype, {})
        ctx = {"phase": phase, "subtype": subtype}

        # prompt
        if not contract.get("prompt", ""):
            errors.append(CompilationError(
                "S3", "MISSING_PROMPT",
                f"Contract '{subtype}' has no prompt",
                ctx,
            ))

        # schema
        schema = contract.get("schema")
        if not isinstance(schema, dict) or not schema:
            errors.append(CompilationError(
                "S3", "MISSING_SCHEMA",
                f"Contract '{subtype}' has no schema",
                ctx,
            ))
        else:
            # schema shape check
            for required_key in ("type", "properties", "required"):
                if required_key not in schema:
                    errors.append(CompilationError(
                        "S3", "INVALID_SCHEMA_SHAPE",
                        f"Contract '{subtype}' schema missing key '{required_key}'",
                        {**ctx, "missing_key": required_key},
                    ))

        # policy.required_principals
        policy = contract.get("policy", {})
        if "required_principals" not in policy:
            errors.append(CompilationError(
                "S3", "MISSING_POLICY_KEY",
                f"Contract '{subtype}' policy missing 'required_principals'",
                ctx,
            ))

        # principals array
        principals = contract.get("principals")
        if not isinstance(principals, list):
            errors.append(CompilationError(
                "S3", "MISSING_PRINCIPALS_ARRAY",
                f"Contract '{subtype}' has no principals array",
                ctx,
            ))

        # cognition_spec.task_shape
        cognition_spec = contract.get("cognition_spec", {})
        if "task_shape" not in cognition_spec:
            errors.append(CompilationError(
                "S3", "MISSING_COGNITION_SPEC",
                f"Contract '{subtype}' cognition_spec missing 'task_shape'",
                ctx,
            ))

        # repair_templates
        repair_templates = contract.get("repair_templates")
        if not isinstance(repair_templates, dict):
            errors.append(CompilationError(
                "S3", "MISSING_REPAIR_TEMPLATES",
                f"Contract '{subtype}' has no repair_templates",
                ctx,
            ))

        # routing.principal_routes
        routing = contract.get("routing", {})
        principal_routes = routing.get("principal_routes")
        if not isinstance(principal_routes, dict):
            errors.append(CompilationError(
                "S3", "MISSING_ROUTING",
                f"Contract '{subtype}' routing missing 'principal_routes'",
                ctx,
            ))

        # phase_spec.allowed_next_phases
        phase_spec = contract.get("phase_spec", {})
        allowed_next = phase_spec.get("allowed_next_phases", [])
        if not allowed_next:
            errors.append(CompilationError(
                "S3", "MISSING_ALLOWED_NEXT_PHASES",
                f"Contract '{subtype}' phase_spec missing or empty 'allowed_next_phases'",
                ctx,
            ))

    return errors


# ---------------------------------------------------------------------------
# S4 — CONSISTENCY CHECK
# ---------------------------------------------------------------------------

def _check_consistency(
    resolved: ResolvedBundle,
) -> tuple[list[CompilationError], list[CompilationWarning]]:
    """S4: Cross-contract consistency validation.

    Six checks (4.1–4.6) that verify contracts, principals, routing, and
    transitions are mutually consistent.

    NEVER raises — always returns (errors, warnings).
    """
    errors: list[CompilationError] = []
    warnings: list[CompilationWarning] = []

    cognition = resolved.raw.get("cognition", {})
    cognition_subtypes = cognition.get("subtypes", [])

    # --- 4.1 Subtype/phase alignment (CC1) ---
    for cog_sub in cognition_subtypes:
        sub_name = cog_sub.get("name", "")
        cog_phase = cog_sub.get("phase", "").upper()
        contract = resolved.subtype_to_contract.get(sub_name)
        if contract is None:
            continue
        contract_phase = contract.get("phase", "").upper()
        if cog_phase and contract_phase and cog_phase != contract_phase:
            errors.append(CompilationError(
                "S4",
                "SUBTYPE_PHASE_MISMATCH",
                f"Cognition subtype '{sub_name}' declares phase '{cog_phase}' "
                f"but contract declares phase '{contract_phase}'",
                {"subtype": sub_name, "cognition_phase": cog_phase, "contract_phase": contract_phase},
            ))

    # --- 4.2 Principal scope cross-check (CC3) ---
    for subtype_key, contract in resolved.subtype_to_contract.items():
        policy = contract.get("policy", {})
        principals_array = contract.get("principals", [])
        principal_names_in_array = {
            p.get("name", "") for p in principals_array if isinstance(p, dict)
        }

        for req_principal in policy.get("required_principals", []):
            # Check: principal must be in the contract's principals[] array
            if req_principal not in principal_names_in_array:
                errors.append(CompilationError(
                    "S4",
                    "PRINCIPAL_NOT_IN_ARRAY",
                    f"Contract '{subtype_key}' requires principal '{req_principal}' "
                    f"but it is not in the contract's principals array",
                    {"subtype": subtype_key, "principal": req_principal},
                ))
                continue  # skip scope check if not even in array

            # Check: principal's applies_to must include current subtype
            principal_def = None
            for p in principals_array:
                if isinstance(p, dict) and p.get("name") == req_principal:
                    principal_def = p
                    break
            if principal_def is not None:
                applies_to = principal_def.get("applies_to", [])
                if applies_to and subtype_key not in applies_to:
                    errors.append(CompilationError(
                        "S4",
                        "PRINCIPAL_SCOPE_MISMATCH",
                        f"Contract '{subtype_key}' requires principal '{req_principal}' "
                        f"but its applies_to does not include '{subtype_key}'",
                        {"subtype": subtype_key, "principal": req_principal, "applies_to": applies_to},
                    ))

    # --- 4.3 Forbidden/required disjoint ---
    for subtype_key, contract in resolved.subtype_to_contract.items():
        policy = contract.get("policy", {})
        required = set(policy.get("required_principals", []))
        forbidden = set(policy.get("forbidden_principals", []))
        overlap = required & forbidden
        if overlap:
            errors.append(CompilationError(
                "S4",
                "FORBIDDEN_REQUIRED_OVERLAP",
                f"Contract '{subtype_key}' has principals in both required and forbidden: "
                f"{sorted(overlap)}",
                {"subtype": subtype_key, "overlap": sorted(overlap)},
            ))

    # --- 4.4 Principal lifecycle (CC3) ---
    for principal_name, principal_def in resolved.principal_registry.items():
        fake_eligible = principal_def.get("fake_check_eligible", False)
        inference_exists = principal_def.get("inference_rule_exists", False)
        if fake_eligible and not inference_exists:
            errors.append(CompilationError(
                "S4",
                "LIFECYCLE_VIOLATION",
                f"Principal '{principal_name}' has fake_check_eligible=true "
                f"but inference_rule_exists=false",
                {"principal": principal_name},
            ))

    # --- 4.5 Routing coverage ---
    for subtype_key, contract in resolved.subtype_to_contract.items():
        policy = contract.get("policy", {})
        routing = contract.get("routing", {})
        principal_routes = routing.get("principal_routes", {})
        if not isinstance(principal_routes, dict):
            principal_routes = {}

        for req_principal in policy.get("required_principals", []):
            if req_principal not in principal_routes:
                errors.append(CompilationError(
                    "S4",
                    "ROUTING_COVERAGE_MISSING",
                    f"Contract '{subtype_key}' requires principal '{req_principal}' "
                    f"but routing.principal_routes has no entry for it",
                    {"subtype": subtype_key, "principal": req_principal},
                ))

    # --- 4.6 Transition matrix completeness (WARNING only) ---
    # Collect all allowed_next_phases from contracts
    declared_transitions: set[tuple[str, str]] = set()
    for subtype_key, contract in resolved.subtype_to_contract.items():
        phase_spec = contract.get("phase_spec", {})
        from_phase = contract.get("phase", "").upper()
        for next_phase in phase_spec.get("allowed_next_phases", []):
            declared_transitions.add((from_phase, next_phase.upper()))

    # Build set of transitions present in cognition.transitions[]
    cognition_transitions = cognition.get("transitions", [])
    covered_transitions: set[tuple[str, str]] = set()
    for t in cognition_transitions:
        from_p = t.get("from", "").upper()
        to_p = t.get("to", "").upper()
        if from_p and to_p:
            covered_transitions.add((from_p, to_p))

    missing_transitions = declared_transitions - covered_transitions
    if missing_transitions:
        warnings.append(CompilationWarning(
            stage="S4",
            code="TRANSITION_MATRIX_INCOMPLETE",
            message=(
                f"Transition matrix missing {len(missing_transitions)} transition(s) "
                f"declared in contract allowed_next_phases"
            ),
            context={"missing": sorted(f"{f}->{t}" for f, t in missing_transitions)},
        ))

    return errors, warnings


# ---------------------------------------------------------------------------
# S5 — COMPILE_PROMPTS (advisory)
# ---------------------------------------------------------------------------

def _compile_prompts(resolved: ResolvedBundle) -> list[CompilationWarning]:
    """S5: Advisory prompt coverage check.

    Uses string.lower() mention check — intentionally surface-level.
    All results are CompilationWarning (never CompilationError).
    String mention != semantic correctness. See design doc Stage 5 + Q4.
    """
    warnings: list[CompilationWarning] = []

    for subtype_key, contract in resolved.subtype_to_contract.items():
        prompt_lower = contract.get("prompt", "").lower()

        # Skip contracts with no prompt — nothing to check
        if not prompt_lower:
            continue

        policy = contract.get("policy", {})
        cognition_spec = contract.get("cognition_spec", {})

        # Check required_fields mentioned in prompt
        for field in policy.get("required_fields", []):
            if field.lower() not in prompt_lower:
                warnings.append(CompilationWarning(
                    stage="S5",
                    code="PROMPT_MISSING_FIELD_MENTION",
                    message=(
                        f"Contract '{subtype_key}' prompt does not mention "
                        f"required field '{field}'"
                    ),
                    context={"subtype": subtype_key, "field": field},
                ))

        # Check required_principals mentioned in prompt
        for principal in policy.get("required_principals", []):
            if principal.lower() not in prompt_lower:
                warnings.append(CompilationWarning(
                    stage="S5",
                    code="PROMPT_MISSING_PRINCIPAL_MENTION",
                    message=(
                        f"Contract '{subtype_key}' prompt does not mention "
                        f"required principal '{principal}'"
                    ),
                    context={"subtype": subtype_key, "principal": principal},
                ))

        # Check forbidden_moves mentioned in prompt
        for move in policy.get("forbidden_moves", []):
            if move.lower() not in prompt_lower:
                warnings.append(CompilationWarning(
                    stage="S5",
                    code="PROMPT_MISSING_FORBIDDEN_MENTION",
                    message=(
                        f"Contract '{subtype_key}' prompt does not mention "
                        f"forbidden move '{move}'"
                    ),
                    context={"subtype": subtype_key, "forbidden_move": move},
                ))

        # Check success_criteria mentioned in prompt
        for criterion in cognition_spec.get("success_criteria", []):
            if criterion.lower() not in prompt_lower:
                warnings.append(CompilationWarning(
                    stage="S5",
                    code="PROMPT_MISSING_CRITERIA_MENTION",
                    message=(
                        f"Contract '{subtype_key}' prompt does not mention "
                        f"success criterion '{criterion}'"
                    ),
                    context={"subtype": subtype_key, "criterion": criterion},
                ))

    return warnings


# ---------------------------------------------------------------------------
# S6 — COMPILE_VALIDATORS
# ---------------------------------------------------------------------------

def _compile_validators(resolved: ResolvedBundle) -> dict[str, CompiledValidator]:
    """S6: Build one frozen CompiledValidator per phase from resolved bundle contracts.

    Returns dict keyed by phase (e.g. "ANALYZE", "EXECUTE").
    Phases without contracts (e.g. "UNDERSTAND") are not included.
    """
    validators: dict[str, CompiledValidator] = {}

    for subtype, contract in resolved.subtype_to_contract.items():
        phase = contract.get("phase", "").upper()
        if not phase:
            continue

        policy = contract.get("policy", {})
        principals_list = contract.get("principals", [])

        # Build lifecycle frozensets from per-principal flags
        inference_eligible: frozenset[str] = frozenset(
            p["name"] for p in principals_list
            if isinstance(p, dict) and p.get("inference_rule_exists")
        )
        fake_check_eligible: frozenset[str] = frozenset(
            p["name"] for p in principals_list
            if isinstance(p, dict) and p.get("fake_check_eligible")
        )

        # Build per-principal requirement maps
        semantic_checks: dict[str, tuple[str, ...]] = {}
        requires_fields_per_principal: dict[str, tuple[str, ...]] = {}
        for p in principals_list:
            if not isinstance(p, dict):
                continue
            name = p.get("name", "")
            if not name:
                continue
            semantic_checks[name] = tuple(p.get("semantic_checks", []))
            requires_fields_per_principal[name] = tuple(p.get("required_evidence_fields", []))

        validators[phase] = CompiledValidator(
            phase=phase,
            subtype=subtype,
            required_fields=tuple(policy.get("required_fields", [])),
            required_principals=tuple(policy.get("required_principals", [])),
            forbidden_principals=tuple(policy.get("forbidden_principals", [])),
            forbidden_moves=tuple(policy.get("forbidden_moves", [])),
            semantic_checks=semantic_checks,
            requires_fields_per_principal=requires_fields_per_principal,
            inference_eligible=inference_eligible,
            fake_check_eligible=fake_check_eligible,
        )

    return validators


# ---------------------------------------------------------------------------
# S7 — COMPILE_ROUTES
# ---------------------------------------------------------------------------

def _compile_retry_router(resolved: ResolvedBundle) -> CompiledRetryRouter:
    """S7: Pre-compile the failure→route lookup table from routing.principal_routes
    and repair_templates.

    repair_template is resolved at compile time — missing repair template for a
    required principal is caught here, not at runtime.
    """
    routes: dict[tuple[str, str], CompiledRoute] = {}
    default_routes: dict[str, CompiledRoute] = {}

    for subtype, contract in resolved.subtype_to_contract.items():
        phase = contract.get("phase", "").upper()
        routing_raw = contract.get("routing", {}).get("principal_routes", {})
        repair_templates = contract.get("repair_templates", {})

        if not isinstance(routing_raw, dict):
            routing_raw = {}
        if not isinstance(repair_templates, dict):
            repair_templates = {}

        for principal, route_data in routing_raw.items():
            # Pre-resolve repair template at compile time
            repair = repair_templates.get(principal, "")
            routes[(phase, principal)] = CompiledRoute(
                failure_principal=principal,
                next_phase=route_data.get("next_phase", ""),
                strategy=route_data.get("strategy", ""),
                repair_template=repair,
            )

        # Default route: catch-all if no specific principal route matches
        default_raw = contract.get("routing", {}).get("default_route", {})
        if default_raw and isinstance(default_raw, dict):
            default_routes[phase] = CompiledRoute(
                failure_principal="__default__",
                next_phase=default_raw.get("next_phase", ""),
                strategy=default_raw.get("strategy", ""),
                repair_template=default_raw.get("repair_template", ""),
            )

    return CompiledRetryRouter(routes=routes, default_routes=default_routes)


def get_route(
    router: CompiledRetryRouter,
    phase: str,
    principal: str,
) -> "CompiledRoute | None":
    """Look up a compiled route by (phase, principal).

    Falls back to the default route for the phase if no specific route exists.
    Returns None if neither a specific nor a default route is found.
    """
    return router.routes.get((phase, principal)) or router.default_routes.get(phase)


# ---------------------------------------------------------------------------
# S8 — ACTIVATION REPORT
# ---------------------------------------------------------------------------

_ALLOWED_MISSING_PHASES: frozenset[str] = frozenset({"UNDERSTAND"})


def _build_activation_report(
    resolved: ResolvedBundle,
    validators: dict[str, "CompiledValidator"],
    fatal_errors: list[CompilationError],
    warnings: list[CompilationWarning],
) -> ActivationReport:
    """S8: Build the activation proof report summarising the full compilation."""

    # Principal counts (summed across all contracts)
    principals_total = sum(
        len(contract.get("principals", []))
        for contract in resolved.subtype_to_contract.values()
    )
    principals_inference = sum(
        len(v.inference_eligible) for v in validators.values()
    )
    principals_fake = sum(
        len(v.fake_check_eligible) for v in validators.values()
    )

    return ActivationReport(
        activation_ok=len(fatal_errors) == 0,
        bundle_version=resolved.raw.get("version", ""),
        compiler_version=resolved.raw.get("compiler_version", ""),
        generated_at=resolved.raw.get("generated_at", ""),
        generator_commit=resolved.raw.get("generator_commit", ""),

        phases_compiled=sorted(resolved.phases_with_contracts),
        phases_missing=sorted(resolved.phases_without_contracts - _ALLOWED_MISSING_PHASES),
        phases_allowed_missing=sorted(resolved.phases_without_contracts & _ALLOWED_MISSING_PHASES),

        contracts_compiled=len(resolved.subtype_to_contract),
        principals_total=principals_total,
        principals_inference_eligible=principals_inference,
        principals_fake_check_eligible=principals_fake,

        completeness_errors=[str(e) for e in fatal_errors if e.stage == "S3"],
        consistency_errors=[str(e) for e in fatal_errors if e.stage == "S4"],
        prompt_warnings=[str(w) for w in warnings if w.stage == "S5"],

        transition_matrix_complete=not any(
            w.code == "TRANSITION_MATRIX_INCOMPLETE" for w in warnings
        ),
        routing_coverage_complete=not any(
            e.code == "ROUTING_COVERAGE_MISSING" for e in fatal_errors
        ),
    )


# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------

_cached_bundle: "CompiledBundle | None" = None


# ---------------------------------------------------------------------------
# compile_bundle — single public entry point (P4a)
# ---------------------------------------------------------------------------

def _compile_bundle_uncached(path: "str | None") -> "CompiledBundle":
    """Run all 8 stages and return a CompiledBundle. Raises CompilationError on fatal errors."""
    bundle_path = path or os.environ.get("JINGU_BUNDLE_PATH", DEFAULT_BUNDLE_PATH)

    # S1: Parse
    parsed = _parse_bundle(bundle_path)

    # S2: Resolve references
    resolved = _resolve_refs(parsed)

    # S3: Completeness (returns errors list — never raises)
    completeness_errors = _check_completeness(resolved)

    # S4: Consistency (returns errors + warnings — never raises)
    consistency_errors, s4_warnings = _check_consistency(resolved)

    fatal_errors = completeness_errors + consistency_errors

    # S5: Compile prompts (warnings only)
    prompt_warnings = _compile_prompts(resolved)

    all_warnings = s4_warnings + prompt_warnings

    # Fail fast: any fatal error aborts compilation
    if fatal_errors:
        raise CompilationError(
            stage="COMPILE",
            code="FATAL_ERRORS",
            message=f"{len(fatal_errors)} fatal error(s) during compilation",
            context={"errors": [str(e) for e in fatal_errors]},
        )

    # S6: Compile validators
    validators = _compile_validators(resolved)

    # S7: Compile retry router
    retry_router = _compile_retry_router(resolved)

    # S8: Activation report
    activation_report = _build_activation_report(resolved, validators, fatal_errors, all_warnings)

    # Build governance from compiled data (p224-09).
    # Lazy import avoids circular dependency: jingu_onboard imports nothing from
    # bundle_compiler, but bundle_compiler now calls into jingu_onboard.
    from jingu_onboard import _build_governance_from_compiled
    governance = _build_governance_from_compiled(resolved, validators)

    return CompiledBundle(
        governance=governance,
        activation_report=activation_report,
        validators=validators,
        retry_router=retry_router,
    )


def compile_bundle(path: "str | None" = None, *, force_reload: bool = False) -> CompiledBundle:
    """8-stage bundle compiler. Single public entry point (P4a).

    Returns CompiledBundle iff ALL fatal checks pass.
    Raises CompilationError on ANY fatal failure — there is no middle state.

    Results are cached at module level. Pass force_reload=True to recompile.
    """
    global _cached_bundle
    if _cached_bundle is not None and not force_reload:
        return _cached_bundle
    _cached_bundle = _compile_bundle_uncached(path)
    return _cached_bundle
