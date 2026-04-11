"""Compute Routing Statistics from FailureEvents (p216 — Wave 3).

Aggregates FailureEvent objects by (phase, principal) pair and computes:
  - Total occurrence count
  - Outcome distribution (resolved / unresolved / regressed)
  - Success rate per next_phase choice
  - Average attempts to resolution

These statistics feed suggest_routing.py to generate the FailureMatrix.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from extract_failure_events import FailureEvent


def compute_routing_stats(events: list[FailureEvent]) -> dict[str, Any]:
    """Aggregate failure events by (phase, principal) pair.

    Args:
        events: list of FailureEvent objects from extraction pipeline

    Returns:
        Dict keyed by "PHASE:principal" with stats:
        {
            "ANALYZE:causal_grounding": {
                "count": 15,
                "outcomes": {"resolved": 5, "unresolved": 9, "regressed": 1},
                "resolution_rate": 0.333,
                "next_phase_stats": {
                    "ANALYZE": {"count": 10, "resolved": 3, "resolution_rate": 0.3},
                    "EXECUTE": {"count": 5, "resolved": 2, "resolution_rate": 0.4},
                },
                "reasons": {"missing_required": 10, "fake_declaration": 5},
                "avg_attempt": 1.5,
                "instances": ["django__django-11099", ...],
            },
            ...
        }
    """
    # Group events by (phase, principal)
    groups: dict[str, list[FailureEvent]] = defaultdict(list)
    for ev in events:
        key = f"{ev.phase}:{ev.principal}"
        groups[key].append(ev)

    stats: dict[str, Any] = {}
    for key, group_events in sorted(groups.items()):
        count = len(group_events)

        # Outcome distribution
        outcomes: dict[str, int] = defaultdict(int)
        for ev in group_events:
            outcomes[ev.outcome] += 1

        resolved_count = outcomes.get("resolved", 0)
        resolution_rate = resolved_count / count if count > 0 else 0.0

        # Next phase stats: for each next_phase, how often did it lead to resolution?
        next_phase_groups: dict[str, list[FailureEvent]] = defaultdict(list)
        for ev in group_events:
            next_phase_groups[ev.next_phase].append(ev)

        next_phase_stats: dict[str, dict] = {}
        for np, np_events in sorted(next_phase_groups.items()):
            np_resolved = sum(1 for ev in np_events if ev.outcome == "resolved")
            np_count = len(np_events)
            next_phase_stats[np] = {
                "count": np_count,
                "resolved": np_resolved,
                "resolution_rate": np_resolved / np_count if np_count > 0 else 0.0,
            }

        # Reason distribution
        reasons: dict[str, int] = defaultdict(int)
        for ev in group_events:
            reasons[ev.reason] += 1

        # Average attempt number
        avg_attempt = sum(ev.attempt for ev in group_events) / count if count > 0 else 0.0

        # Unique instances affected
        instances = sorted(set(ev.instance_id for ev in group_events))

        stats[key] = {
            "count": count,
            "outcomes": dict(outcomes),
            "resolution_rate": round(resolution_rate, 3),
            "next_phase_stats": next_phase_stats,
            "reasons": dict(reasons),
            "avg_attempt": round(avg_attempt, 2),
            "instances": instances,
        }

    return stats


def get_top_failures(
    stats: dict[str, Any],
    top_n: int = 10,
) -> list[tuple[str, dict]]:
    """Return the top-N most frequent failure patterns.

    Args:
        stats: output from compute_routing_stats
        top_n: number of top patterns to return

    Returns:
        List of (key, stats_dict) tuples sorted by count descending.
    """
    sorted_items = sorted(stats.items(), key=lambda x: x[1]["count"], reverse=True)
    return sorted_items[:top_n]


def get_best_next_phase(
    stats_entry: dict,
) -> tuple[str, float]:
    """Determine the best next_phase for a failure pattern.

    Selects the next_phase with the highest resolution rate,
    breaking ties by count (prefer more observations).

    Args:
        stats_entry: a single entry from compute_routing_stats output

    Returns:
        (best_next_phase, resolution_rate) tuple.
        Returns ("", 0.0) if no next_phase data available.
    """
    nps = stats_entry.get("next_phase_stats", {})
    if not nps:
        return ("", 0.0)

    best = max(
        nps.items(),
        key=lambda x: (x[1]["resolution_rate"], x[1]["count"]),
    )
    return (best[0], best[1]["resolution_rate"])
