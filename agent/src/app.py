"""FastAPI entrypoint for the living AI agent (April).

Runs as a Databricks App. UC Volumes mount automatically. FMAPI auth is via
the App's service principal (Databricks SDK picks it up from env).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

import config
from cognition import Cognition
from heartbeat import heartbeat_loop
from memory import Memory
from telegram import TelegramClient, make_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = config.load()
    memory = Memory(cfg)
    cognition = Cognition(cfg, memory)
    telegram = TelegramClient.from_secrets(cfg.secrets_scope)

    app.state.cfg = cfg
    app.state.memory = memory
    app.state.cognition = cognition
    app.state.telegram = telegram

    app.include_router(make_router(cognition, telegram), prefix="/telegram")

    last_chat_id: dict[str, int] = {}

    async def on_proactive(text: str):
        if telegram is None or not last_chat_id.get("id"):
            log.info("proactive message (no channel ready): %s", text[:80])
            return
        await telegram.send(last_chat_id["id"], text)

    task = asyncio.create_task(heartbeat_loop(cfg, memory, cognition, on_proactive))

    log.info("agent %s online; tick=%ss; llm=%s; telegram=%s",
             cfg.agent_name, cfg.heartbeat_seconds, cfg.llm_endpoint,
             "configured" if telegram else "pending")

    yield

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Living AI Agent", lifespan=lifespan)


@app.get("/")
async def root():
    cfg = app.state.cfg
    return {
        "agent": cfg.agent_name,
        "llm_endpoint": cfg.llm_endpoint,
        "heartbeat_seconds": cfg.heartbeat_seconds,
        "telegram": "configured" if app.state.telegram else "pending",
    }


@app.get("/health")
async def health():
    return {"status": "alive"}


@app.get("/snapshot")
async def snapshot():
    """Quick introspection: identity, goals, last 10 events."""
    memory: Memory = app.state.memory
    return {
        "identity": memory.identity()[:1000],
        "goals": memory.goals()[:1000],
        "recent_events": memory.recent_events(limit=10),
    }
