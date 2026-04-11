"""Bundle compiler — 8-stage pipeline. compile_bundle() is the single entry point."""

from __future__ import annotations

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
