"""FastAPI entrypoint for the living AI agent (April).

Runs as a Databricks App. UC Volumes mount automatically. Serving-endpoint
auth is via the App's service principal (Databricks SDK picks it up from env).

Telegram I/O runs in long-polling mode in a background asyncio task. Inbound
webhooks aren't viable on Databricks Apps because the workspace OAuth gate
intercepts them, so the agent reaches out to api.telegram.org instead.
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
from telegram import TelegramClient

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

    last_chat_id: dict[str, int] = {}

    async def on_telegram_message(chat_id: int, text: str):
        last_chat_id["id"] = chat_id
        thread_id = f"chat:{chat_id}"
        result = await asyncio.to_thread(
            cognition.respond, text, "telegram", thread_id
        )
        await telegram.send(chat_id, result["text"])

    async def on_proactive(text: str):
        if telegram is None or not last_chat_id.get("id"):
            log.info("proactive message (no channel ready): %s", text[:80])
            return
        await telegram.send(last_chat_id["id"], text)

    tasks: list[asyncio.Task] = [
        asyncio.create_task(heartbeat_loop(cfg, memory, cognition, on_proactive)),
    ]
    if telegram is not None:
        tasks.append(asyncio.create_task(telegram.poll_loop(on_telegram_message)))

    log.info(
        "agent %s online; tick=%ss; llm=%s; telegram=%s",
        cfg.agent_name, cfg.heartbeat_seconds, cfg.llm_endpoint,
        "polling" if telegram else "pending",
    )

    yield

    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
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
        "telegram": "polling" if app.state.telegram else "pending",
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
