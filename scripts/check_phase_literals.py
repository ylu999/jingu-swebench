#!/usr/bin/env python3
"""Lint: detect hardcoded phase literal sets that should use ALL_PHASES.

Catches the pattern:
  {"ANALYZE", "DESIGN", "EXECUTE", ...}  # should be ALL_PHASES
  {"ANALYZE", "DECIDE", ...}             # should be ALL_PHASES

Does NOT flag:
  - Single phase strings in routing tables (legitimate config data)
  - Phase strings in print/log statements
  - canonical_symbols.py itself (the authoritative source)
  - Test assertions comparing to specific expected values

Only flags SET LITERALS containing 3+ phase names — these are almost certainly
re-definitions of the phase vocabulary that should reference ALL_PHASES.
"""

import ast
import sys
from pathlib import Path

CANONICAL_PHASES = {"UNDERSTAND", "OBSERVE", "ANALYZE", "DECIDE", "DESIGN", "EXECUTE", "JUDGE"}
SKIP_FILES = {"canonical_symbols.py", "check_phase_literals.py"}
MIN_PHASES_IN_SET = 3  # only flag sets with 3+ phases


def _find_parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    """Build child-id → parent map for context checking."""
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def check_file(path: Path) -> list[str]:
    """Return list of violation messages for a file."""
    if path.name in SKIP_FILES:
        return []
    try:
        source = path.read_text()
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return []

    parents = _find_parent_map(tree)
    violations = []
    for node in ast.walk(tree):
        # Check set literals: {str, str, ...}
        if isinstance(node, ast.Set):
            phase_elts = [
                e.value for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
                and e.value in CANONICAL_PHASES
            ]
            if len(phase_elts) >= MIN_PHASES_IN_SET:
                # Skip if used in subtraction (frozenset(X) - {phases}) — exclusion list
                parent = parents.get(id(node))
                if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Sub):
                    continue
                # Skip if used in comparison (assert X == {phases}) — test assertion
                if isinstance(parent, ast.Compare):
                    continue

                violations.append(
                    f"  {path}:{node.lineno}: set literal with {len(phase_elts)} phase names "
                    f"({', '.join(sorted(phase_elts))}). Use ALL_PHASES from canonical_symbols."
                )
    return violations


def main():
    scan_dirs = [Path("scripts"), Path("tests")]
    all_violations = []

    for d in scan_dirs:
        if not d.exists():
            continue
        for py_file in sorted(d.rglob("*.py")):
            all_violations.extend(check_file(py_file))

    if all_violations:
        print(f"[check-phase-literals] FAIL: {len(all_violations)} hardcoded phase set(s) found\n")
        for v in all_violations:
            print(v)
        print(f"\nFix: replace hardcoded phase sets with ALL_PHASES from canonical_symbols.py")
        sys.exit(1)
    else:
        print("[check-phase-literals] PASS: no hardcoded phase sets found")
        sys.exit(0)


if __name__ == "__main__":
    main()
