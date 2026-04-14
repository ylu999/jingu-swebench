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
from cognition_contracts import decision_fix_direction as decision_fix_direction  # noqa: F401
from cognition_contracts import design_solution_shape as design_solution_shape  # noqa: F401
from cognition_contracts import execution_code_patch as execution_code_patch  # noqa: F401
from cognition_contracts import implementation_governance as implementation_governance  # noqa: F401
from cognition_contracts import judge_verification as judge_verification  # noqa: F401
from cognition_contracts import observation_fact_gathering as observation_fact_gathering  # noqa: F401
