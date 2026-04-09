"""
Governance Runtime — pack installer and execution engine.

Usage in run_with_jingu_gate.py:

    from governance_runtime import install_governance_pack, run_governance_packs
    from swebench_failure_reroute_pack import SWEBENCH_FAILURE_REROUTE_PACK

    # At module startup (once):
    install_governance_pack(SWEBENCH_FAILURE_REROUTE_PACK)

    # After each attempt (inside the attempt loop):
    ctx = ExecutionContext(
        jingu_body=jingu_body,
        fail_to_pass=fail_to_pass,
        attempt=attempt,
        instance_id=instance_id,
        patch_text=patch,
    )
    pack_decision = run_governance_packs(ctx)
    if pack_decision:
        retry_plan = override_retry_plan_from_pack(retry_plan, pack_decision)

Architecture (p27 ADR):
  - installGovernancePack() logs onboarding completeness at install time
  - run_governance_packs() iterates all installed packs, returns first RouteDecision
    with action=REROUTE (or None if all CONTINUE)
  - Packs run in installation order; first REROUTE wins
"""
from __future__ import annotations

from typing import Optional

from governance_pack import ExecutionContext, GovernancePack, RouteDecision  # re-exported for consumers
from retry_controller import RetryPlan


# ── Pack registry ─────────────────────────────────────────────────────────────

_INSTALLED_PACKS: list[GovernancePack] = []


def install_governance_pack(pack: GovernancePack) -> None:
    """
    Register a GovernancePack with the runtime.

    Logs onboarding completeness status at install time.
    In v0: warnings only. Future: missing required steps = OnboardingError.
    """
    status = pack.onboarding_status()
    missing = [step for step, present in status.items() if not present]

    if missing:
        print(
            f"[governance] WARNING: pack={pack.name} missing onboarding steps: {missing}. "
            f"Capability will be partially enforced."
        )
    else:
        print(f"[governance] pack={pack.name} fully onboarded (all 5 steps present)")

    _INSTALLED_PACKS.append(pack)


def get_installed_packs() -> list[GovernancePack]:
    return list(_INSTALLED_PACKS)


# ── Pack execution ─────────────────────────────────────────────────────────────

def run_governance_packs(ctx: ExecutionContext) -> Optional[RouteDecision]:
    """
    Run all installed packs against the execution context.

    Pipeline per pack:
      parse_failure(ctx) → FailureSignal
      recognize(signal)  → RecognitionResult
      route(recog, ctx)  → RouteDecision

    Returns the first RouteDecision with action=REROUTE, or None if all CONTINUE.
    """
    for pack in _INSTALLED_PACKS:
        if not pack.parse_failure or not pack.recognize or not pack.route:
            continue

        signal = pack.parse_failure(ctx)
        if signal is None:
            print(f"    [governance] pack={pack.name} no signal (parse_failure returned None)")
            continue

        recog = pack.recognize(signal)
        if recog is None:
            print(f"    [governance] pack={pack.name} no recognition for failure_type={signal.failure_type}")
            continue
        recog.pack_name = pack.name

        decision = pack.route(recog, ctx)
        if decision is None:
            continue
        decision.pack_name = pack.name

        print(
            f"    [governance] pack={pack.name} "
            f"failure_type={signal.failure_type} "
            f"state={recog.state} confidence={recog.confidence:.2f} "
            f"action={decision.action} target_phase={decision.target_phase or '-'}"
        )

        if decision.action == "REROUTE":
            return decision

    print(f"    [governance] all packs returned CONTINUE — no reroute")
    return None


# ── RetryPlan override helper ──────────────────────────────────────────────────

def override_retry_plan_from_pack(
    retry_plan: RetryPlan,
    decision: RouteDecision,
) -> RetryPlan:
    """
    Override a RetryPlan with a GovernancePack's RouteDecision.

    Preserves existing principal_violations.
    Sets control_action=ADJUST (not STOP — agent gets another attempt with new direction).
    """
    if not decision.hint:
        return retry_plan

    must_do: list[str]
    must_not_do: list[str]

    if decision.target_phase == "ANALYSIS":
        must_do = [
            "Re-read the failing FAIL_TO_PASS tests to understand expected behavior",
            "Identify the correct source location for the bug from first principles",
            "Write a patch that targets the root cause, not symptoms",
        ]
        must_not_do = [
            "Do not expand or continue the current patch direction",
            "Do not add workarounds or suppress test errors",
        ]
    elif decision.target_phase == "EXECUTION":
        must_do = [
            "Identify which test cases are still failing and why",
            "Extend the patch to cover the uncovered branches or cases",
        ]
        must_not_do = retry_plan.must_not_do
    else:
        must_do = retry_plan.must_do
        must_not_do = retry_plan.must_not_do

    updated = RetryPlan(
        root_causes=retry_plan.root_causes + [
            f"governance_pack={decision.pack_name}",
            f"target_phase={decision.target_phase}",
        ],
        must_do=must_do,
        must_not_do=must_not_do,
        validation_requirement=(
            f"Run FAIL_TO_PASS tests and confirm controlled_failed decreases"
        ),
        next_attempt_prompt=decision.hint[:600],
        control_action="ADJUST",
        principal_violations=retry_plan.principal_violations,
    )
    contains_hint = "[JINGU ROUTING]" in updated.next_attempt_prompt
    print(
        f"    [governance] reroute_applied=true "
        f"pack={decision.pack_name} "
        f"retry_plan_before={retry_plan.control_action} retry_plan_after=ADJUST "
        f"phase_before=EXECUTION phase_after={decision.target_phase} "
        f"routing_hint_present={contains_hint}"
    )
    return updated
