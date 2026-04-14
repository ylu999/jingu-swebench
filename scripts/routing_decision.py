"""Typed routing decision for admission and failure routing (EF-5).

Unifies in-loop routing (principal_gate) and between-attempt routing (failure_classifier)
into a single RoutingDecision object.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class AdmissionStatus(str, Enum):
    ADMITTED = "ADMITTED"
    RETRYABLE = "RETRYABLE"
    REJECTED = "REJECTED"
    ESCALATED = "ESCALATED"


class EscalationReason(str, Enum):
    CONTRACT_BUG = "contract_bug"       # same (phase, reason) N times
    FAKE_LOOP = "fake_loop"             # same fake principal N times


@dataclass(frozen=True)
class EscalationInfo:
    """Structured escalation metadata returned when admission status is ESCALATED.

    Attributes:
        reason: Why escalation was triggered (contract bug or fake loop).
        loop_key: The (phase, violation_code) tuple that exceeded the threshold.
        loop_count: How many times this loop_key was seen consecutively.
        action: What the system should do — "bypass" (force admit) or "selective_bypass".
        bypassed_principals: Principals bypassed (populated for FAKE_LOOP).
    """
    reason: EscalationReason
    loop_key: tuple                     # (phase, violation_code)
    loop_count: int
    action: str                         # "bypass" | "selective_bypass"
    bypassed_principals: list = field(default_factory=list)


@dataclass(frozen=True)
class RoutingDecision:
    """Where to redirect on admission failure or between-attempt failure routing.

    Attributes:
        next_phase: Target phase for retry/redirect (e.g. "OBSERVE", "DESIGN", "ANALYZE").
        strategy: Routing strategy name (from bundle routing or failure classifier).
        repair_hints: Repair hints for the agent (from bundle repair_templates or failure record).
        source: Origin of this routing decision.
            "principal_route"    — from principal gate violation routing
            "default_route"     — from bundle default routing
            "failure_type_route" — from failure_classifier FailureType routing
            "failure_layer_route" — from failure_classifier FailureRecord routing
    """
    next_phase: str             # e.g. "OBSERVE", "DESIGN"
    strategy: str               # from bundle routing or failure classifier
    repair_hints: list[str] = field(default_factory=list)  # from bundle repair_templates
    source: str = "default_route"  # "principal_route" | "default_route" | "failure_type_route" | "failure_layer_route"
