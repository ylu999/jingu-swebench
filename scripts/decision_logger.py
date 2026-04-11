"""decision_logger — p230: decision provenance logger.

Logs every gate verdict, retry decision, phase advance, and early stop
to ``attempt_N/decisions.jsonl`` for offline analysis.

All logging is exception-safe — failures here must never crash a run.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class DecisionEvent:
    """A single decision event in the provenance log."""

    decision_type: str  # gate_verdict | retry_decision | phase_advance | early_stop | materialization_gate
    step_n: int
    timestamp_ms: float
    verdict: str
    rule_violated: str | None = None
    signals_evaluated: dict | None = None
    reason_text: str = ""
    phase_from: str | None = None
    phase_to: str | None = None


class DecisionLogger:
    """Append-only JSONL writer for decision events.

    One logger per attempt. Creates ``attempt_N/decisions.jsonl``.
    """

    def __init__(self, output_dir: Path, attempt: int) -> None:
        self._dir = output_dir / f"attempt_{attempt}"
        os.makedirs(self._dir, exist_ok=True)
        self._fh: Any = open(self._dir / "decisions.jsonl", "a")

    def log(self, event: DecisionEvent) -> None:
        """Write one event as a JSON line. Flushes immediately."""
        if self._fh is None:
            return
        self._fh.write(json.dumps(asdict(event)) + "\n")
        self._fh.flush()

    def close(self) -> None:
        """Close the file handle. Safe to call multiple times."""
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
