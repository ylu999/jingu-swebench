# cognition_contracts — single source of truth for phase/subtype contracts.
# Each module defines ONE contract; all consumers derive from it.

from cognition_contracts._base import (  # noqa: F401
    ContractDefinition,
    FieldSpec,
    GateRule,
    build_field_spec_map,
    build_gate_required_fields,
    build_gate_rule_map,
    validate_contract_definition,
)
from cognition_contracts import analysis_root_cause as analysis_root_cause  # noqa: F401
from cognition_contracts import implementation_governance as implementation_governance  # noqa: F401
