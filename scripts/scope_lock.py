"""ScopeLockGate v0.1 — execution-level enforcement for near-miss/residual_gap repairs.

Three deterministic admission rules:
  Rule 1 (File lock):          a2_files subset of allowed_files
  Rule 2 (Size growth bound):  a2_total <= max(a1_total * 2, a1_total + 12)
  Rule 3 (Direction continuity): file overlap ratio >= 0.5 (Jaccard)

Rollout: Step 1 = near_miss only, Step 2 = residual_gap, Step 3 = escape hatch.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ScopeLockEnvelope:
    """Captures A1 patch state for scope constraint enforcement on A2."""
    touched_files: list[str]
    patch_additions: int
    patch_deletions: int
    patch_hunks: int
    failing_test_names: list[str]
    passing_test_names: list[str]
    failure_type: str  # "near_miss" | "residual_gap" | ...

    @property
    def patch_total(self) -> int:
        return self.patch_additions + self.patch_deletions

    @property
    def allowed_files(self) -> set[str]:
        return set(self.touched_files)

    @property
    def size_limit(self) -> int:
        return max(self.patch_total * 2, self.patch_total + 12)


@dataclass
class ScopeLockVerdict:
    """Gate output from evaluate_scope_lock."""
    admitted: bool
    violation_codes: list[str]
    observed: dict  # metrics for telemetry
    repair_hint: str  # agent-facing feedback on violation


# Failure types that trigger scope lock enforcement
_SCOPE_LOCK_FAILURE_TYPES = frozenset({"near_miss", "residual_gap"})


def build_scope_lock_envelope(
    patch_fp: dict,
    cv_result: dict,
    failure_type: str,
) -> Optional[ScopeLockEnvelope]:
    """Build envelope from A1 patch fingerprint and controlled_verify result.

    Args:
        patch_fp: output of patch_fingerprint() — {files, hunks, lines_added, lines_removed}
        cv_result: jingu_body["controlled_verify"] — {f2p_failing_names, p2p_failing_names, ...}
        failure_type: classified failure type from A1

    Returns:
        ScopeLockEnvelope if failure_type is scope-lockable, else None.
    """
    if failure_type not in _SCOPE_LOCK_FAILURE_TYPES:
        return None

    return ScopeLockEnvelope(
        touched_files=list(patch_fp.get("files", [])),
        patch_additions=patch_fp.get("lines_added", 0),
        patch_deletions=patch_fp.get("lines_removed", 0),
        patch_hunks=patch_fp.get("hunks", 0),
        failing_test_names=list(cv_result.get("f2p_failing_names", [])),
        passing_test_names=list(cv_result.get("p2p_failing_names", [])),
        failure_type=failure_type,
    )


def evaluate_scope_lock(
    envelope: ScopeLockEnvelope,
    a2_patch_fp: dict,
) -> ScopeLockVerdict:
    """Run 3 deterministic admission rules against A2 patch.

    Args:
        envelope: ScopeLockEnvelope built after A1 controlled_verify
        a2_patch_fp: patch_fingerprint() output for A2 patch

    Returns:
        ScopeLockVerdict with admission decision and violation codes.
    """
    violations: list[str] = []
    hints: list[str] = []

    a2_files = set(a2_patch_fp.get("files", []))
    a2_added = a2_patch_fp.get("lines_added", 0)
    a2_removed = a2_patch_fp.get("lines_removed", 0)
    a2_total = a2_added + a2_removed

    allowed = envelope.allowed_files
    size_limit = envelope.size_limit

    # Rule 1: File lock — a2_files must be subset of allowed_files
    new_files = a2_files - allowed
    if new_files:
        violations.append("scope_lock_new_files")
        hints.append(
            f"SCOPE LOCK VIOLATION: You introduced new file(s): {', '.join(sorted(new_files))}. "
            f"You MUST only modify: {', '.join(sorted(allowed))}."
        )

    # Rule 2: Size growth bound — a2_total <= max(a1_total * 2, a1_total + 12)
    if a2_total > size_limit:
        violations.append("scope_lock_patch_growth")
        hints.append(
            f"SCOPE LOCK VIOLATION: Patch grew to {a2_total} lines "
            f"(limit: {size_limit}, A1 was {envelope.patch_total}). "
            f"A near-miss repair must be SURGICAL — fix only the failing condition."
        )

    # Rule 3: Direction continuity — file overlap ratio >= 0.5 (Jaccard)
    if allowed and a2_files:
        intersection = allowed & a2_files
        union = allowed | a2_files
        overlap_ratio = len(intersection) / len(union) if union else 0.0
    elif not allowed and not a2_files:
        overlap_ratio = 1.0
    else:
        overlap_ratio = 0.0

    if overlap_ratio < 0.5:
        violations.append("scope_lock_direction_reset")
        hints.append(
            f"SCOPE LOCK VIOLATION: File overlap ratio {overlap_ratio:.0%} < 50%. "
            f"You changed direction too much. A1 files: {sorted(allowed)}, "
            f"A2 files: {sorted(a2_files)}. Stay focused on the same area."
        )

    # Build observed metrics for telemetry
    observed = {
        "a1_files": sorted(allowed),
        "a2_files": sorted(a2_files),
        "a1_total": envelope.patch_total,
        "a2_total": a2_total,
        "size_limit": size_limit,
        "new_files": sorted(new_files),
        "overlap_ratio": round(overlap_ratio, 3),
        "failure_type": envelope.failure_type,
    }

    admitted = len(violations) == 0
    repair_hint = "\n".join(hints) if hints else ""

    return ScopeLockVerdict(
        admitted=admitted,
        violation_codes=violations,
        observed=observed,
        repair_hint=repair_hint,
    )


def build_scope_lock_prompt_block(envelope: ScopeLockEnvelope) -> str:
    """Build the SCOPE LOCK constraint block for A2 repair prompt injection.

    This tells the agent upfront what the constraints are before it starts working.
    """
    return (
        "=== SCOPE LOCK (enforced) ===\n"
        f"Your previous attempt was classified as [{envelope.failure_type}].\n"
        f"You are in a SCOPE-LOCKED repair. The following constraints are HARD ENFORCED:\n\n"
        f"  1. FILE LOCK: You may ONLY modify these files: {', '.join(sorted(envelope.allowed_files))}\n"
        f"     Any write to other files will be REJECTED.\n\n"
        f"  2. SIZE LIMIT: Your patch must be <= {envelope.size_limit} lines changed "
        f"(A1 was {envelope.patch_total} lines). Larger patches will be REJECTED.\n\n"
        f"  3. DIRECTION: You must stay focused on the same code area. "
        f"Changing to completely different files will be REJECTED.\n\n"
        f"Focus on the EXACT failing condition. Make a surgical fix.\n"
        "=== END SCOPE LOCK ===\n"
    )
