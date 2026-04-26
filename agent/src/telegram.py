"""Telegram channel adapter — long-polling mode.

Databricks Apps can't be exposed publicly, so inbound webhooks from Telegram
get bounced by the workspace OAuth gate. We sidestep that by *outbound*
long-polling: the agent calls api.telegram.org/getUpdates and processes
messages itself. Outbound HTTPS is unrestricted on Databricks Apps.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any, Awaitable, Callable

import httpx
from databricks.sdk import WorkspaceClient

log = logging.getLogger(__name__)


# Async callback: (chat_id, text) -> None
OnMessage = Callable[[int, str], Awaitable[None]]


class TelegramClient:
    def __init__(self, token: str, primary_user_handle: str | None):
        self.token = token
        self.primary_user_handle = (primary_user_handle or "").lstrip("@").lower()
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
            return cls(
                token=base64.b64decode(token).decode(),
                primary_user_handle=base64.b64decode(handle).decode() if handle else None,
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

    async def delete_webhook(self) -> None:
        """Telegram disallows getUpdates while a webhook is set. Make sure no webhook is configured."""
        async with httpx.AsyncClient(timeout=15) as c:
            try:
                r = await c.post(
                    f"{self.api}/deleteWebhook",
                    json={"drop_pending_updates": False},
                )
                if r.status_code >= 300:
                    log.warning("telegram deleteWebhook failed: %s %s", r.status_code, r.text)
                else:
                    log.info("telegram webhook cleared (long-polling mode)")
            except Exception:
                log.exception("telegram deleteWebhook errored; continuing")

    async def poll_loop(self, on_message: OnMessage) -> None:
        """Long-poll Telegram for new messages and dispatch to on_message.

        Runs forever. Cancellable via the asyncio task. Reconnects on transient
        failures with exponential backoff capped at 60s.
        """
        await self.delete_webhook()

        offset: int | None = None
        backoff = 1.0
        long_poll_timeout = 25  # seconds
        async with httpx.AsyncClient(timeout=long_poll_timeout + 10) as client:
            while True:
                try:
                    params: dict[str, Any] = {
                        "timeout": long_poll_timeout,
                        "allowed_updates": '["message","edited_message"]',
                    }
                    if offset is not None:
                        params["offset"] = offset
                    r = await client.get(f"{self.api}/getUpdates", params=params)
                    if r.status_code != 200:
                        log.warning("getUpdates http %s: %s", r.status_code, r.text[:200])
                        await asyncio.sleep(min(backoff, 60))
                        backoff = min(backoff * 2, 60)
                        continue
                    body = r.json()
                    if not body.get("ok"):
                        log.warning("getUpdates not ok: %s", body)
                        await asyncio.sleep(min(backoff, 60))
                        backoff = min(backoff * 2, 60)
                        continue

                    backoff = 1.0  # reset after a successful round-trip

                    for update in body.get("result", []):
                        offset = update["update_id"] + 1
                        await self._dispatch_update(update, on_message)

                except asyncio.CancelledError:
                    log.info("telegram poll cancelled")
                    raise
                except Exception:
                    log.exception("telegram poll iteration failed; backing off")
                    await asyncio.sleep(min(backoff, 60))
                    backoff = min(backoff * 2, 60)

    async def _dispatch_update(self, update: dict, on_message: OnMessage) -> None:
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return
        chat_id = msg["chat"]["id"]
        from_user = msg.get("from") or {}
        username = (from_user.get("username") or "").lower()

        if self.primary_user_handle and username != self.primary_user_handle:
            await self.send(
                chat_id,
                f"Sorry, I only respond to @{self.primary_user_handle}.",
            )
            return

        text = msg.get("text") or ""
        if not text:
            return

        await on_message(chat_id, text)
