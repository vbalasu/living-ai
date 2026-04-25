"""Memory subsystem.

Files (identity, goals, learnings) live in a UC Volume mounted at /Volumes/...
Episodic events are appended to a JSONL file per UTC date in the same volume.
Delta tables exist for future SQL queries but are not the primary write path.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import Config


@dataclass
class Event:
    id: str
    ts: str
    kind: str
    channel: str | None = None
    thread_id: str | None = None
    payload: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "kind": self.kind,
            "channel": self.channel,
            "thread_id": self.thread_id,
            "payload": self.payload or {},
        }


class Memory:
    """Read identity/goals/learnings; append events; tail recent events for context."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.config_dir = Path(cfg.config_volume_path)
        self.episodes_dir = self.config_dir / "episodes"
        self.episodes_dir.mkdir(parents=True, exist_ok=True)

    # --- identity / goals / learnings (read-mostly) ---

    def identity(self) -> str:
        return self._read_or_default("identity.md", default_identity(self.cfg.agent_name))

    def goals(self) -> str:
        return self._read_or_default("goals.md", default_goals(self.cfg.agent_name))

    def learnings(self) -> str:
        return self._read_or_default("learnings.md", "# Learnings\n\n*(empty — accumulates over time)*\n")

    def _read_or_default(self, name: str, default: str) -> str:
        path = self.config_dir / name
        if not path.exists():
            path.write_text(default)
            return default
        return path.read_text()

    # --- episodic event log ---

    def append_event(self, kind: str, channel: str | None = None,
                     thread_id: str | None = None, payload: dict | None = None) -> Event:
        ev = Event(
            id=str(uuid.uuid4()),
            ts=datetime.now(timezone.utc).isoformat(),
            kind=kind,
            channel=channel,
            thread_id=thread_id,
            payload=payload,
        )
        date = ev.ts[:10]
        path = self.episodes_dir / f"{date}.jsonl"
        with path.open("a") as f:
            f.write(json.dumps(ev.to_dict()) + "\n")
        return ev

    def recent_events(self, limit: int = 30) -> list[dict]:
        """Return the most recent N events across the last few days."""
        events: list[dict] = []
        files = sorted(self.episodes_dir.glob("*.jsonl"), reverse=True)
        for f in files:
            with f.open() as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        events.append(json.loads(line))
            if len(events) >= limit * 3:
                break
        events.sort(key=lambda e: e["ts"], reverse=True)
        return events[:limit]


def default_identity(agent_name: str) -> str:
    return f"""# Identity

**Name**: {agent_name}

**Persona**: Concise. Curious. Takes small initiatives without asking. Asks before risky actions.

**Values**:
- Respect the user's time
- Be honest about uncertainty
- Show your work; don't hide reasoning when asked

**Created**: {datetime.now(timezone.utc).date().isoformat()}
"""


def default_goals(agent_name: str) -> str:
    return f"""# Goals

## Active
- [ ] (P0, ongoing) Introduce yourself and learn what the user cares about
- [ ] (P1, daily) Summarize each conversation into learnings.md
- [ ] (P2, when wallet enabled) Report wallet balance daily

## Completed
"""
