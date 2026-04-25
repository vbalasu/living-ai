"""Telegram channel adapter.

The bot token and primary user handle are loaded from Databricks Secrets at startup.
Inbound webhooks are verified via the X-Telegram-Bot-Api-Secret-Token header.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx
from databricks.sdk import WorkspaceClient
from fastapi import APIRouter, Header, HTTPException, Request

from cognition import Cognition

log = logging.getLogger(__name__)


class TelegramClient:
    def __init__(self, token: str, primary_user_handle: str | None,
                 webhook_secret: str | None = None):
        self.token = token
        self.primary_user_handle = (primary_user_handle or "").lstrip("@").lower()
        self.webhook_secret = webhook_secret
        self.api = f"https://api.telegram.org/bot{token}"

    @classmethod
    def from_secrets(cls, scope: str) -> "TelegramClient | None":
        try:
            w = WorkspaceClient()
            token = w.secrets.get_secret(scope=scope, key="telegram_bot_token").value
            handle = ""
            try:
                handle = w.secrets.get_secret(scope=scope, key="telegram_primary_user_handle").value
            except Exception:
                pass
            secret = ""
            try:
                secret = w.secrets.get_secret(scope=scope, key="telegram_webhook_secret").value
            except Exception:
                pass
            import base64
            return cls(
                token=base64.b64decode(token).decode(),
                primary_user_handle=base64.b64decode(handle).decode() if handle else None,
                webhook_secret=base64.b64decode(secret).decode() if secret else None,
            )
        except Exception as exc:
            log.warning("Telegram secrets not configured yet: %s", exc)
            return None

    async def send(self, chat_id: int, text: str) -> None:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{self.api}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            )
            if r.status_code >= 300:
                log.warning("telegram send failed: %s %s", r.status_code, r.text)


def make_router(cognition: Cognition, telegram: TelegramClient | None) -> APIRouter:
    router = APIRouter()

    @router.post("/webhook")
    async def webhook(
        request: Request,
        x_telegram_bot_api_secret_token: str | None = Header(default=None),
    ):
        if telegram is None:
            raise HTTPException(503, "Telegram not configured")
        if telegram.webhook_secret and x_telegram_bot_api_secret_token != telegram.webhook_secret:
            raise HTTPException(403, "bad webhook secret")

        update = await request.json()
        message = update.get("message") or update.get("edited_message")
        if not message:
            return {"ok": True, "ignored": "no message"}

        chat_id = message["chat"]["id"]
        from_user = message.get("from", {}) or {}
        username = (from_user.get("username") or "").lower()

        if telegram.primary_user_handle and username != telegram.primary_user_handle:
            await telegram.send(
                chat_id,
                f"Sorry, I only respond to @{telegram.primary_user_handle}.",
            )
            return {"ok": True, "ignored": "not primary user"}

        text = message.get("text") or ""
        if not text:
            return {"ok": True, "ignored": "no text"}

        thread_id = f"chat:{chat_id}"
        result = await asyncio.to_thread(
            cognition.respond, text, "telegram", thread_id
        )
        await telegram.send(chat_id, result["text"])
        return {"ok": True}

    return router
