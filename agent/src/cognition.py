"""Cognition core: builds context, calls the configured serving endpoint, returns a response.

Both Databricks Foundation Model API endpoints (e.g. databricks-qwen3-next-80b-a3b-instruct,
which is available on Free Edition) and user-created external model endpoints (OpenAI,
Anthropic, Bedrock, etc.) speak the same OpenAI-protocol API surface, so we always use
WorkspaceClient.serving_endpoints.get_open_ai_client(). Picking a different LLM is just
a matter of changing the LLM_ENDPOINT env var.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from databricks.sdk import WorkspaceClient

from config import Config
from memory import Memory

log = logging.getLogger(__name__)


SYSTEM_PROMPT_TEMPLATE = """You are {agent_name}, an autonomous AI agent.

This is your persistent identity. Read and inhabit it.

{identity}

These are your current goals.

{goals}

These are facts you have learned over time.

{learnings}

The messages that follow this system prompt are the live, continuing conversation
with the user. Treat them as one ongoing exchange — do not introduce yourself
again, do not greet the user as if it's a fresh session, and do not repeat
earlier turns. Pick up from the most recent message.

Behavior:
- Respond as {agent_name}, in the user's primary channel.
- Take small initiatives when warranted by goals or stimuli.
- Do not invent capabilities you don't have. If you can't do something, say so.
- Keep responses tight unless detail is requested.
- Current UTC time: {now}
"""


# How many user/assistant pairs of history to thread into the messages list.
HISTORY_PAIRS = 30  # = up to 60 messages of conversation context
# Approximate char budget for the threaded history (≈ 3K tokens).
HISTORY_CHAR_BUDGET = 12_000


class Cognition:
    def __init__(self, cfg: Config, memory: Memory):
        self.cfg = cfg
        self.memory = memory
        self._client = None

    def client(self):
        if self._client is None:
            w = WorkspaceClient()
            self._client = w.serving_endpoints.get_open_ai_client()
        return self._client

    def build_system_prompt(self) -> str:
        return SYSTEM_PROMPT_TEMPLATE.format(
            agent_name=self.cfg.agent_name,
            identity=self.memory.identity(),
            goals=self.memory.goals(),
            learnings=self.memory.learnings(),
            now=datetime.now(timezone.utc).isoformat(),
        )

    def respond(self, user_message: str, channel: str = "telegram",
                thread_id: str | None = None) -> dict[str, Any]:
        system_prompt = self.build_system_prompt()

        history: list[dict] = []
        if thread_id:
            history = self.memory.conversation_history(
                thread_id,
                limit_pairs=HISTORY_PAIRS,
                char_budget=HISTORY_CHAR_BUDGET,
            )

        self.memory.append_event(
            kind="stimulus",
            channel=channel,
            thread_id=thread_id,
            payload={"text": user_message},
        )

        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        try:
            resp = self.client().chat.completions.create(
                model=self.cfg.llm_endpoint,
                messages=messages,
                max_tokens=1024,
            )
            text = resp.choices[0].message.content or ""
            usage = {
                "prompt_tokens": getattr(resp.usage, "prompt_tokens", None),
                "completion_tokens": getattr(resp.usage, "completion_tokens", None),
                "total_tokens": getattr(resp.usage, "total_tokens", None),
            }
        except Exception as exc:
            log.exception("cognition failure")
            self.memory.append_event(
                kind="error",
                channel=channel,
                thread_id=thread_id,
                payload={"error": str(exc), "stage": "llm_call"},
            )
            return {"text": f"({self.cfg.agent_name} hit an error and is recovering.)", "error": str(exc)}

        self.memory.append_event(
            kind="response",
            channel=channel,
            thread_id=thread_id,
            payload={"text": text, "usage": usage},
        )
        return {"text": text, "usage": usage}

    IDLE_REFLECTION_INTERVAL_SECONDS = 1800

    def idle_reflection(self) -> dict[str, Any] | None:
        """Called on idle ticks. Returns a response only if the agent decides to act.

        Skips if any stimulus/response — including the previous idle reflection's
        own response — happened within IDLE_REFLECTION_INTERVAL_SECONDS. We must
        scan past tick events at the head of the log: heartbeat_loop appends a
        tick event right before calling us, so recent_events()[0] is always a
        tick, not a stimulus/response.
        """
        recent = self.memory.recent_events(limit=50)
        last_io = next(
            (e for e in recent if e.get("kind") in ("stimulus", "response")),
            None,
        )
        if last_io and last_io.get("ts"):
            ts = datetime.fromisoformat(last_io["ts"].replace("Z", "+00:00"))
            elapsed = (datetime.now(timezone.utc) - ts).total_seconds()
            if elapsed < self.IDLE_REFLECTION_INTERVAL_SECONDS:
                return None

        prompt = (
            "It has been a while since the last stimulus. "
            "Look at your goals. Is there anything worth proactively doing or saying right now? "
            "If yes, write a short message you would send to the user. "
            "If no, reply with the single word: PASS."
        )
        result = self.respond(prompt, channel="internal", thread_id="idle")
        if result.get("text", "").strip().upper().startswith("PASS"):
            return None
        return result
