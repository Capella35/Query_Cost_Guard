"""Episodic memory for the Investigator: what happened, per query shape.

Local-first JSONL store — one incident per line, recalled by fingerprint.
The hosted alternative (Mem0) implements the same two methods; swap it in
when you want cloud memory without changing the Investigator.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Incident:
    fingerprint: str
    summary: str
    outcome: str  # e.g. "threshold tightened", "false alarm", "rewrite shipped"
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class EpisodicMemory:
    """Record incidents, recall them by query-shape fingerprint."""

    def __init__(self, path: str | Path = "runs/incidents.jsonl"):
        self.path = Path(path)

    def record(self, incident: Incident) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(asdict(incident)) + "\n")

    def recall(self, fingerprint: str, limit: int = 3) -> list[Incident]:
        if not self.path.exists():
            return []
        out: list[Incident] = []
        with self.path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                if d.get("fingerprint") == fingerprint:
                    out.append(Incident(**d))
        return out[-limit:]
