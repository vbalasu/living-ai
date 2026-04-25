"""Memory subsystem for Databricks Apps.

- Identity / goals / learnings: Markdown files in a UC Volume, accessed via the
  SDK Files API (Apps don't auto-mount /Volumes as POSIX).
- Episodic events: Lakebase Postgres `events` table.
- Wallet ledger: Lakebase Postgres `wallet_ledger` table (used by the wallet skill).
- Semantic facts: Lakebase Postgres `semantic_facts` table (populated by nightly job).
"""
from __future__ import annotations

import io
import json
import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from databricks.sdk import WorkspaceClient

from config import Config
from lakebase import Lakebase

log = logging.getLogger(__name__)


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
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.config_path = cfg.config_volume_path.rstrip("/")
        self._w = WorkspaceClient()
        self._lock = threading.Lock()
        self._lakebase = Lakebase(cfg.lakebase_instance) if cfg.lakebase_instance else None
        self._buffer: list[dict] = []

        self._ensure_seed_files()

    # --- file primitives (UC Volume via SDK Files API) ---

    def _read(self, path: str) -> str | None:
        try:
            resp = self._w.files.download(path)
            return resp.contents.read().decode("utf-8")
        except Exception as exc:
            if "NOT_FOUND" in str(exc) or "404" in str(exc):
                return None
            log.warning("read failed for %s: %s", path, exc)
            return None

    def _write(self, path: str, content: str) -> None:
        self._w.files.upload(path, io.BytesIO(content.encode("utf-8")), overwrite=True)

    def _ensure_seed_files(self) -> None:
        seeds = {
            f"{self.config_path}/identity.md": default_identity(self.cfg.agent_name),
            f"{self.config_path}/goals.md": default_goals(self.cfg.agent_name),
            f"{self.config_path}/learnings.md":
                "# Learnings\n\n*(empty — accumulates over time)*\n",
        }
        for path, content in seeds.items():
            try:
                if self._read(path) is None:
                    self._write(path, content)
            except Exception:
                log.exception("seed write failed for %s", path)

    # --- identity / goals / learnings ---

    def identity(self) -> str:
        return self._read(f"{self.config_path}/identity.md") or default_identity(self.cfg.agent_name)

    def goals(self) -> str:
        return self._read(f"{self.config_path}/goals.md") or default_goals(self.cfg.agent_name)

    def learnings(self) -> str:
        return self._read(f"{self.config_path}/learnings.md") or "# Learnings\n"

    # --- episodic events (Lakebase) ---

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
        with self._lock:
            self._buffer.append(ev.to_dict())
            if len(self._buffer) > 200:
                self._buffer = self._buffer[-200:]

        if self._lakebase is not None:
            try:
                with self._lakebase.cursor() as cur:
                    cur.execute(
                        "INSERT INTO events (id, ts, kind, channel, thread_id, payload) "
                        "VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
                        (
                            ev.id,
                            ev.ts,
                            ev.kind,
                            ev.channel,
                            ev.thread_id,
                            json.dumps(ev.payload or {}),
                        ),
                    )
            except Exception:
                log.exception("event persist to lakebase failed; buffered in memory")
        return ev

    def recent_events(self, limit: int = 30) -> list[dict]:
        with self._lock:
            buffered = list(self._buffer)
        if len(buffered) >= limit:
            return list(reversed(buffered))[:limit]

        events = list(reversed(buffered))
        if self._lakebase is None:
            return events[:limit]

        try:
            with self._lakebase.cursor() as cur:
                cur.execute(
                    "SELECT id, ts, kind, channel, thread_id, payload "
                    "FROM events ORDER BY ts DESC LIMIT %s",
                    (limit,),
                )
                rows = cur.fetchall()
            for row in rows:
                events.append({
                    "id": str(row[0]),
                    "ts": row[1].isoformat() if row[1] else None,
                    "kind": row[2],
                    "channel": row[3],
                    "thread_id": row[4],
                    "payload": row[5] or {},
                })
        except Exception:
            log.exception("recent_events lakebase read failed")
        # de-dup by id, prefer first-seen (buffer)
        seen: set[str] = set()
        out: list[dict] = []
        for e in events:
            if e["id"] not in seen:
                seen.add(e["id"])
                out.append(e)
            if len(out) >= limit:
                break
        return out


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
