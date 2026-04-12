"""Step-level checkpoint — save/load serializable snapshots at key decision points.

p231: Checkpoints are saved at phase advances, gate stops, gate redirects, and
materialization gate firings. Each checkpoint captures the full conversation
history + control-plane state, enabling replay-from-step analysis.

All operations are exception-safe — failures here must never crash a run.
"""

from __future__ import annotations

import dataclasses
import gzip
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Checkpoint:
    """A serializable snapshot of agent state at a key decision point."""

    step_n: int
    instance_id: str
    attempt: int
    trigger: str  # phase_advance | gate_stop | gate_redirect | materialization_gate
    messages_so_far: list[dict[str, Any]]  # full conversation history
    cp_state: dict[str, Any]  # serialized control-plane state
    phase_records: list[dict[str, Any]]
    pending_hints: list[str]
    prompt_sections: list[dict[str, Any]]  # from prompt_snapshot (p229)
    metadata: dict[str, Any] = field(default_factory=dict)  # timestamp_ms, phase, model


def save_checkpoint(checkpoint: Checkpoint, instance_dir: Path) -> Path | None:
    """Save a checkpoint to ``attempt_{N}/checkpoints/step_{N}.json.gz``.

    Uses gzip compression (messages array is 50-200KB, gzip -> 10-30KB).
    Returns the path on success, None on failure. Never raises.
    """
    try:
        ckpt_dir = instance_dir / f"attempt_{checkpoint.attempt}" / "checkpoints"
        os.makedirs(ckpt_dir, exist_ok=True)
        path = ckpt_dir / f"step_{checkpoint.step_n}.json.gz"
        data = json.dumps(dataclasses.asdict(checkpoint), ensure_ascii=False)
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write(data)
        return path
    except Exception:
        return None


def load_checkpoint(path: Path) -> Checkpoint | None:
    """Load a checkpoint from a gzip-compressed JSON file. Never raises."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            raw = json.loads(f.read())
        return Checkpoint(**raw)
    except Exception:
        return None


def list_checkpoints(instance_dir: Path, attempt: int) -> list[dict[str, Any]]:
    """List checkpoint metadata for a given attempt.

    Returns a list of ``{"step_n": N, "trigger": str, "phase": str, "path": str}``
    sorted by step_n. Loads each file to extract metadata fields.
    Never raises — returns empty list on error.
    """
    results: list[dict[str, Any]] = []
    try:
        ckpt_dir = instance_dir / f"attempt_{attempt}" / "checkpoints"
        if not ckpt_dir.exists():
            return results
        for gz_path in sorted(ckpt_dir.glob("step_*.json.gz")):
            try:
                with gzip.open(gz_path, "rt", encoding="utf-8") as f:
                    raw = json.loads(f.read())
                results.append({
                    "step_n": raw.get("step_n", -1),
                    "trigger": raw.get("trigger", "unknown"),
                    "phase": raw.get("metadata", {}).get("phase", "unknown"),
                    "path": str(gz_path),
                })
            except Exception:
                continue
    except Exception:
        pass
    return results
