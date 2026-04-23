"""Design Admission Gate — in-loop hard validation for DESIGN phase records.

MVP: Before the agent runs freely in mini-swe-agent, a separate LLM call
generates a DesignRecord. This record is hard-validated. If it fails,
the attempt is marked as design_invalid and the agent never runs.

If it passes, the admitted design is injected as execution context,
constraining the agent's behavior during patch generation.

Feature flag: DESIGN_ADMISSION_ENABLED (env, default "0")
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional


# ── Feature flag ──────────────────────────────────────────────────────────

def is_design_admission_enabled() -> bool:
    return os.environ.get("DESIGN_ADMISSION_ENABLED", "0") == "1"


# ── DesignRecord dataclass ────────────────────────────────────────────────

@dataclass
class DesignRecord:
    phase: str = "DESIGN"
    target_files: list[str] = field(default_factory=list)
    solution_approach: str = ""
    scope_boundary: str = ""
    validation_plan: str = ""
    principals: list[str] = field(default_factory=list)
    raw_content: str = ""


@dataclass
class DesignAdmissionResult:
    admitted: bool = False
    record: Optional[DesignRecord] = None
    failure_reasons: list[str] = field(default_factory=list)
    repair_hint: str = ""


# ── Validation rules ─────────────────────────────────────────────────────

_WEAKENING_PATTERNS = re.compile(
    r"\b(loosen|relax|skip|bypass|ignore|broader|fallback.only|disable|remove.validation|"
    r"remove.check|turn.off|accept.all|accept.any|allow.all|allow.any)\b",
    re.IGNORECASE,
)

_REQUIRED_FIELDS = ["target_files", "solution_approach", "scope_boundary", "validation_plan"]
_REQUIRED_PRINCIPALS = {"causal_grounding", "minimal_change"}


def validate_design(record: DesignRecord) -> DesignAdmissionResult:
    """Hard-validate a DesignRecord. Returns admitted=True only if all rules pass."""
    result = DesignAdmissionResult(record=record)
    failures: list[str] = []

    # Rule 1: Required fields present and non-empty
    if not record.target_files:
        failures.append("MISSING_FIELD:target_files — must specify at least one target file")
    if not record.solution_approach or len(record.solution_approach.strip()) < 10:
        failures.append("MISSING_FIELD:solution_approach — must describe the fix approach (>=10 chars)")
    if not record.scope_boundary or len(record.scope_boundary.strip()) < 20:
        failures.append("MISSING_FIELD:scope_boundary — must define what is in/out of scope (>=20 chars)")
    if not record.validation_plan or len(record.validation_plan.strip()) < 10:
        failures.append("MISSING_FIELD:validation_plan — must describe how to verify the fix (>=10 chars)")

    # Rule 2: Bounded files (1-5)
    if record.target_files and len(record.target_files) > 5:
        failures.append(
            f"SCOPE_TOO_BROAD:target_files={len(record.target_files)} — "
            f"max 5 files allowed; narrow your approach"
        )

    # Rule 3: Required principals
    declared = {p.lower().strip() for p in record.principals}
    missing = _REQUIRED_PRINCIPALS - declared
    if missing:
        failures.append(
            f"MISSING_PRINCIPALS:{','.join(sorted(missing))} — "
            f"you must declare: {', '.join(sorted(_REQUIRED_PRINCIPALS))}"
        )

    # Rule 4: No obvious weakening
    for field_name in ("solution_approach", "scope_boundary"):
        text = getattr(record, field_name, "")
        match = _WEAKENING_PATTERNS.search(text)
        if match:
            failures.append(
                f"DESIGN_WEAKENING:{field_name} contains '{match.group()}' — "
                f"the design must preserve existing behavior, not weaken constraints"
            )

    # Rule 5: Actionable boundary (not generic platitudes)
    _generic = re.compile(
        r"^(fix.the.bug|make.minimal.changes|be.careful|fix.it|do.the.right.thing|"
        r"handle.the.issue|address.the.problem|resolve.the.bug)\s*\.?$",
        re.IGNORECASE,
    )
    if record.scope_boundary and _generic.match(record.scope_boundary.strip()):
        failures.append(
            "GENERIC_BOUNDARY:scope_boundary is too vague — "
            "must specify concrete files, functions, or behaviors that are in/out of scope"
        )

    result.failure_reasons = failures
    if failures:
        result.admitted = False
        result.repair_hint = build_repair_hint(failures)
    else:
        result.admitted = True

    return result


def build_repair_hint(failures: list[str]) -> str:
    """Build an actionable repair hint from validation failures."""
    lines = ["[DESIGN ADMISSION FAILED] Your design record was rejected. Fix these issues:\n"]
    for i, f in enumerate(failures, 1):
        lines.append(f"  {i}. {f}")
    lines.append(
        "\nResubmit a valid DESIGN record with all required fields before proceeding to code."
    )
    return "\n".join(lines)


# ── Design lock context (injected into agent prompt) ─────────────────────

def build_design_lock_context(record: DesignRecord) -> str:
    """Build the execution constraint context from an admitted design."""
    files_list = "\n".join(f"- {f}" for f in record.target_files)
    return (
        "=== ADMITTED DESIGN (HARD CONSTRAINTS) ===\n\n"
        f"Target files (you may ONLY modify these):\n{files_list}\n\n"
        f"Solution approach:\n{record.solution_approach}\n\n"
        f"Scope boundary:\n{record.scope_boundary}\n\n"
        f"Validation plan:\n{record.validation_plan}\n\n"
        "HARD CONSTRAINTS:\n"
        "1. You may ONLY modify the admitted target files listed above.\n"
        "2. Do not broaden, relax, skip, or bypass existing validation/check logic.\n"
        "3. Do not introduce new files unless absolutely necessary for the fix.\n"
        "4. Your patch must be consistent with the solution approach above.\n"
        "5. Run the validation plan before submitting.\n"
        "=== END ADMITTED DESIGN ==="
    )


# ── Design record generation prompt ─────────────────────────────────────

def build_design_prompt(instance: dict, previous_failure: str = "") -> str:
    """Build the prompt for the pre-execution DESIGN LLM call."""
    problem = instance.get("problem_statement", "")
    hints = instance.get("hints_text", "")
    repo = instance.get("repo", "")
    base_commit = instance.get("base_commit", "")

    parts = [
        "You are a software engineer about to fix a bug. Before writing any code, "
        "you must produce a DESIGN RECORD that will be validated.\n\n"
        "## Problem\n\n"
        f"{problem}\n\n"
    ]
    if hints:
        parts.append(f"## Hints\n\n{hints}\n\n")

    parts.append(
        f"## Repository\n\n"
        f"Repo: {repo}\n"
        f"Base commit: {base_commit}\n\n"
    )

    if previous_failure:
        parts.append(
            f"## Previous Attempt Feedback\n\n{previous_failure}\n\n"
        )

    parts.append(
        "## Required Output\n\n"
        "Respond with a JSON object (and nothing else) containing:\n\n"
        "```json\n"
        "{\n"
        '  "target_files": ["path/to/file1.py", "path/to/file2.py"],\n'
        '  "solution_approach": "Description of how you will fix the bug (>=10 chars)",\n'
        '  "scope_boundary": "What is in scope and out of scope for this fix (>=20 chars)",\n'
        '  "validation_plan": "How you will verify the fix works (>=10 chars)",\n'
        '  "principals": ["causal_grounding", "minimal_change"]\n'
        "}\n"
        "```\n\n"
        "Rules:\n"
        "- target_files: 1-5 files you plan to modify\n"
        "- solution_approach: must NOT contain weakening words (loosen, relax, skip, bypass, ignore, broader)\n"
        "- scope_boundary: must be specific (not generic like 'fix the bug carefully')\n"
        "- principals: must include at least 'causal_grounding' and 'minimal_change'\n"
    )
    return "".join(parts)


# ── Parse LLM response into DesignRecord ─────────────────────────────────

def parse_design_response(text: str) -> DesignRecord:
    """Parse the LLM's JSON response into a DesignRecord."""
    # Try to extract JSON from markdown code block or raw JSON
    json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if json_match:
        raw = json_match.group(1)
    else:
        # Try raw JSON
        raw = text.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return DesignRecord(raw_content=text)

    return DesignRecord(
        target_files=data.get("target_files", []),
        solution_approach=data.get("solution_approach", ""),
        scope_boundary=data.get("scope_boundary", ""),
        validation_plan=data.get("validation_plan", ""),
        principals=data.get("principals", []),
        raw_content=text,
    )
