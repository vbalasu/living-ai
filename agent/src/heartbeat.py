"""Heartbeat: ticks every N seconds, decides whether to invoke cognition."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from cognition import Cognition
from config import Config
from memory import Memory

log = logging.getLogger(__name__)


async def heartbeat_loop(cfg: Config, memory: Memory, cognition: Cognition,
                         on_proactive_message=None) -> None:
    log.info("heartbeat starting; tick=%ss", cfg.heartbeat_seconds)
    memory.append_event(kind="boot", payload={"agent_name": cfg.agent_name})

    while True:
        try:
            await asyncio.sleep(cfg.heartbeat_seconds)
            tick_ts = datetime.now(timezone.utc).isoformat()
            memory.append_event(kind="tick", payload={"ts": tick_ts})

            if on_proactive_message is None:
                continue

            result = await asyncio.to_thread(cognition.idle_reflection)
            if result and result.get("text"):
                await on_proactive_message(result["text"])
        except asyncio.CancelledError:
            log.info("heartbeat cancelled")
            raise
        except Exception:
            log.exception("heartbeat tick failed; continuing")
