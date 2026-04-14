"""
Shadow Contract Detector — finds field definitions outside authoritative contracts.

Four AST-based scans:
  1. Gate-private field definitions — dict/set literals defining field names not in any contract
  2. Regex semantic checks — re.compile/search/match/findall in gate files (P1 violation candidates)
  3. Prompt-only fields — field-name-like tokens in prompt strings not in schema properties
  4. Hardcoded principal names — principal name literals outside authoritative contract modules

Usage:
    from shadow_detector import scan_all
    violations = scan_all("/path/to/scripts")
    for v in violations:
        print(f"  {v.violation_type}: {v.item} in {v.file}:{v.line}")

Or as CLI:
    python -m shadow_detector /path/to/scripts
"""

from __future__ import annotations

import ast
import importlib
import os
import re
import sys
from dataclasses import dataclass, field


@dataclass
class ShadowContractViolation:
    """One detected shadow contract violation."""
    file: str
    line: int
    violation_type: str  # "gate_private_field" | "regex_semantic_check" | "prompt_only_field" | "hardcoded_principal"
    item: str
    detail: str


# ── Helpers ──────────────────────────────────────────────────────────────────

# Universal fields that appear in every schema (not shadow violations).
_UNIVERSAL_FIELDS = frozenset({
    "phase", "subtype", "principals", "content", "from_steps",
    "evidence_refs", "claims", "fix_type",
})

# Variable name patterns that indicate field-defining dicts/sets/lists.
_SHADOW_DICT_PATTERNS = re.compile(
    r'_FIELDS|_CONTRACT|_REQUIRED|_SCHEMA|_SPECS|_PROPERTIES',
    re.IGNORECASE,
)

# Authorized consumers for principal names (may reference but not define).
_PRINCIPAL_AUTHORIZED_FILES = frozenset({
    "principal_gate.py",
    "principal_inference.py",
    "analyze_principal_metrics.py",
    "cognition_check.py",
    "cognition_schema.py",
    "subtype_contracts.py",
})

# Files/dirs to skip entirely.
_SKIP_DIRS = frozenset({"__pycache__", "cognition_contracts", ".git"})
_SKIP_FILES = frozenset({"shadow_detector.py"})


# ── Gather contract fields and principal names ───────────────────────────────

def _gather_contract_fields(scripts_dir: str) -> set[str]:
    """Collect all field names from cognition_contracts FIELD_SPECS + SCHEMA_PROPERTIES."""
    fields: set[str] = set()

    # Ensure scripts_dir is on sys.path for imports
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    try:
        # Import contract modules
        from cognition_contracts import analysis_root_cause
        for fs in getattr(analysis_root_cause, "FIELD_SPECS", []):
            fields.add(fs.name)
        for key in getattr(analysis_root_cause, "SCHEMA_PROPERTIES", {}):
            fields.add(key)
    except ImportError:
        pass

    # Add universal fields
    fields.update(_UNIVERSAL_FIELDS)
    return fields


def _gather_principal_names(scripts_dir: str) -> set[str]:
    """Collect all known principal names from subtype_contracts and cognition_contracts."""
    principals: set[str] = set()

    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    try:
        from cognition_contracts import analysis_root_cause
        principals.update(getattr(analysis_root_cause, "REQUIRED_PRINCIPALS", []))
        principals.update(getattr(analysis_root_cause, "EXPECTED_PRINCIPALS", []))
        principals.update(getattr(analysis_root_cause, "FORBIDDEN_PRINCIPALS", []))
    except ImportError:
        pass

    try:
        from subtype_contracts import SUBTYPE_CONTRACTS
        for contract in SUBTYPE_CONTRACTS.values():
            principals.update(contract.get("required_principals", []))
            principals.update(contract.get("expected_principals", []))
            principals.update(contract.get("forbidden_principals", []))
    except ImportError:
        pass

    return principals


# ── Scan 1: Gate-private field definitions ───────────────────────────────────

def scan_gate_private_fields(
    filepath: str,
    contract_fields: set[str],
) -> list[ShadowContractViolation]:
    """
    Scan for dict/set/list literals that define field names not in any contract.

    Targets: *_gate.py, gate_rejection.py
    Looks for: assignments to variables matching _FIELDS, _CONTRACT, _REQUIRED, etc.
    whose dict keys or list/set string elements are not in contract_fields.
    """
    violations: list[ShadowContractViolation] = []
    try:
        with open(filepath) as f:
            source = f.read()
        tree = ast.parse(source, filename=filepath)
    except (SyntaxError, OSError):
        return violations

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if not _SHADOW_DICT_PATTERNS.search(target.id):
                continue

            # Extract string keys from dict literal
            if isinstance(node.value, ast.Dict):
                for key in node.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        if key.value not in contract_fields:
                            violations.append(ShadowContractViolation(
                                file=filepath,
                                line=key.lineno,
                                violation_type="gate_private_field",
                                item=key.value,
                                detail=(
                                    f"Field '{key.value}' defined in variable "
                                    f"'{target.id}' but not in any contract FIELD_SPECS"
                                ),
                            ))
            # Extract string elements from set/list literals
            elif isinstance(node.value, (ast.Set, ast.List)):
                for elt in node.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        if elt.value not in contract_fields:
                            violations.append(ShadowContractViolation(
                                file=filepath,
                                line=elt.lineno,
                                violation_type="gate_private_field",
                                item=elt.value,
                                detail=(
                                    f"Field '{elt.value}' in variable "
                                    f"'{target.id}' but not in any contract FIELD_SPECS"
                                ),
                            ))

    return violations


# ── Scan 2: Regex semantic checks ────────────────────────────────────────────

def scan_regex_semantic_checks(filepath: str) -> list[ShadowContractViolation]:
    """
    Find regex patterns used in gate files.

    Reports all re.compile/search/match/findall calls as candidates for
    structure-over-surface (P1) review. Not all are violations — code ref
    detection patterns are structural (OK), but semantic content matching
    (checking for keywords like "because", "hypothesis", "invariant") is a
    P1 violation candidate.
    """
    violations: list[ShadowContractViolation] = []
    try:
        with open(filepath) as f:
            source = f.read()
        tree = ast.parse(source, filename=filepath)
    except (SyntaxError, OSError):
        return violations

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func = node.func

        # re.compile(...), re.search(...), etc.
        if isinstance(func, ast.Attribute) and func.attr in (
            "compile", "search", "match", "findall",
        ):
            if isinstance(func.value, ast.Name) and func.value.id == "re":
                # Try to extract the pattern string for detail
                pattern_str = "<dynamic>"
                if node.args and isinstance(node.args[0], ast.Constant):
                    raw = node.args[0].value
                    if isinstance(raw, str):
                        pattern_str = raw[:80] + ("..." if len(raw) > 80 else "")

                violations.append(ShadowContractViolation(
                    file=filepath,
                    line=node.lineno,
                    violation_type="regex_semantic_check",
                    item=f"re.{func.attr}()",
                    detail=(
                        f"Regex call at line {node.lineno}: "
                        f"pattern={pattern_str!r} — "
                        f"verify if semantic (P1 violation) or structural (OK)"
                    ),
                ))

    return violations


# ── Scan 3: Prompt-only fields ───────────────────────────────────────────────

# Pattern: tokens that look like field names in prompt strings (snake_case words).
_FIELD_NAME_PATTERN = re.compile(r'\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b')

# Common false positives — not field names.
_PROMPT_FIELD_IGNORE = frozenset({
    "root_cause", "causal_chain", "evidence_refs",  # known contract fields
    "phase_record", "phase_prompt", "phase_schemas",  # module names
    "submit_phase_record",  # tool name
    "file_path", "line_number",  # generic terms
    "do_not", "must_not", "should_not", "can_not",  # negation patterns
    "single_source", "source_of",  # common phrases
    "re_compile", "re_search",  # code patterns
})


def scan_prompt_only_fields(
    filepath: str,
    contract_fields: set[str],
) -> list[ShadowContractViolation]:
    """
    Extract field-name-like tokens from prompt string constants.

    Targets: files with 'prompt' in name.
    Reports tokens that look like schema fields but are not in any
    contract SCHEMA_PROPERTIES — potential prompt-only contracts.
    """
    violations: list[ShadowContractViolation] = []
    try:
        with open(filepath) as f:
            source = f.read()
        tree = ast.parse(source, filename=filepath)
    except (SyntaxError, OSError):
        return violations

    # Find all string constants in the file (prompt templates)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        text = node.value
        # Only check substantial strings (prompt-like)
        if len(text) < 30:
            continue

        # Extract snake_case tokens
        candidates = _FIELD_NAME_PATTERN.findall(text)
        for candidate in candidates:
            if candidate in _PROMPT_FIELD_IGNORE:
                continue
            if candidate in contract_fields:
                continue
            # Filter out very common coding patterns
            if candidate.startswith(("_", "re_", "os_", "sys_")):
                continue
            # Only report if it looks like a field name (not a module or function)
            # Heuristic: must NOT be an import or module reference
            if any(prefix in candidate for prefix in ("__", "test_", "check_", "build_")):
                continue

            violations.append(ShadowContractViolation(
                file=filepath,
                line=node.lineno,
                violation_type="prompt_only_field",
                item=candidate,
                detail=(
                    f"Token '{candidate}' appears in prompt string "
                    f"but is not in any contract SCHEMA_PROPERTIES"
                ),
            ))

    return violations


# ── Scan 4: Hardcoded principal names ────────────────────────────────────────

def scan_hardcoded_principals(
    filepath: str,
    principal_names: set[str],
) -> list[ShadowContractViolation]:
    """
    Find hardcoded principal name string literals outside cognition_contracts/.

    Authorized consumers (principal_gate.py, principal_inference.py, etc.)
    are exempt — they consume names for enforcement, not re-definition.
    """
    basename = os.path.basename(filepath)
    if basename in _PRINCIPAL_AUTHORIZED_FILES:
        return []

    violations: list[ShadowContractViolation] = []
    try:
        with open(filepath) as f:
            source = f.read()
        tree = ast.parse(source, filename=filepath)
    except (SyntaxError, OSError):
        return violations

    if not principal_names:
        return violations

    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        val = node.value.strip()
        if val in principal_names:
            violations.append(ShadowContractViolation(
                file=filepath,
                line=node.lineno,
                violation_type="hardcoded_principal",
                item=val,
                detail=(
                    f"Principal name '{val}' hardcoded at line {node.lineno} — "
                    f"should be derived from cognition_contracts or subtype_contracts"
                ),
            ))

    return violations


# ── Orchestration ────────────────────────────────────────────────────────────

def scan_file(
    filepath: str,
    contract_fields: set[str],
    principal_names: set[str],
) -> list[ShadowContractViolation]:
    """Scan a single file for shadow contracts."""
    violations: list[ShadowContractViolation] = []
    basename = os.path.basename(filepath)

    # Scan 1 + 2: gate files
    if basename.endswith("_gate.py") or basename == "gate_rejection.py":
        violations.extend(scan_gate_private_fields(filepath, contract_fields))
        violations.extend(scan_regex_semantic_checks(filepath))

    # Scan 3: prompt files
    if "prompt" in basename:
        violations.extend(scan_prompt_only_fields(filepath, contract_fields))

    # Scan 4: hardcoded principals (all .py files except contracts dir)
    violations.extend(scan_hardcoded_principals(filepath, principal_names))

    return violations


def scan_all(scripts_dir: str) -> list[ShadowContractViolation]:
    """
    Scan all .py files in scripts_dir for shadow contracts.

    Gathers contract fields and principal names from cognition_contracts
    and subtype_contracts, then runs all 4 scans across applicable files.

    Returns list of ShadowContractViolation sorted by (file, line).
    """
    contract_fields = _gather_contract_fields(scripts_dir)
    principal_names = _gather_principal_names(scripts_dir)

    violations: list[ShadowContractViolation] = []

    for root, dirs, files in os.walk(scripts_dir):
        # Skip excluded directories
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]

        for f in sorted(files):
            if not f.endswith(".py"):
                continue
            if f in _SKIP_FILES:
                continue
            filepath = os.path.join(root, f)
            violations.extend(scan_file(filepath, contract_fields, principal_names))

    # Sort by file then line
    violations.sort(key=lambda v: (v.file, v.line))
    return violations


# ── CLI entry point ──────────────────────────────────────────────────────────

def main() -> None:
    """CLI entry point: python -m shadow_detector [scripts_dir]"""
    scripts_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(__file__)
    results = scan_all(scripts_dir)

    # Group by violation type for summary
    by_type: dict[str, list[ShadowContractViolation]] = {}
    for v in results:
        by_type.setdefault(v.violation_type, []).append(v)

    print(f"Shadow Contract Detector: {len(results)} violations found\n")

    for vtype in sorted(by_type):
        items = by_type[vtype]
        print(f"--- {vtype} ({len(items)}) ---")
        for v in items:
            relpath = os.path.relpath(v.file, scripts_dir)
            print(f"  {relpath}:{v.line}  {v.item}")
            print(f"    {v.detail}")
        print()


if __name__ == "__main__":
    main()
