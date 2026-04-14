"""
drift_audit.py — Layer-alignment drift detection for bundle contracts.

Detects when gate fields, schema fields, prompt fields, extractor fields,
and policy principals drift out of sync across the 7+ contract layers.

5 checks:
  1. gate_fields_subset_schema — every field the gate checks must exist in schema
  2. schema_fields_subset_prompt — every schema field should have description
  3. prompt_fields_subset_schema — prompt should not describe non-schema fields (warning)
  4. extractor_fields_subset_record — every extracted field must exist in PhaseRecord
  5. policy_principals_subset_contract — policy principals must be in contract principals
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields as dataclass_fields

from phase_record import PhaseRecord


@dataclass
class DriftViolation:
    """A single layer-alignment violation detected by drift audit."""
    check_name: str       # e.g. "gate_fields_subset_schema"
    layer_a: str          # "gate" | "schema" | "prompt" | "extractor" | "record" | "policy"
    layer_b: str
    item: str             # field name or principal name
    violation_type: str   # "missing_in_b" | "extra_in_b" | "mismatch"
    detail: str


# ---------------------------------------------------------------------------
# Known field name mappings (gate/policy layer -> schema layer)
# Some gate fields use different names than schema properties.
# ---------------------------------------------------------------------------
_FIELD_NAME_MAPPINGS: dict[str, str] = {
    # Add mappings here as drift is resolved, e.g.:
    # "evidence": "evidence_refs",
}

# Universal fields present in all schemas (excluded from some checks)
_UNIVERSAL_FIELDS = {"phase", "subtype", "principals"}

# Fields that are stored in PhaseRecord.content rather than named attrs
_CONTENT_STORED_FIELDS = {"content"}


def _get_schema_fields(contract: dict) -> set[str]:
    """Extract property keys from contract schema."""
    schema = contract.get("schema", {})
    return set(schema.get("properties", {}).keys())


def _get_policy_required_fields(contract: dict) -> set[str]:
    """Extract required_fields from contract policy."""
    policy = contract.get("policy", {})
    return set(policy.get("required_fields", []))


def _get_principal_names(contract: dict) -> set[str]:
    """Extract principal names from contract principals array."""
    principals = contract.get("principals", [])
    return {p["name"] for p in principals if isinstance(p, dict) and "name" in p}


def _get_policy_required_principals(contract: dict) -> set[str]:
    """Extract required_principals from contract policy."""
    policy = contract.get("policy", {})
    return set(policy.get("required_principals", []))


def _map_field_name(name: str) -> str:
    """Apply known field name mappings."""
    return _FIELD_NAME_MAPPINGS.get(name, name)


# ---------------------------------------------------------------------------
# Check 1: gate_fields_subset_schema
# ---------------------------------------------------------------------------
def check_gate_fields_subset_schema(
    subtype: str, contract: dict
) -> list[DriftViolation]:
    """Every field the gate/policy checks must exist in the schema.

    Gate fields come from policy.required_fields.
    Also checks principal requires_fields against schema.
    """
    violations: list[DriftViolation] = []
    schema_fields = _get_schema_fields(contract)
    policy_required = _get_policy_required_fields(contract)

    # Check policy.required_fields against schema properties
    for fld in policy_required:
        mapped = _map_field_name(fld)
        if mapped not in schema_fields:
            violations.append(DriftViolation(
                check_name="gate_fields_subset_schema",
                layer_a="gate",
                layer_b="schema",
                item=fld,
                violation_type="missing_in_b",
                detail=(
                    f"[{subtype}] policy.required_fields contains '{fld}' "
                    f"but schema.properties does not define it"
                ),
            ))

    # Check principal requires_fields against schema properties
    for principal in contract.get("principals", []):
        if not isinstance(principal, dict):
            continue
        p_name = principal.get("name", "unknown")
        for fld in principal.get("requires_fields", []):
            mapped = _map_field_name(fld)
            if mapped not in schema_fields:
                violations.append(DriftViolation(
                    check_name="gate_fields_subset_schema",
                    layer_a="gate",
                    layer_b="schema",
                    item=fld,
                    violation_type="missing_in_b",
                    detail=(
                        f"[{subtype}] principal '{p_name}' requires_fields "
                        f"contains '{fld}' but schema.properties does not define it"
                    ),
                ))

    return violations


# ---------------------------------------------------------------------------
# Check 2: schema_fields_subset_prompt
# ---------------------------------------------------------------------------
def check_schema_fields_subset_prompt(
    subtype: str, contract: dict
) -> list[DriftViolation]:
    """Every substantive schema field should have a description.

    Fields with schema descriptions are rendered at runtime via
    schema_field_guidance, so we check that each non-universal schema
    field has a description defined in the schema itself.
    """
    violations: list[DriftViolation] = []
    schema = contract.get("schema", {})
    properties = schema.get("properties", {})

    for fld_name, fld_spec in properties.items():
        if fld_name in _UNIVERSAL_FIELDS:
            continue
        desc = fld_spec.get("description", "")
        if not desc or not desc.strip():
            violations.append(DriftViolation(
                check_name="schema_fields_subset_prompt",
                layer_a="schema",
                layer_b="prompt",
                item=fld_name,
                violation_type="missing_in_b",
                detail=(
                    f"[{subtype}] schema field '{fld_name}' has no description; "
                    f"prompt renderer cannot generate guidance for it"
                ),
            ))

    return violations


# ---------------------------------------------------------------------------
# Check 3: prompt_fields_subset_schema
# ---------------------------------------------------------------------------

# Heuristic patterns to extract field-like references from prompt text.
# Matches patterns like: "- field_name", "field_name field", "`field_name`"
_PROMPT_FIELD_PATTERN = re.compile(
    r"""
    (?:^|\s|`)                     # preceded by whitespace, start, or backtick
    ([a-z][a-z0-9_]{2,30})         # field-like identifier (3-31 chars, snake_case)
    (?:\s+field|\s*`|$)            # followed by "field", backtick, or end
    """,
    re.VERBOSE | re.MULTILINE,
)

# Additional pattern: "- field_name\n" or "- field_name " in Required Fields section
_PROMPT_REQUIRED_FIELD_PATTERN = re.compile(
    r"^-\s+([a-z][a-z0-9_]+)",
    re.MULTILINE,
)

# Words that look like field names but are not
_PROMPT_FALSE_POSITIVES = {
    "the", "and", "for", "not", "you", "must", "your", "that", "this",
    "from", "with", "have", "are", "was", "has", "been", "but", "can",
    "all", "each", "than", "more", "does", "into", "when", "how",
    "phase", "subtype", "principals",  # universal fields (always valid)
    "goal", "requirement", "checks", "performed", "violation", "detected",
    "declare", "ensure", "stay", "within", "scope", "current",
    "direction", "explicit", "tradeoffs", "choosing", "one",
    "production", "code", "patch", "design", "write", "new",
    "verification", "against", "success", "criteria", "report",
    "test", "results", "pass", "specific", "failures", "explained",
    "checked", "residual", "risks", "named",
    "fix", "compare", "options", "compared", "tradeoffs",
    "verified", "unknown", "unknowns", "calibrated", "confidence",
    "constraints", "satisfied", "invariants", "respected",
    "grounded", "admitted", "prior", "record",
    "minimal", "change", "smallest", "necessary", "remove",
    "unnecessary", "modifications", "satisfies",
    "root", "cause", "causal", "chain", "analysis",
    "observation", "fact", "evidence", "file", "line",
    "reference", "references", "output", "collected",
    "captured", "identified", "messages", "relevant",
    "files", "error", "reason",
    "scope", "bounded", "minimum", "surface", "area",
    "solution", "shape", "relax", "violate", "system",
    "any", "remaining", "even", "addressed",
    "outcome", "stated",
}


def check_prompt_fields_subset_schema(
    subtype: str, contract: dict
) -> list[DriftViolation]:
    """Prompt should not describe fields absent from schema.

    This is a heuristic check — false positives are expected.
    All violations are reported as warnings (violation_type="extra_in_b").
    """
    violations: list[DriftViolation] = []
    prompt = contract.get("prompt", "")
    schema_fields = _get_schema_fields(contract)

    if not prompt:
        return violations

    # Extract field-like tokens from prompt Required Fields section
    candidate_fields: set[str] = set()
    # Look for "## Required Fields" section
    req_section = re.search(
        r"## Required Fields\n(.*?)(?=\n##|\Z)",
        prompt,
        re.DOTALL,
    )
    if req_section:
        for m in _PROMPT_REQUIRED_FIELD_PATTERN.finditer(req_section.group(1)):
            candidate_fields.add(m.group(1))

    # Look for field references in success criteria (e.g., "root_cause field contains")
    for m in re.finditer(r"(\b[a-z][a-z0-9_]+)\s+field\b", prompt):
        candidate_fields.add(m.group(1))

    # Filter out false positives and known schema fields
    for candidate in candidate_fields:
        if candidate in _PROMPT_FALSE_POSITIVES:
            continue
        if candidate in schema_fields:
            continue
        # This is a field referenced in prompt but not in schema
        violations.append(DriftViolation(
            check_name="prompt_fields_subset_schema",
            layer_a="prompt",
            layer_b="schema",
            item=candidate,
            violation_type="extra_in_b",
            detail=(
                f"[{subtype}] prompt references field-like name '{candidate}' "
                f"but schema.properties does not define it (heuristic — may be false positive)"
            ),
        ))

    return violations


# ---------------------------------------------------------------------------
# Check 4: extractor_fields_subset_record
# ---------------------------------------------------------------------------

# Cache PhaseRecord field names
_PHASE_RECORD_FIELDS: set[str] | None = None


def _get_phase_record_fields() -> set[str]:
    """Get all field names from PhaseRecord dataclass."""
    global _PHASE_RECORD_FIELDS
    if _PHASE_RECORD_FIELDS is None:
        _PHASE_RECORD_FIELDS = {f.name for f in dataclass_fields(PhaseRecord)}
    return _PHASE_RECORD_FIELDS


def check_extractor_fields_subset_record(
    subtype: str, contract: dict
) -> list[DriftViolation]:
    """Every field the extractor populates must exist as a PhaseRecord attribute.

    Schema fields map to PhaseRecord attributes. If a schema defines a field
    that PhaseRecord doesn't have as a named attribute, the extractor cannot
    store it.
    """
    violations: list[DriftViolation] = []
    schema_fields = _get_schema_fields(contract)
    record_fields = _get_phase_record_fields()

    for fld in schema_fields:
        if fld in _CONTENT_STORED_FIELDS:
            continue
        if fld not in record_fields:
            violations.append(DriftViolation(
                check_name="extractor_fields_subset_record",
                layer_a="schema",
                layer_b="record",
                item=fld,
                violation_type="missing_in_b",
                detail=(
                    f"[{subtype}] schema defines field '{fld}' but PhaseRecord "
                    f"has no attribute named '{fld}'"
                ),
            ))

    return violations


# ---------------------------------------------------------------------------
# Check 5: policy_principals_subset_contract
# ---------------------------------------------------------------------------
def check_policy_principals_subset_contract(
    subtype: str, contract: dict
) -> list[DriftViolation]:
    """Policy principals must appear in contract principals array.

    Additionally checks:
    - repair_templates coverage for required_principals
    - routing.principal_routes coverage for required_principals
    """
    violations: list[DriftViolation] = []
    principal_names = _get_principal_names(contract)
    policy_required = _get_policy_required_principals(contract)
    repair_templates = set(contract.get("repair_templates", {}).keys())
    routing = contract.get("routing", {})
    principal_routes = set(routing.get("principal_routes", {}).keys())

    # Check required_principals subset of contract principals
    for p in policy_required:
        if p not in principal_names:
            violations.append(DriftViolation(
                check_name="policy_principals_subset_contract",
                layer_a="policy",
                layer_b="contract",
                item=p,
                violation_type="missing_in_b",
                detail=(
                    f"[{subtype}] policy.required_principals contains '{p}' "
                    f"but contract principals array does not include it"
                ),
            ))

    # Check repair_templates coverage
    for p in policy_required:
        if p not in repair_templates:
            violations.append(DriftViolation(
                check_name="policy_principals_subset_contract",
                layer_a="policy",
                layer_b="repair_templates",
                item=p,
                violation_type="missing_in_b",
                detail=(
                    f"[{subtype}] policy.required_principals contains '{p}' "
                    f"but repair_templates has no template for it"
                ),
            ))

    # Check routing.principal_routes coverage
    for p in policy_required:
        if p not in principal_routes:
            violations.append(DriftViolation(
                check_name="policy_principals_subset_contract",
                layer_a="policy",
                layer_b="routing",
                item=p,
                violation_type="missing_in_b",
                detail=(
                    f"[{subtype}] policy.required_principals contains '{p}' "
                    f"but routing.principal_routes has no route for it"
                ),
            ))

    return violations


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def audit_contract(subtype: str, contract: dict) -> list[DriftViolation]:
    """Run all 5 checks on a single contract section."""
    violations: list[DriftViolation] = []
    violations.extend(check_gate_fields_subset_schema(subtype, contract))
    violations.extend(check_schema_fields_subset_prompt(subtype, contract))
    violations.extend(check_prompt_fields_subset_schema(subtype, contract))
    violations.extend(check_extractor_fields_subset_record(subtype, contract))
    violations.extend(check_policy_principals_subset_contract(subtype, contract))
    return violations


def audit_all_contracts(bundle: dict) -> list[DriftViolation]:
    """Run all 5 checks on all contracts in bundle."""
    violations: list[DriftViolation] = []
    for subtype, contract in bundle.get("contracts", {}).items():
        violations.extend(audit_contract(subtype, contract))
    return violations


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    import sys

    bundle_path = sys.argv[1] if len(sys.argv) > 1 else "bundle.json"
    with open(bundle_path) as f:
        bundle = json.load(f)

    violations = audit_all_contracts(bundle)
    print(f"Drift audit: {len(violations)} violation(s) found\n")
    for v in violations:
        severity = "WARNING" if v.violation_type == "extra_in_b" else "ERROR"
        print(f"  [{severity}] {v.check_name}: {v.item}")
        print(f"    {v.detail}")
        print()

    sys.exit(1 if violations else 0)
